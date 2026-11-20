"""Lookup Discogs: credits (producers, engineers, studios, artwork, composers), year and label.

Uses the official Discogs API (https://www.discogs.com/developers). It needs a free personal
access token: ``"discogs_token"`` under ``"enrichment"`` in config.json, or the DISCOGS_TOKEN
environment variable. One request every 1.1 s keeps well under the 60 requests a minute allowed.
Everything here only fills gaps: tags and MusicBrainz win (see services/resolver.py).
"""
import os
import re
from dataclasses import dataclass, field

from django.utils import timezone

from library import repository
from library.models import Album, AlbumFacet, DiscogsAlbum, EnrichmentStatus, FacetKind
from library.services import resolver
from library.services.http_cache import CachedJsonClient, ServiceError

SOURCE = "discogs"
NAME_THRESHOLD = 0.85
TITLE_THRESHOLD = 0.85
CANDIDATES = 25


class DiscogsError(ServiceError):
    pass


def configured_token():
    return (os.environ.get("DISCOGS_TOKEN")
            or repository.get_config().get("enrichment", {}).get("discogs_token") or "").strip()


class DiscogsClient(CachedJsonClient):
    base_url = "https://api.discogs.com/"
    service_name = "Discogs"
    min_interval = 1.1
    cache_name = "discogs_cache"
    error_class = DiscogsError

    def __init__(self, *, token=None, **kwargs):
        super().__init__(**kwargs)
        self.token = configured_token() if token is None else token

    def extra_headers(self):
        return {"Authorization": f"Discogs token={self.token}"} if self.token else {}

    def search_releases(self, artist, title):
        data = self.get("database/search", type="release", artist=artist, release_title=title,
                        per_page=CANDIDATES) or {}
        return data.get("results", [])

    def master(self, master_id):
        return self.get(f"masters/{master_id}")

    def release(self, release_id):
        return self.get(f"releases/{release_id}")


# -- reading the answers -----------------------------------------------------------

def clean_name(name):
    """Discogs tells apart people of one name with "(2)"; "The Band*" marks a name variation."""
    return re.sub(r"\s*\(\d+\)$", "", (name or "").strip()).rstrip("*").strip()


# (words in the role) -> credit kind; checked in order, the first hit wins
ROLE_KINDS = (
    (("lyrics", "words by"), FacetKind.LYRICIST),
    (("written-by", "written by", "composed", "music by", "songwriter"), FacetKind.COMPOSER),
    (("producer",), FacetKind.PRODUCER),
    (("engineer", "recorded by", "mixed by", "mastered by", "mastering", "mixing", "recording"), FacetKind.ENGINEER),
    (("artwork", "cover", "illustration", "design", "painting", "layout"), FacetKind.ARTWORK),
)
STUDIO_ENTITIES = ("recorded at", "mixed at", "mastered at")


def role_kind(role):
    base = re.sub(r"\[.*?\]", "", role or "").casefold()
    for words, kind in ROLE_KINDS:
        if any(word in base for word in words):
            return kind
    return None


def parse_credits(release):
    """Discogs release -> [{"kind", "name", "role", "area", "count", "evidence"}] (MusicBrainz's shape)."""
    evidence = f"Discogs release {release.get('id', '')}".strip()
    merged = {}

    def add(kind, name, role):
        name = clean_name(name)
        if not name:
            return
        entry = merged.setdefault((kind, name.casefold()), {
            "kind": str(kind), "name": name, "role": "", "area": "", "count": 0, "evidence": evidence})
        entry["count"] += 1
        role = re.sub(r"\s+", " ", role or "").strip()
        if role and role.casefold() not in entry["role"].casefold():
            entry["role"] = f"{entry['role']}, {role}" if entry["role"] else role

    for person in release.get("extraartists") or []:
        kind = role_kind(person.get("role"))
        if kind:
            add(kind, person.get("name"), person.get("role"))
    for company in release.get("companies") or []:
        if (company.get("entity_type_name") or "").casefold() in STUDIO_ENTITIES:
            add(FacetKind.STUDIO, company.get("name"), (company.get("entity_type_name") or "").lower())
    return list(merged.values())


def _norm(text):
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", text or "").strip()


def match_release(client, artist_name, album_title):
    """The Discogs release of this album to read credits from, or None.

    Search hits are "Artist - Title". Of those that match both, the album's master release (the
    canonical edition) is preferred; otherwise the earliest pressing.
    """
    from library.services.enrichment import similarity

    candidates = []
    for row in client.search_releases(artist_name, _norm(album_title)):
        artist, _, title = (row.get("title") or "").partition(" - ")
        if (similarity(clean_name(artist), artist_name) >= NAME_THRESHOLD
                and similarity(_norm(title), _norm(album_title)) >= TITLE_THRESHOLD):
            candidates.append(row)
    if not candidates:
        return None
    candidates.sort(key=lambda row: (not row.get("master_id"), int(row.get("year") or 9999)))
    best = candidates[0]
    release_id = best["id"]
    if best.get("master_id"):
        master = client.master(best["master_id"]) or {}
        release_id = master.get("main_release") or release_id
    release = client.release(release_id)
    return release


# -- running -------------------------------------------------------------------------

@dataclass
class DiscogsStats:
    albums_total: int = 0
    albums_done: int = 0
    albums_matched: int = 0
    albums_not_found: int = 0
    credits_found: int = 0
    errors: list = field(default_factory=list)
    current: str = ""


def albums_to_look_up(refresh=False, album_ids=None, artist_ids=None):
    queryset = Album.objects.select_related("artist").order_by("artist__artist_name", "album_name")
    if album_ids:
        return list(queryset.filter(pk__in=album_ids))
    if artist_ids:
        return list(queryset.filter(artist_id__in=artist_ids))
    if refresh:
        return list(queryset)
    return list(queryset.exclude(discogs__status__in=[EnrichmentStatus.MATCHED, EnrichmentStatus.NOT_FOUND]))


def run(*, client=None, refresh=False, artist_ids=None, album_ids=None, progress=None, should_stop=None):
    """Lookup Discogs for the library, some artists, or some albums."""
    client = client or DiscogsClient()
    if not client.token:
        raise DiscogsError(
            'Discogs needs a free personal access token: create one at discogs.com/settings/developers '
            'and set the DISCOGS_TOKEN environment variable (or "discogs_token" under "enrichment" in config.json).')
    albums = albums_to_look_up(refresh=refresh, album_ids=album_ids, artist_ids=artist_ids)
    stats = DiscogsStats(albums_total=len(albums))
    failures_in_a_row = 0
    touched = []
    for album in albums:
        if should_stop and should_stop():
            break
        if failures_in_a_row >= 3:
            stats.errors.append("Stopped: Discogs refused three requests in a row (check the token).")
            break
        stats.current = f"{album.artist.artist_name} – {album.album_name}"
        if progress:
            progress(stats)
        try:
            look_up_album(client, album, stats)
            failures_in_a_row = 0
        except Exception as error:  # one album's problem must not end the run
            message = str(error) if isinstance(error, DiscogsError) else f"{type(error).__name__}: {error}"
            stats.errors.append(f"{stats.current}: {message}")
            failures_in_a_row = failures_in_a_row + 1 if "answered 401" in message or "answered 403" in message else 0
            DiscogsAlbum.objects.update_or_create(
                album=album, defaults={"status": EnrichmentStatus.ERROR, "note": message[:300]})
        touched.append(album.pk)
        stats.albums_done += 1
    if touched:
        resolver.resolve_for_albums(touched)
    stats.current = ""
    if progress:
        progress(stats)
    return stats


def look_up_album(client, album, stats):
    entry, _ = DiscogsAlbum.objects.get_or_create(album=album)
    now = timezone.now()
    release = match_release(client, album.artist.artist_name, album.album_name)
    if not release:
        entry.status, entry.note, entry.enriched_at = EnrichmentStatus.NOT_FOUND, "No matching release on Discogs", now
        entry.credits = []
        entry.save()
        _store_facets(album, [])
        stats.albums_not_found += 1
        return False
    labels = release.get("labels") or []
    credits = parse_credits(release)
    entry.release_id, entry.url = release.get("id"), (release.get("uri") or "")[:300]
    entry.title = (release.get("title") or "")[:300]
    entry.release_year = release.get("year") or None
    entry.label = clean_name(labels[0].get("name"))[:100] if labels else ""
    entry.credits = credits
    entry.status, entry.note, entry.enriched_at = EnrichmentStatus.MATCHED, "" if credits else "Matched, but the release lists no credits", now
    entry.save()
    _store_facets(album, credits)
    stats.albums_matched += 1
    stats.credits_found += len(credits)
    return True


def _store_facets(album, credits):
    from library.services.enrichment import link_facets

    AlbumFacet.objects.filter(album=album, source=SOURCE).delete()
    link_facets(AlbumFacet, "album", album,
                [(credit["kind"], credit["name"], "", credit["role"], 50) for credit in credits], source=SOURCE)
