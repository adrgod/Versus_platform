"""Lookup on Metal Archives (https://www.metal-archives.com): country, formation year, genre, label, release year and cover.

Metal Archives has no official API. This reads the same public pages and search endpoints the
site's own pages use, so it is deliberately gentle: it only runs when asked (a button, never in
the background), sends one request every two seconds with a descriptive User-Agent, and keeps
every answer on disk (``db/metal_archives_cache``) so nothing is requested twice. If the site
changes its pages or Cloudflare blocks the request, the lookup reports that and stops for that item.

Like every lookup, it only fills gaps: tags and MusicBrainz win (see services/resolver.py), and
an album's existing cover is never replaced.
"""
import html as html_lib
import json
import re
import urllib.error
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from library.models import (
    Album, Artist, ArtistFacet, EnrichmentStatus, FacetKind, MetalArchivesAlbum, MetalArchivesArtist,
)
from library.services import resolver
from library.services.http_cache import CachedJsonClient, ServiceError
from library.services.images import download_image

SOURCE = "metal-archives"
BASE_URL = "https://www.metal-archives.com/"
NAME_THRESHOLD = 0.85
TITLE_THRESHOLD = 0.85
MAX_PROBES = 6  # albums searched to work out which band of that name is the right one
MAX_GENRES = 4


class MetalArchivesError(ServiceError):
    pass


class MetalArchivesClient(CachedJsonClient):
    base_url = BASE_URL
    service_name = "Metal Archives"
    min_interval = 2.0
    cache_name = "metal_archives_cache"
    accept = "text/html,application/json;q=0.9"
    error_class = MetalArchivesError

    def _decode(self, raw):
        text = raw.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except ValueError:
            return {"html": text}

    # -- endpoints -----------------------------------------------------------
    def search_bands(self, name):
        data = self.get("search/ajax-band-search/", field="name", query=name) or {}
        return parse_band_rows(data.get("aaData", []))

    def search_albums(self, band_name, title):
        data = self.get(
            "search/ajax-advanced/searching/albums/",
            bandName=band_name, releaseTitle=title, exactBandMatch=1, exactReleaseMatch=0,
        ) or {}
        return parse_album_rows(data.get("aaData", []))

    def page(self, url):
        """The HTML of a band or album page on metal-archives.com (None when it does not exist)."""
        if not url.startswith(BASE_URL):
            raise MetalArchivesError(f"Not a Metal Archives address: {url}")
        data = self.get(url[len(BASE_URL):]) or {}
        return data.get("html")

    def cover(self, image_url, referer):
        self._wait_turn()
        try:
            return download_image(image_url, self.user_agent, referer=referer)
        except (urllib.error.URLError, TimeoutError) as error:
            raise MetalArchivesError(f"Could not fetch the cover from Metal Archives: {error}") from error


# -- reading the pages -----------------------------------------------------------

def _text(fragment):
    """Visible text of an HTML fragment, whitespace collapsed."""
    fragment = re.sub(r"<!--.*?-->", "", fragment or "", flags=re.S)
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _link(fragment):
    match = re.search(r'href="([^"]+)"', fragment or "")
    return match.group(1) if match else ""


def _entity_id(url):
    match = re.search(r"/(?:bands/[^/]+|albums/[^/]+/[^/]+)/(\d+)", url or "")
    return int(match.group(1)) if match else None


def parse_band_rows(rows):
    """Band search rows -> [{"id", "name", "url", "genre", "country"}]."""
    result = []
    for row in rows:
        if len(row) < 3 or _entity_id(_link(row[0])) is None:
            continue
        url = _link(row[0])
        result.append({"id": _entity_id(url), "name": _text(row[0]), "url": url,
                       "genre": _text(row[1]), "country": _text(row[2])})
    return result


def parse_album_rows(rows):
    """Album search rows -> [{"band_id", "band_name", "band_url", "album_id", "album_url", "title", "type"}]."""
    result = []
    for row in rows:
        if len(row) < 3:
            continue
        band_url, album_url = _link(row[0]), _link(row[1])
        if _entity_id(band_url) is None or _entity_id(album_url) is None:
            continue
        result.append({
            "band_id": _entity_id(band_url), "band_name": _text(row[0]), "band_url": band_url,
            "album_id": _entity_id(album_url), "album_url": album_url,
            "title": _text(row[1]), "type": _text(row[2]),
        })
    return result


def _definitions(html_text):
    """Every <dt>Label:</dt><dd>value</dd> pair on the page -> {"label": "value"} (first one wins)."""
    pairs = {}
    for label, value in re.findall(r"<dt>\s*(.*?):?\s*</dt>\s*<dd[^>]*>(.*?)</dd>", html_text or "", flags=re.S):
        pairs.setdefault(_text(label).rstrip(":").casefold(), _text(value))
    return pairs


def _year(text):
    match = re.search(r"\b(1[89]\d\d|20\d\d)\b", text or "")
    return int(match.group(1)) if match else None


def parse_band_page(html_text):
    pairs = _definitions(html_text)
    label = pairs.get("current label") or pairs.get("last known label") or ""
    return {
        "country": pairs.get("country of origin", ""),
        "location": pairs.get("location", ""),
        "formed_year": _year(pairs.get("formed in", "")),
        "band_status": pairs.get("status", ""),
        "genre_text": pairs.get("genre", ""),
        "label": "" if label.casefold() in ("unsigned/independent", "unsigned") else label,
    }


def parse_album_page(html_text):
    pairs = _definitions(html_text)
    cover = re.search(r'id="cover"[^>]*?href="([^"]+)"', html_text or "")
    label = pairs.get("label", "")
    return {
        "release_type": pairs.get("type", ""),
        "release_date": pairs.get("release date", ""),
        "release_year": _year(pairs.get("release date", "")),
        "label": "" if label.casefold() in ("independent", "unsigned/independent") else label,
        "cover_url": cover.group(1) if cover else "",
    }


def split_genres(text):
    """"Thrash Metal (early); Hard Rock (mid)" -> ["Thrash Metal", "Hard Rock"]."""
    names = []
    for part in (text or "").split(";"):
        name = re.sub(r"\s*\([^)]*\)", "", part).strip()
        if name and name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return names[:MAX_GENRES]


# -- matching --------------------------------------------------------------------

def _similarity():
    from library.services.enrichment import similarity
    return similarity


def _query_title(title):
    """The album title as Metal Archives would list it: without "(Remastered)", "[Deluxe]" and the like."""
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip() or (title or "")


def match_band(client, artist, albums):
    """(band dict, note) of the Metal Archives band this artist is, or (None, why not).

    Bands share names a lot on Metal Archives, so the artist's own albums decide: the band that has
    the most of them wins. Only when no album matches does the band name alone decide, and then
    only if exactly one band carries it.
    """
    similarity = _similarity()
    votes, bands = Counter(), {}
    for album in albums[:MAX_PROBES]:
        for row in client.search_albums(artist.artist_name, _query_title(album.album_name)):
            if (similarity(row["title"], album.album_name) >= TITLE_THRESHOLD
                    and similarity(row["band_name"], artist.artist_name) >= NAME_THRESHOLD):
                votes[row["band_id"]] += 1
                bands[row["band_id"]] = {"id": row["band_id"], "name": row["band_name"], "url": row["band_url"]}
    if votes:
        best = votes.most_common(1)[0][0]
        return bands[best], ""

    named = [band for band in client.search_bands(artist.artist_name)
             if similarity(band["name"], artist.artist_name) >= NAME_THRESHOLD]
    if len(named) == 1:
        return named[0], ""
    if not named:
        return None, f'No Metal Archives band is named like "{artist.artist_name}"'
    return None, (f'{len(named)} Metal Archives bands are named "{artist.artist_name}" and none of '
                  "this artist's albums were found on them")


def match_album(client, band, album):
    """The best Metal Archives release of ``band`` titled like ``album``, or None (full-lengths first)."""
    similarity = _similarity()
    rows = [row for row in client.search_albums(band["name"], _query_title(album.album_name))
            if row["band_id"] == band["id"] and similarity(row["title"], album.album_name) >= TITLE_THRESHOLD]
    if not rows:
        return None
    rows.sort(key=lambda row: (row["type"] != "Full-length", -similarity(row["title"], album.album_name)))
    return rows[0]


# -- storing ---------------------------------------------------------------------

@dataclass
class MetalStats:
    artists_total: int = 0
    artists_done: int = 0
    artists_matched: int = 0
    artists_not_found: int = 0
    albums_matched: int = 0
    albums_not_found: int = 0
    covers_added: int = 0
    errors: list = field(default_factory=list)
    current: str = ""


def artists_to_look_up(refresh=False, artist_ids=None):
    queryset = Artist.objects.order_by("artist_name")
    if artist_ids is not None:
        return list(queryset.filter(pk__in=artist_ids))
    if refresh:
        return list(queryset)
    done = [EnrichmentStatus.MATCHED, EnrichmentStatus.NOT_FOUND]
    return list(queryset.exclude(metal_archives__status__in=done))


def run(*, client=None, refresh=False, artist_ids=None, album_ids=None, progress=None, should_stop=None):
    """Lookup on Metal Archives for the library, some artists, or only some albums."""
    client = client or MetalArchivesClient()
    if album_ids:
        artist_ids = sorted(set(Album.objects.filter(pk__in=album_ids).values_list("artist_id", flat=True)))
    artists = artists_to_look_up(refresh=refresh, artist_ids=artist_ids)
    stats = MetalStats(artists_total=len(artists))
    blocked_in_a_row = 0
    for artist in artists:
        if should_stop and should_stop():
            break
        if blocked_in_a_row >= 3:
            stats.errors.append("Stopped: Metal Archives keeps refusing requests (403), so it is blocking this client.")
            break
        stats.current = artist.artist_name
        if progress:
            progress(stats)
        try:
            look_up_artist(client, artist, stats=stats,
                           refresh=refresh or artist_ids is not None, only_album_ids=album_ids)
        except Exception as error:  # one band's problem (or a blocked request) must not end the run
            message = str(error) if isinstance(error, MetalArchivesError) else f"{type(error).__name__}: {error}"
            stats.errors.append(f"{artist.artist_name}: {message}")
            blocked_in_a_row = blocked_in_a_row + 1 if " 403 " in message or "answered 403" in message else 0
            MetalArchivesArtist.objects.update_or_create(
                artist=artist, defaults={"status": EnrichmentStatus.ERROR, "note": message[:300]})
        else:
            blocked_in_a_row = 0
        stats.artists_done += 1
    stats.current = ""
    if progress:
        progress(stats)
    return stats


def look_up_artist(client, artist, *, stats=None, refresh=True, only_album_ids=None):
    try:
        return _look_up_artist(client, artist, stats=stats or MetalStats(), refresh=refresh, only_album_ids=only_album_ids)
    finally:
        resolver.resolve_for_artist(artist)


def _look_up_artist(client, artist, *, stats, refresh, only_album_ids):
    entry, _ = MetalArchivesArtist.objects.get_or_create(artist=artist)
    albums = list(artist.album_set.all())
    band, note = match_band(client, artist, albums)
    now = timezone.now()
    if band is None:
        entry.status, entry.note, entry.enriched_at = EnrichmentStatus.NOT_FOUND, note[:300], now
        entry.save()
        stats.artists_not_found += 1
        return entry
    page = client.page(band["url"])
    if page is None:
        entry.status, entry.note, entry.enriched_at = EnrichmentStatus.NOT_FOUND, "The band page no longer exists", now
        entry.save()
        stats.artists_not_found += 1
        return entry
    info = parse_band_page(page)
    with transaction.atomic():
        entry.ma_id, entry.url, entry.name = band["id"], band["url"], band["name"]
        entry.status, entry.note, entry.enriched_at = EnrichmentStatus.MATCHED, "", now
        for name in ("country", "location", "formed_year", "band_status", "genre_text", "label"):
            setattr(entry, name, info[name])
        entry.save()
        _store_genres(artist, info["genre_text"])
    stats.artists_matched += 1

    for album in albums:
        if only_album_ids and album.pk not in only_album_ids:
            continue
        existing = MetalArchivesAlbum.objects.filter(album=album).first()
        if (not refresh and not only_album_ids and existing
                and existing.status in (EnrichmentStatus.MATCHED, EnrichmentStatus.NOT_FOUND)):
            continue
        try:
            found = _look_up_album(client, band, album, stats)
        except Exception as error:
            message = str(error) if isinstance(error, MetalArchivesError) else f"{type(error).__name__}: {error}"
            stats.errors.append(f"{artist.artist_name} – {album.album_name}: {message}")
            MetalArchivesAlbum.objects.update_or_create(
                album=album, defaults={"status": EnrichmentStatus.ERROR, "note": message[:300]})
            continue
        if found:
            stats.albums_matched += 1
        else:
            stats.albums_not_found += 1
    return entry


def _store_genres(artist, genre_text):
    from library.services.enrichment import link_facets

    ArtistFacet.objects.filter(artist=artist, source=SOURCE).delete()
    names = split_genres(genre_text)
    link_facets(ArtistFacet, "artist", artist,
                [(FacetKind.GENRE, name, "", "", 100 - 10 * index) for index, name in enumerate(names)],
                source=SOURCE)


def _look_up_album(client, band, album, stats):
    entry, _ = MetalArchivesAlbum.objects.get_or_create(album=album)
    row = match_album(client, band, album)
    now = timezone.now()
    if row is None:
        entry.status, entry.note, entry.enriched_at = EnrichmentStatus.NOT_FOUND, "Not among this band's releases", now
        entry.save()
        return False
    page = client.page(row["album_url"])
    if page is None:
        entry.status, entry.note, entry.enriched_at = EnrichmentStatus.NOT_FOUND, "The album page no longer exists", now
        entry.save()
        return False
    info = parse_album_page(page)
    entry.ma_id, entry.url, entry.title = row["album_id"], row["album_url"], row["title"][:300]
    entry.release_type, entry.release_date = info["release_type"][:30], info["release_date"][:40]
    entry.release_year, entry.label = info["release_year"], info["label"][:100]
    entry.status, entry.note, entry.enriched_at = EnrichmentStatus.MATCHED, "", now
    entry.save()
    if info["cover_url"] and _album_lacks_cover(album):
        try:
            image = client.cover(info["cover_url"], referer=row["album_url"])
        except MetalArchivesError as error:
            stats.errors.append(f"{album.album_name}: {error}")
            image = None
        if image:
            Album.objects.filter(pk=album.pk).update(album_cover=image)
            album.album_cover = image
            stats.covers_added += 1
    return True


def _album_lacks_cover(album):
    return not Album.objects.filter(pk=album.pk).exclude(album_cover__isnull=True).exclude(album_cover=b"").exists()
