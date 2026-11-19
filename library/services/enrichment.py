"""Enriches artists and albums with data from MusicBrainz.

Matching tolerates spelling differences (case, accents, punctuation, a leading
"The", "(Remastered)"-style suffixes). When several MusicBrainz artists share a
name, the one whose discography contains the most of our album titles wins.

Nothing read from the MP3 files is overwritten: results go to ArtistProfile /
AlbumProfile and to facet links whose ``source`` is "musicbrainz". A refresh
replaces only those rows.
"""
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from library.models import (
    Album, AlbumFacet, AlbumProfile, Artist, ArtistFacet, ArtistProfile,
    EnrichmentStatus, Facet, FacetKind,
)
from library.services.musicbrainz import MusicBrainzClient, MusicBrainzError

SOURCE = "musicbrainz"
NAME_THRESHOLD = 0.85
TITLE_THRESHOLD = 0.85
MAX_CANDIDATES = 3
MAX_GENRES = 6
MBID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

EDITION_WORDS = (
    "remaster", "deluxe", "edition", "bonus", "expanded", "anniversary", "reissue",
    "version", "mono", "stereo", "disc", "cd", "lp", "special", "limited", "digipak",
)
EDITION_PATTERN = re.compile(r"[\(\[][^\)\]]*\b(%s)\w*\b[^\)\]]*[\)\]]" % "|".join(EDITION_WORDS))
NOT_INSTRUMENTS = {
    "original", "additional", "founder", "guest", "minor", "emeritus", "solo",
    "touring", "co", "assistant", "associate", "executive", "pre-production",
}

# MusicBrainz relationship type -> (facet kind, role shown next to the name)
ARTIST_CREDIT_TYPES = {
    "producer": (FacetKind.PRODUCER, "producer"),
    "co-producer": (FacetKind.PRODUCER, "co-producer"),
    "engineer": (FacetKind.ENGINEER, "engineer"),
    "recording": (FacetKind.ENGINEER, "recording engineer"),
    "audio": (FacetKind.ENGINEER, "audio engineer"),
    "sound": (FacetKind.ENGINEER, "sound engineer"),
    "mix": (FacetKind.ENGINEER, "mixing"),
    "mastering": (FacetKind.ENGINEER, "mastering"),
    "design/illustration": (FacetKind.ARTWORK, "design / illustration"),
    "illustration": (FacetKind.ARTWORK, "illustration"),
    "design": (FacetKind.ARTWORK, "design"),
    "graphic design": (FacetKind.ARTWORK, "graphic design"),
    "artwork": (FacetKind.ARTWORK, "artwork"),
    "art direction": (FacetKind.ARTWORK, "art direction"),
    "photography": (FacetKind.ARTWORK, "photography"),
    "instrument": (FacetKind.MUSICIAN, "instrument"),
    "vocal": (FacetKind.MUSICIAN, "vocals"),
    "performer": (FacetKind.MUSICIAN, "performer"),
}
PLACE_CREDIT_TYPES = {
    "recorded at": "recorded",
    "recorded in": "recorded",
    "mixed at": "mixed",
    "mixed in": "mixed",
    "mastered at": "mastered",
    "mastered in": "mastered",
    "engineered at": "engineered",
    "produced at": "produced",
}


# -- text matching -------------------------------------------------------------

def normalize(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    text = EDITION_PATTERN.sub(" ", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"^\s*the\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(left, right):
    left, right = normalize(left), normalize(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def parse_mbid(text):
    match = MBID_PATTERN.search(text or "")
    return match.group(0).lower() if match else None


def year_of(date_text):
    match = re.match(r"(\d{4})", date_text or "")
    return int(match.group(1)) if match else None


def _rating(data):
    rating = (data or {}).get("rating") or {}
    value = rating.get("value")
    return (Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else None,
            rating.get("votes-count") or 0)


INSTRUMENT_ALIASES = {
    "membranophone": "drums",
    "drum set": "drums",
    "bass": "bass guitar",
    "electric bass guitar": "bass guitar",
    "keyboard": "keyboards",
}


def _clean_instrument(name):
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    return INSTRUMENT_ALIASES.get(name.casefold(), name)


# -- the run -------------------------------------------------------------------

@dataclass
class EnrichmentStats:
    artists_total: int = 0
    artists_done: int = 0
    artists_matched: int = 0
    artists_not_found: int = 0
    albums_matched: int = 0
    albums_not_found: int = 0
    errors: list = field(default_factory=list)
    current: str = ""


def artists_to_enrich(refresh=False, artist_ids=None):
    queryset = Artist.objects.order_by("artist_name")
    if artist_ids is not None:
        return list(queryset.filter(pk__in=artist_ids))
    if refresh:
        return list(queryset)
    # Artists never looked up (or that failed), plus matched ones with albums not looked up yet.
    done = [EnrichmentStatus.MATCHED, EnrichmentStatus.NOT_FOUND]
    pending = set(queryset.exclude(profile__status__in=done).values_list("pk", flat=True))
    with_new_albums = set(
        queryset.filter(profile__status=EnrichmentStatus.MATCHED)
        .filter(Q(album__profile__isnull=True)
                | Q(album__profile__status__in=[EnrichmentStatus.PENDING, EnrichmentStatus.ERROR]))
        .values_list("pk", flat=True)
    )
    return list(queryset.filter(pk__in=pending | with_new_albums))


def enrich_library(client=None, *, refresh=False, artist_ids=None, album_ids=None, progress=None, should_stop=None):
    """Lookup Details Online for the library, some artists, or only some albums.

    With ``album_ids`` each album's artist is matched again, but only those albums are looked up.
    """
    client = client or MusicBrainzClient()
    if album_ids:
        artist_ids = sorted(set(Album.objects.filter(pk__in=album_ids).values_list("artist_id", flat=True)))
    artists = artists_to_enrich(refresh=refresh, artist_ids=artist_ids)
    stats = EnrichmentStats(artists_total=len(artists))
    for artist in artists:
        if should_stop and should_stop():
            break
        stats.current = artist.artist_name
        if progress:
            progress(stats)
        try:
            enrich_artist(client, artist, stats=stats, refresh=refresh or artist_ids is not None, only_album_ids=album_ids)
        except Exception as error:  # one artist's problem must not end the whole run
            message = _error_text(error)
            stats.errors.append(f"{artist.artist_name}: {message}")
            _mark_error(ArtistProfile, artist=artist, note=message)
        stats.artists_done += 1
    stats.current = ""
    if progress:
        progress(stats)
    return stats


def _error_text(error):
    return str(error) if isinstance(error, MusicBrainzError) else f"{type(error).__name__}: {error}"


def _mark_error(model, note, **owner):
    try:
        model.objects.update_or_create(**owner, defaults={"status": EnrichmentStatus.ERROR, "note": note[:300]})
    except Exception:
        pass  # e.g. the artist or album was deleted by a library scan meanwhile


def _artist_tags_missing(profile, artist):
    """Common tags the artist still lacks (country, genres); empty when it is complete."""
    missing = []
    if not (profile.country or artist.artist_country):
        missing.append("country")
    if not ArtistFacet.objects.filter(artist=artist, source=SOURCE, facet__kind=FacetKind.GENRE).exists():
        missing.append("genre")
    return missing


def enrich_artist(client, artist, *, stats=None, refresh=True, only_album_ids=None):
    """Look an artist up, then its albums.

    For an album-only run the artist's stored tags come first: when it is already matched and
    has its country and genres, the artist search and detail requests are skipped (only its
    release groups are fetched, to match the albums) and the albums inherit the artist's tags.
    """
    stats = stats or EnrichmentStats()
    profile, _ = ArtistProfile.objects.get_or_create(artist=artist)
    albums = list(artist.album_set.all())
    titles = [album.album_name for album in albums]

    reuse_artist = (
        bool(only_album_ids)
        and profile.status == EnrichmentStatus.MATCHED
        and bool(profile.external_id)
        and not _artist_tags_missing(profile, artist)
    )
    if reuse_artist:
        release_groups = client.release_groups(profile.external_id)
    elif profile.id_set_manually and profile.external_id:
        mbid, score, note = profile.external_id, Decimal("1.00"), "MusicBrainz ID set by hand"
        release_groups = client.release_groups(mbid)
    else:
        mbid, score, release_groups, note = match_artist(client, artist.artist_name, titles, artist.artist_country)
        if mbid is None:
            _store_artist_not_found(profile, note)
            for album in albums:
                AlbumProfile.objects.update_or_create(
                    album=album, defaults={"status": EnrichmentStatus.NOT_FOUND, "note": "Artist not matched"}
                )
            stats.artists_not_found += 1
            return profile

    if not reuse_artist:
        data = client.artist(mbid)
        if data is None:
            _store_artist_not_found(profile, f"MusicBrainz has no artist {mbid}")
            stats.artists_not_found += 1
            return profile

        with transaction.atomic():
            _store_artist(profile, artist, data, score, note, release_groups, albums)
        stats.artists_matched += 1

    for album in albums:
        if only_album_ids and album.pk not in only_album_ids:
            continue
        album_profile = AlbumProfile.objects.filter(album=album).first()
        if not refresh and album_profile and album_profile.status in (EnrichmentStatus.MATCHED, EnrichmentStatus.NOT_FOUND):
            continue
        try:
            found = enrich_album(client, album, release_groups)
        except Exception as error:
            message = _error_text(error)
            stats.errors.append(f"{artist.artist_name} – {album.album_name}: {message}")
            _mark_error(AlbumProfile, album=album, note=message)
            continue
        if found:
            stats.albums_matched += 1
        else:
            stats.albums_not_found += 1
    for album in albums:
        if not only_album_ids or album.pk in only_album_ids:
            inherit_artist_tags(album)
    return profile


def match_artist(client, name, album_titles, country_hint=""):
    """(mbid, score, release groups, note) of the best MusicBrainz artist, or mbid None."""
    candidates = []
    for result in client.search_artists(name):
        names = [result.get("name", "")] + [alias.get("name", "") for alias in result.get("aliases") or []]
        name_score = max(similarity(name, candidate) for candidate in names)
        if name_score >= NAME_THRESHOLD:
            candidates.append((name_score, int(result.get("score") or 0), result))
    if not candidates:
        return None, None, [], f'No MusicBrainz artist is named like "{name}"'
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    best = None
    for name_score, search_score, result in candidates[:MAX_CANDIDATES]:
        release_groups = client.release_groups(result["id"])
        hits = sum(1 for title in album_titles if best_release_group(title, release_groups)[0] is not None)
        country_bonus = 1 if country_hint and country_hint.upper() in (result.get("country") or "", (result.get("area") or {}).get("name", "").upper()) else 0
        key = (hits, country_bonus, name_score, search_score)
        if best is None or key > best[0]:
            best = (key, result, release_groups, name_score, hits)
        if len(candidates) == 1 or (album_titles and hits == len(album_titles)):
            break

    _, result, release_groups, name_score, hits = best
    coverage = hits / len(album_titles) if album_titles else 0
    score = Decimal(str(round(name_score * (0.6 + 0.4 * coverage), 2)))
    if hits == 0 and len(candidates) > 1:
        note = f"{len(candidates)} artists share this name and none lists our albums; picked the closest name"
    else:
        note = f"{hits} of {len(album_titles)} album(s) found in its discography"
    return result["id"], score, release_groups, note


def best_release_group(title, release_groups, year=None):
    """(release group, similarity) closest to the album title, or (None, best score)."""
    best, best_key = None, None
    for group in release_groups:
        score = similarity(title, group.get("title", ""))
        group_year = year_of(group.get("first-release-date"))
        closeness = -abs(group_year - year) if year and group_year else -100
        is_album = 1 if group.get("primary-type") == "Album" else 0
        plain = 0 if group.get("secondary-types") else 1  # a studio album before its live/compilation namesakes
        key = (round(score, 3), is_album, plain, closeness)
        if best_key is None or key > best_key:
            best, best_key = group, key
    if best is None or best_key[0] < TITLE_THRESHOLD:
        return None, (best_key[0] if best_key else 0.0)
    return best, best_key[0]


def _store_artist_not_found(profile, note):
    profile.status = EnrichmentStatus.NOT_FOUND
    profile.note = (note or "")[:300]
    profile.enriched_at = timezone.now()
    profile.save()
    ArtistFacet.objects.filter(artist=profile.artist, source=SOURCE).delete()


def _store_artist(profile, artist, data, score, note, release_groups, albums):
    life_span = data.get("life-span") or {}
    rating, votes = _rating(data)
    profile.source = SOURCE
    profile.external_id = data["id"]
    profile.status = EnrichmentStatus.MATCHED
    profile.match_score = score
    profile.name = data.get("name", "")[:200]
    profile.disambiguation = (data.get("disambiguation") or "")[:200]
    profile.artist_type = data.get("type") or ""
    profile.country = ((data.get("area") or {}).get("name") or "")[:100]
    profile.country_code = (data.get("country") or "")[:2]
    profile.begin_area = ((data.get("begin-area") or {}).get("name") or "")[:100]
    profile.begin_year = year_of(life_span.get("begin"))
    profile.end_year = year_of(life_span.get("end")) if life_span.get("ended") else None
    profile.rating, profile.rating_votes = rating, votes
    profile.lineup = _lineup(data)
    profile.discography = _discography(release_groups, albums)
    profile.studio_album_count = sum(1 for entry in profile.discography if entry["type"] == "Album")
    profile.note = (note or "")[:300]
    profile.enriched_at = timezone.now()
    profile.save()

    ArtistFacet.objects.filter(artist=artist, source=SOURCE).delete()
    links = []
    for name, count in _top_genres(data.get("genres")):
        links.append((FacetKind.GENRE, name, "", "", count))
    instruments = {}
    for member in profile.lineup:
        years = _years_label(member["begin"], member["end"], member["current"])
        detail = " · ".join(part for part in (", ".join(member["instruments"]), years) if part)
        links.append((FacetKind.MEMBER, member["name"], member["mbid"], detail, 0))
        for instrument in member["instruments"]:
            instruments.setdefault(instrument, []).append(member["name"])
    for instrument, people in instruments.items():
        links.append((FacetKind.INSTRUMENT, instrument, "", ", ".join(people), len(people)))
    link_facets(ArtistFacet, "artist", artist, links)


def _lineup(data):
    members = {}
    for relation in data.get("relations") or []:
        if relation.get("type") != "member of band" or relation.get("direction") != "backward":
            continue
        person = relation.get("artist") or {}
        attributes = relation.get("attributes") or []
        entry = members.setdefault(person.get("id") or person.get("name"), {
            "name": person.get("name", ""),
            "mbid": person.get("id", ""),
            "instruments": [],
            "begin": None,
            "end": None,
            "current": False,
            "original": False,
        })
        for attribute in attributes:
            if attribute in NOT_INSTRUMENTS:
                entry["original"] |= attribute in ("original", "founder")
                continue
            instrument = _clean_instrument(attribute)
            if instrument and instrument not in entry["instruments"]:
                entry["instruments"].append(instrument)
        begin, end = year_of(relation.get("begin")), year_of(relation.get("end"))
        if begin and (entry["begin"] is None or begin < entry["begin"]):
            entry["begin"] = begin
        if not relation.get("ended"):
            entry["current"] = True
        elif end and (entry["end"] is None or end > entry["end"]):
            entry["end"] = end
    lineup = list(members.values())
    for entry in lineup:
        if entry["current"]:
            entry["end"] = None
    return sorted(lineup, key=lambda entry: (entry["begin"] or 9999, entry["name"].casefold()))


def _years_label(begin, end, current):
    if not begin and not end:
        return "present" if current else ""
    return f"{begin or '?'}–{'present' if current else (end or '?')}"


def _discography(release_groups, albums):
    entries = []
    for group in release_groups:
        if group.get("primary-type") not in ("Album", "EP") or group.get("secondary-types"):
            continue
        rating, votes = _rating(group)
        entries.append({
            "mbid": group["id"],
            "title": group.get("title", ""),
            "type": group["primary-type"],
            "date": group.get("first-release-date") or "",
            "year": year_of(group.get("first-release-date")),
            "rating": float(rating) if rating is not None else None,
            "votes": votes,
            "album_id": None,
        })
    groups = [{"id": e["mbid"], "title": e["title"], "primary-type": e["type"], "first-release-date": e["date"]}
              for e in entries]
    by_id = {entry["mbid"]: entry for entry in entries}
    for album in albums:
        match, _ = best_release_group(album.album_name, groups, album.album_release_year)
        if match:
            by_id[match["id"]]["album_id"] = album.pk
    return sorted(entries, key=lambda entry: (entry["date"] or "9999", entry["title"].casefold()))


def _top_genres(genres):
    genres = sorted(genres or [], key=lambda genre: genre.get("count", 0), reverse=True)
    if not genres:
        return []
    floor = max(1, genres[0].get("count", 0) * 0.25)
    return [(genre["name"], genre.get("count", 0)) for genre in genres[:MAX_GENRES] if genre.get("count", 0) >= floor]


def link_facets(link_model, owner_field, owner, links, source=SOURCE):
    """Create facet links; one per (kind, name), joining details of repeats."""
    merged = {}
    for kind, name, external_id, detail, weight in links:
        name = (name or "").strip()[:200]
        if not name:
            continue
        key = (kind, name.casefold())
        entry = merged.setdefault(key, {"kind": kind, "name": name, "external_id": external_id, "details": [], "weight": 0})
        if detail and detail not in entry["details"]:
            entry["details"].append(detail)
        entry["weight"] = max(entry["weight"], weight)
    for entry in merged.values():
        facet = Facet.objects.filter(kind=entry["kind"], name__iexact=entry["name"]).first()
        if facet is None:
            facet = Facet.objects.create(kind=entry["kind"], name=entry["name"], external_id=entry["external_id"] or "")
        link_model.objects.create(**{
            owner_field: owner,
            "facet": facet,
            "source": source,
            "detail": "; ".join(entry["details"])[:300],
            "weight": entry["weight"],
        })


# -- albums --------------------------------------------------------------------

def enrich_album(client, album, release_groups):
    profile, _ = AlbumProfile.objects.get_or_create(album=album)
    if profile.id_set_manually and profile.external_id:
        group = client.release_group(profile.external_id)
        score = 1.0
    else:
        group, score = best_release_group(album.album_name, release_groups, album.album_release_year)
    if not group:
        profile.status = EnrichmentStatus.NOT_FOUND
        profile.note = "No release in the artist's MusicBrainz discography has this title"
        profile.match_score = Decimal(str(round(score, 2))) if score else None
        profile.enriched_at = timezone.now()
        profile.save()
        AlbumFacet.objects.filter(album=album, source=SOURCE).delete()
        inherit_artist_tags(album)
        return False

    release = _richest_release(client, group["id"])
    credits = _credits(release) if release else []
    rating, votes = _rating(group)
    with transaction.atomic():
        profile.source = SOURCE
        profile.external_id = group["id"]
        profile.release_id = (release or {}).get("id", "")
        profile.status = EnrichmentStatus.MATCHED
        profile.match_score = Decimal(str(round(score, 2)))
        profile.note = "" if release else "Matched, but no official release with credits was found"
        profile.title = group.get("title", "")[:300]
        profile.primary_type = group.get("primary-type") or ""
        profile.secondary_types = ", ".join(group.get("secondary-types") or [])[:200]
        profile.first_release_date = (group.get("first-release-date") or "")[:10]
        profile.release_year = year_of(profile.first_release_date)
        profile.rating, profile.rating_votes = rating, votes
        profile.credits = credits
        profile.enriched_at = timezone.now()
        profile.save()

        AlbumFacet.objects.filter(album=album, source=SOURCE).delete()
        genres = Counter()
        for genre in (group.get("genres") or []) + ((release or {}).get("genres") or []):
            genres[genre["name"]] += genre.get("count", 0)
        links = [(FacetKind.GENRE, name, "", "", count)
                 for name, count in _top_genres([{"name": n, "count": c} for n, c in genres.items()])]
        links += [(credit["kind"], credit["name"], credit.get("mbid", ""), credit["role"], credit.get("count", 0)) for credit in credits]
        link_facets(AlbumFacet, "album", album, links)
    inherit_artist_tags(album)
    return True


INHERITED = "from artist"


def inherit_artist_tags(album):
    """Copy the tags an album shares with its artist (genres, country) onto the album itself.

    No network: it reads what the artist already has. An album's own genres are never
    replaced; genres copied earlier are refreshed when the artist's change.
    """
    artist = album.artist
    artist_profile = ArtistProfile.objects.filter(artist=artist, status=EnrichmentStatus.MATCHED).first()
    country = (artist_profile.country if artist_profile else "") or artist.artist_country
    genres = list(
        ArtistFacet.objects.filter(artist=artist, source=SOURCE, facet__kind=FacetKind.GENRE)
        .select_related("facet").order_by("-weight", "facet__name")
    )
    profile, _ = AlbumProfile.objects.get_or_create(album=album)
    if profile.country != (country or ""):
        profile.country = country or ""
        profile.save(update_fields=["country"])

    own = AlbumFacet.objects.filter(album=album, facet__kind=FacetKind.GENRE).exclude(detail=INHERITED)
    if not own.exists():
        AlbumFacet.objects.filter(album=album, facet__kind=FacetKind.GENRE, detail=INHERITED).delete()
        link_facets(AlbumFacet, "album", album, [
            (FacetKind.GENRE, link.facet.name, "", INHERITED, link.weight) for link in genres
        ])
        if genres and not album.album_genre:
            names = []
            for link in genres:
                if len(", ".join(names + [link.facet.name])) > 50:
                    break
                names.append(link.facet.name)
            if names:
                Album.objects.filter(pk=album.pk).update(album_genre=", ".join(names))


def _richest_release(client, release_group_id, tries=3):
    """The earliest official release that carries credits (falls back to the earliest)."""
    releases = client.releases_of_group(release_group_id)
    releases.sort(key=lambda release: (not release.get("date"), release.get("date") or "", release.get("country") != "XW"))
    first = None
    for summary in releases[:tries]:
        release = client.release(summary["id"])
        if release is None:
            continue
        first = first or release
        if _credits(release):
            return release
    return first


def _credits(release):
    relations = list(release.get("relations") or [])
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            relations.extend((track.get("recording") or {}).get("relations") or [])

    credits = {}

    def add(kind, name, role, mbid="", **extra):
        key = (kind, name.casefold())
        entry = credits.setdefault(key, {"kind": str(kind), "name": name, "mbid": mbid, "roles": []})
        if role and role not in entry["roles"]:
            entry["roles"].append(role)
        for field_name, value in extra.items():
            entry[field_name] = value or entry.get(field_name, 0 if field_name == "count" else "")

    for relation in relations:
        relation_type = relation.get("type", "")
        attributes = [a for a in relation.get("attributes") or [] if a not in NOT_INSTRUMENTS]
        if relation.get("target-type") == "place" and relation_type in PLACE_CREDIT_TYPES:
            place = relation.get("place") or {}
            add(FacetKind.STUDIO, place.get("name", ""), PLACE_CREDIT_TYPES[relation_type], place.get("id", ""),
                area=(place.get("area") or {}).get("name", ""))
        elif relation.get("target-type") == "artist" and relation_type in ARTIST_CREDIT_TYPES:
            person = relation.get("artist") or {}
            kind, role = ARTIST_CREDIT_TYPES[relation_type]
            if kind == FacetKind.MUSICIAN:
                # An "instrument" credit without a named instrument gets no role text.
                for attribute in attributes or ([role] if relation_type != "instrument" else [""]):
                    add(kind, person.get("name", ""), _clean_instrument(attribute), person.get("id", ""))
            else:
                add(kind, person.get("name", ""), role, person.get("id", ""))
    # Writers sit on the works the recordings perform; count the works each person wrote.
    written = {FacetKind.COMPOSER: Counter(), FacetKind.LYRICIST: Counter()}
    names = {}
    seen_works = set()
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            for relation in (track.get("recording") or {}).get("relations") or []:
                work = relation.get("work") if relation.get("target-type") == "work" else None
                if not work or work.get("id") in seen_works:
                    continue
                seen_works.add(work.get("id"))
                for part in work.get("relations") or []:
                    person = part.get("artist") or {}
                    if part.get("target-type") != "artist" or not person.get("name"):
                        continue
                    kinds = {"composer": [FacetKind.COMPOSER], "lyricist": [FacetKind.LYRICIST],
                             "writer": [FacetKind.COMPOSER, FacetKind.LYRICIST]}.get(part.get("type"), [])
                    for kind in kinds:
                        written[kind][person["name"]] += 1
                        names[person["name"]] = person.get("id", "")
    for kind, counter in written.items():
        for name, count in counter.items():
            add(kind, name, kind.label.lower(), names[name], count=count)

    for label_info in release.get("label-info") or []:
        label = label_info.get("label") or {}
        if label.get("name"):
            add(FacetKind.LABEL, label["name"], label_info.get("catalog-number") or "", label.get("id", ""))

    order = [kind.value for kind in FacetKind]
    return [
        {"kind": entry["kind"], "name": entry["name"], "mbid": entry["mbid"], "role": ", ".join(entry["roles"]),
         "area": entry.get("area", ""), "count": entry.get("count", 0)}
        for entry in sorted(credits.values(), key=lambda e: (order.index(e["kind"]), e["name"].casefold()))
        if entry["name"]
    ]


# -- manual corrections --------------------------------------------------------

def set_artist_id(artist, text):
    mbid = parse_mbid(text)
    if not mbid:
        raise ValueError("Paste a MusicBrainz artist link or ID.")
    ArtistProfile.objects.update_or_create(
        artist=artist,
        defaults={"external_id": mbid, "id_set_manually": True, "status": EnrichmentStatus.PENDING, "note": ""},
    )
    return mbid


def set_album_id(album, text):
    mbid = parse_mbid(text)
    if not mbid:
        raise ValueError("Paste a MusicBrainz release-group link or ID.")
    AlbumProfile.objects.update_or_create(
        album=album,
        defaults={"external_id": mbid, "id_set_manually": True, "status": EnrichmentStatus.PENDING, "note": ""},
    )
    return mbid
