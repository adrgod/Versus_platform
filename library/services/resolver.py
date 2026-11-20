"""One final value per field, kept in the database, with where it came from.

File tags are what the files say; lookups (MusicBrainz), liner notes and file names
only ever *fill gaps*:  tags  >  MusicBrainz  >  liner notes  >  file name.
The results are stored in ``resolved_*`` columns next to ``resolved_sources``
({"year": "musicbrainz", ...}) and every page reads only those. Nothing here
writes to the identity columns (artist_country, album_release_year, ...) or to files.

Run it after anything that changes tags or enrichment (see the callers): it is cheap,
idempotent and works on a subset of albums or artists.
"""
from library.models import (
    Album, AlbumCredit, AlbumFacet, AlbumInsight, Artist, EnrichmentStatus, FacetKind, Track,
)
from library.services.local_credits import SOURCE_LABELS as _LABELS
from library.services.places import locate

INHERITED = "from artist"  # AlbumFacet.detail of a genre copied from the artist
SOURCE_LABELS = {**_LABELS, "filename": "File name", "city lookup": "City lookup", "metal-archives": "Metal Archives", "discogs": "Discogs"}
CREDIT_SOURCES = ("musicbrainz", "discogs", "tags", "notes")
CHUNK = 400


def source_label(source):
    """Human text for a stored source: "tags" -> "MP3 tags"; "artist:musicbrainz" -> "MusicBrainz, from the artist"."""
    if not source:
        return ""
    if source.startswith("artist:"):
        return f"{source_label(source[7:])}, from the artist"
    return SOURCE_LABELS.get(source, source)


def _chunks(ids):
    ids = sorted(ids)
    for start in range(0, len(ids), CHUNK):
        yield ids[start:start + CHUNK]


def _matched(profile):
    return profile is not None and profile.status == EnrichmentStatus.MATCHED


def _save(objects, fields):
    changed = [obj for obj in objects if obj._resolved_changed]
    if changed:
        type(changed[0]).objects.bulk_update(changed, fields, batch_size=200)
    return len(changed)


def _set(obj, **values):
    for name, value in values.items():
        if getattr(obj, name) != value:
            setattr(obj, name, value)
            obj._resolved_changed = True


# -- artists ---------------------------------------------------------------------

def resolve_artists(artist_ids=None):
    artists = Artist.objects.select_related("profile", "metal_archives")
    if artist_ids is not None:
        artists = artists.filter(pk__in=artist_ids)
    result = []
    for artist in artists:
        artist._resolved_changed = False
        profile = getattr(artist, "profile", None)
        metal = getattr(artist, "metal_archives", None)
        sources = {}
        # A tag that holds a city ("Budapest") still gets a country: from the other sources, else a
        # city lookup. The city is kept in resolved_place and shown in brackets after the country.
        others = []  # countries the lookups gave, best first
        if _matched(profile) and profile.country:
            others.append((profile.country, "musicbrainz"))
        if _matched(metal) and metal.country:
            others.append((metal.country, "metal-archives"))
        country, place = "", ""
        if artist.artist_country:
            country, place, via = locate(artist.artist_country, hint_country=others[0][0] if others else "")
            if via == "hint":
                sources["country"] = others[0][1]
            elif country:
                sources["country"] = "city lookup" if via == "city" else "tags"
            else:  # not a country nor a city we know: show it as it is, as before
                country, place, sources["country"] = artist.artist_country, "", "tags"
        elif others:
            (country, sources["country"]) = others[0]
        if artist.artist_formed_year:
            formed, sources["formed_year"] = artist.artist_formed_year, "tags"
        elif _matched(profile) and profile.begin_year:
            formed, sources["formed_year"] = profile.begin_year, "musicbrainz"
        elif _matched(metal) and metal.formed_year:
            formed, sources["formed_year"] = metal.formed_year, "metal-archives"
        else:
            formed = None
        _set(artist, resolved_country=country, resolved_place=place, resolved_formed_year=formed, resolved_sources=sources)
        result.append(artist)
    return _save(result, ["resolved_country", "resolved_place", "resolved_formed_year", "resolved_sources"])


# -- albums ----------------------------------------------------------------------

def _first_year(text):
    return int(text[:4]) if text and text[:4].isdigit() else None


def resolve_albums(album_ids=None):
    albums = Album.objects.select_related("artist", "profile", "insight", "metal_archives", "discogs")
    if album_ids is not None:
        albums = albums.filter(pk__in=album_ids)
    albums = list(albums)
    if not albums:
        return 0
    ids = {album.pk for album in albums}
    artist_ids = {album.artist_id for album in albums}

    genres, labels, credit_rows = {}, {}, {}
    for chunk in _chunks(ids):
        for album_id, kind, name, detail in (
            AlbumFacet.objects.filter(album_id__in=chunk, source="musicbrainz",
                                      facet__kind__in=[FacetKind.GENRE, FacetKind.LABEL])
            .order_by("-weight", "facet__name").values_list("album_id", "facet__kind", "facet__name", "detail")
        ):
            if kind == FacetKind.GENRE:
                genres.setdefault(album_id, []).append((name, detail == INHERITED))
            else:
                labels.setdefault(album_id, []).append(name)
        for row in AlbumCredit.objects.filter(album_id__in=chunk):
            credit_rows.setdefault(row.album_id, []).append(row)
    artist_state = {
        artist.pk: artist
        for chunk in _chunks(artist_ids)
        for artist in Artist.objects.filter(pk__in=chunk).select_related("profile")
    }
    artist_genres = {}  # artist -> (source, [genre names]); MusicBrainz's genres win over other sources'
    from library.models import ArtistFacet
    by_source = {}
    for chunk in _chunks(artist_ids):
        for artist_id, source, name in (
            ArtistFacet.objects.filter(artist_id__in=chunk, facet__kind=FacetKind.GENRE)
            .order_by("-weight", "facet__name").values_list("artist_id", "source", "facet__name")
        ):
            by_source.setdefault(artist_id, {}).setdefault(source, []).append(name)
    for artist_id, sources_found in by_source.items():
        source = "musicbrainz" if "musicbrainz" in sources_found else sorted(sources_found)[0]
        artist_genres[artist_id] = (source, sources_found[source])

    for album in albums:
        album._resolved_changed = False
        profile, insight = getattr(album, "profile", None), getattr(album, "insight", None)
        metal = getattr(album, "metal_archives", None)
        discogs = getattr(album, "discogs", None)
        artist = artist_state[album.artist_id]
        artist_profile = getattr(artist, "profile", None)
        sources = {}

        notes_year = _first_year(insight.release_date) if insight and insight.release_date else None
        if album.album_release_year:
            year, sources["year"] = album.album_release_year, "tags"
        elif _matched(profile) and profile.release_year:
            year, sources["year"] = profile.release_year, "musicbrainz"
        elif _matched(metal) and metal.release_year:
            year, sources["year"] = metal.release_year, "metal-archives"
        elif _matched(discogs) and discogs.release_year:
            year, sources["year"] = discogs.release_year, "discogs"
        elif notes_year:
            year, sources["year"] = notes_year, "notes"
        else:
            year = None

        own = [name for name, inherited in genres.get(album.pk, []) if not inherited]
        inherited_names = [name for name, inherited in genres.get(album.pk, []) if inherited]
        artist_source, artist_names = artist_genres.get(album.artist_id, ("musicbrainz", []))
        borrowed = inherited_names or artist_names
        borrowed_source = "musicbrainz" if inherited_names else artist_source
        if album.album_genre:
            genre, sources["genre"] = album.album_genre, "tags"
        elif own:
            genre, sources["genre"] = ", ".join(own), "musicbrainz"
        elif borrowed:
            genre, sources["genre"] = ", ".join(borrowed[:4]), f"artist:{borrowed_source}"
        else:
            genre = ""

        notes_label = next((row.name for row in credit_rows.get(album.pk, [])
                            if row.kind == FacetKind.LABEL and row.source == AlbumCredit.SOURCE_NOTES), "")
        if album.album_publisher:
            label, sources["label"] = album.album_publisher, "tags"
        elif labels.get(album.pk):
            label, sources["label"] = labels[album.pk][0], "musicbrainz"
        elif _matched(metal) and metal.label:
            label, sources["label"] = metal.label, "metal-archives"
        elif _matched(discogs) and discogs.label:
            label, sources["label"] = discogs.label, "discogs"
        elif notes_label:
            label, sources["label"] = notes_label, "notes"
        else:
            label = ""

        artist_source = artist.resolved_sources.get("country", "")
        country = artist.resolved_country
        if country:
            sources["country"] = f"artist:{artist_source}" if artist_source else ""

        members = [member["name"] for member in artist_profile.lineup] if _matched(artist_profile) else []
        credits = build_credits(
            profile.credits if _matched(profile) else [],
            [row for row in credit_rows.get(album.pk, []) if row.source == AlbumCredit.SOURCE_TAGS],
            [row for row in credit_rows.get(album.pk, []) if row.source == AlbumCredit.SOURCE_NOTES],
            artist.artist_name, members,
            discogs=discogs.credits if _matched(discogs) else [],
        )
        _set(album, resolved_year=year, resolved_genre=genre[:200], resolved_label=label[:100],
             resolved_country=country, resolved_credits=credits, resolved_sources=sources)
    return _save(albums, ["resolved_year", "resolved_genre", "resolved_label", "resolved_country",
                          "resolved_credits", "resolved_sources"])


CREDIT_CELLS = ("composer", "lyricist", "studio", "producer", "engineer", "artwork")
CELL_KINDS = {
    "composer": FacetKind.COMPOSER, "lyricist": FacetKind.LYRICIST, "studio": FacetKind.STUDIO,
    "producer": FacetKind.PRODUCER, "engineer": FacetKind.ENGINEER, "artwork": FacetKind.ARTWORK,
}


def build_credits(musicbrainz, tag_rows, note_rows, artist_name, members, discogs=()):
    """{"composer": {"source": "tags", "entries": [{"name", "detail", "evidence"}]}, ...}

    Each cell comes from the first source that has something for it. The main composer and
    lyricist are the person credited on the most works; one who is not in the band is shown
    as the band, since the album is credited to it.
    """
    from library.services.enrichment import NAME_THRESHOLD, similarity

    by_source = {
        "musicbrainz": [dict(c) for c in musicbrainz],
        "discogs": [dict(c) for c in discogs],
        "tags": [{"kind": r.kind, "name": r.name, "role": r.detail, "area": "", "count": r.count, "evidence": r.evidence} for r in tag_rows],
        "notes": [{"kind": r.kind, "name": r.name, "role": r.detail, "area": "", "count": r.count, "evidence": r.evidence} for r in note_rows],
    }

    def first(kind):
        for source in CREDIT_SOURCES:
            found = [c for c in by_source[source] if c["kind"] == kind]
            if found:
                return source, found
        return "", []

    result = {}
    for cell in CREDIT_CELLS:
        kind = CELL_KINDS[cell]
        source, found = first(kind)
        if not found:
            result[cell] = {"source": "", "entries": []}
            continue
        if cell in ("composer", "lyricist"):
            top = sorted(found, key=lambda c: (-c.get("count", 0), c["name"].casefold()))[0]
            is_member = any(similarity(top["name"], member) >= NAME_THRESHOLD for member in members)
            entries = [{"name": top["name"] if is_member else artist_name, "detail": "", "evidence": top.get("evidence", "")}]
        elif cell == "studio":
            recorded = [c for c in found if "recorded" in c.get("role", "")] or found
            entries = [{"name": recorded[0]["name"], "detail": recorded[0].get("area", ""), "evidence": recorded[0].get("evidence", "")}]
        else:
            entries = [{"name": c["name"], "detail": c.get("role", ""), "evidence": c.get("evidence", "")} for c in found]
        result[cell] = {"source": source, "entries": entries}
    return result


# -- tracks ----------------------------------------------------------------------

def resolve_tracks(album_ids=None):
    from library.services.fill_missing import track_number_from_filename

    tracks = Track.objects.all()
    if album_ids is not None:
        tracks = tracks.filter(album_id__in=album_ids)
    tracks = list(tracks)
    counts = {}
    for track in tracks:
        counts[track.album_id] = counts.get(track.album_id, 0) + 1
    for track in tracks:
        track._resolved_changed = False
        if track.track_number:
            number, sources = track.track_number, {"number": "tags"}
        else:
            number = track_number_from_filename(track.track_file_path, counts[track.album_id])
            sources = {"number": "filename"} if number else {}
        _set(track, resolved_number=number, resolved_sources=sources)
    return _save(tracks, ["resolved_number", "resolved_sources"])


# -- entry points ----------------------------------------------------------------

def resolve_all():
    resolve_artists()
    resolve_albums()
    resolve_tracks()


def resolve_for_albums(album_ids):
    """Re-resolve these albums, their artists and their tracks (after a lookup or an edit)."""
    ids = set(album_ids)
    if not ids:
        return
    if len(ids) > 3000:  # a whole-library import: one pass is cheaper than many filters
        return resolve_all()
    artist_ids = set(Album.objects.filter(pk__in=ids).values_list("artist_id", flat=True))
    resolve_artists(artist_ids)
    resolve_albums(ids)
    resolve_tracks(ids)


def resolve_for_artist(artist):
    """Re-resolve an artist and every album of it (its country and genres flow to the albums)."""
    resolve_artists([artist.pk])
    ids = list(Album.objects.filter(artist=artist).values_list("pk", flat=True))
    resolve_albums(ids)
    resolve_tracks(ids)


def resolve_for_tracks(track_ids):
    ids = sorted(set(track_ids))
    albums = set()
    for chunk in _chunks(ids):
        albums.update(Track.objects.filter(pk__in=chunk).values_list("album_id", flat=True))
    resolve_for_albums(albums)
