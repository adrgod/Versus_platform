"""Keep an album's or artist's details when changed tags turn it into a new catalogue entry.

Artist name, album name, year and publisher are part of an entry's identity, so
fixing one of them in the MP3 tags makes the importer create a *new* artist,
album and track row. Everything the old rows had (Lookup Details Online results,
Enrich Data with AI results, liner notes, lyrics, cover, batch, first-loaded date)
is carried over to the new rows, the old track rows are removed, and old albums
or artists left without tracks are pruned.

Rules: old rows are matched to new ones by file path. Carrying over only fills
gaps: whatever the new entry already has (its own tags, its own matched lookup)
wins, except that a matched online/AI result replaces an unmatched or failed one.
"""
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Min

from library.models import (
    Album, AlbumDocument, AlbumFacet, AlbumInsight, AlbumProfile, Artist, ArtistFacet, ArtistProfile,
    EnrichmentStatus, Track, TrackLyrics,
)
from library.services.importer import DELETE_CHUNK, import_records, is_blank, prune_empty

# Album and artist fields that come from tags or the path but are not part of the identity.
ALBUM_FIELDS = ("album_cover", "album_batch_date", "album_genre", "album_length_class")
ARTIST_FIELDS = ("artist_photo",)


@dataclass
class CarryOver:
    tracks_replaced: int = 0
    albums: int = 0
    artists: int = 0


def import_and_carry_over(records, *, on_skip=None, touched=None):
    """``import_records`` for tracks, then carry details from replaced rows to their new rows.

    Returns ``(stats, CarryOver)``. Call inside a transaction so a failure undoes both.
    """
    paths = sorted({str(record["file_path"]) for record in records if record.get("file_path")})
    before = _tracks_by_path(paths)
    touched = set() if touched is None else touched
    stats = import_records(records, on_skip=on_skip, touched=touched)

    now = {
        path: (pk, album_id, artist_id)
        for pk, path, album_id, artist_id in Track.objects.filter(pk__in=touched)
        .values_list("pk", "track_file_path", "album_id", "album__artist_id")
    }
    summary = CarryOver()
    replaced, album_pairs, artist_pairs = [], set(), set()
    for path, old_rows in before.items():
        if path not in now:
            continue  # not imported this time (unreadable, skipped): leave it alone
        new_track, new_album, new_artist = now[path]
        for old_track, old_album, old_artist in old_rows:
            if old_track == new_track:
                continue
            _carry_track(old_track, new_track)
            replaced.append(old_track)
            if old_album != new_album:
                album_pairs.add((old_album, new_album))
            if old_artist != new_artist:
                artist_pairs.add((old_artist, new_artist))

    for old, new in sorted(artist_pairs):
        _carry_artist(old, new)
    for old, new in sorted(album_pairs):
        _carry_album(old, new)
    for start in range(0, len(replaced), DELETE_CHUNK):
        Track.objects.filter(pk__in=replaced[start:start + DELETE_CHUNK]).delete()
    if replaced:
        prune_empty()

    # The enrichment now sits on the new rows: rebuild what the pages show from tags + enrichment.
    from library.services import resolver
    resolver.resolve_for_tracks(touched)

    summary.tracks_replaced = len(replaced)
    summary.albums = len({new for _, new in album_pairs})
    summary.artists = len({new for _, new in artist_pairs})
    return stats, summary


def _tracks_by_path(paths):
    rows = defaultdict(list)
    for start in range(0, len(paths), DELETE_CHUNK):
        for pk, path, album_id, artist_id in (
            Track.objects.filter(track_file_path__in=paths[start:start + DELETE_CHUNK])
            .values_list("pk", "track_file_path", "album_id", "album__artist_id")
        ):
            rows[path].append((pk, album_id, artist_id))
    return rows


def _clone(obj, **changes):
    obj.pk = None
    obj.id = None
    obj._state.adding = True
    for name, value in changes.items():
        setattr(obj, name, value)
    obj.save()
    return obj


def _plain(value):
    return bytes(value) if isinstance(value, memoryview) else value


def _better(old, new):
    """True when ``old`` should replace ``new`` (one-to-one online/AI results)."""
    if old is None:
        return False
    if new is None:
        return True
    return old.status == EnrichmentStatus.MATCHED and new.status != EnrichmentStatus.MATCHED


def _fill_fields(model, old_pk, new_pk, fields):
    """Copy non-blank values into blank fields of the new row and keep the earliest first-loaded date."""
    old = model.objects.filter(pk=old_pk).first()
    new = model.objects.filter(pk=new_pk).first()
    if old is None or new is None:
        return
    changes = {}
    for name in fields:
        old_value, new_value = _plain(getattr(old, name)), _plain(getattr(new, name))
        if is_blank(new_value) and not is_blank(old_value):
            changes[name] = old_value
    earliest = model.objects.filter(pk__in=[old_pk, new_pk]).aggregate(first=Min("when_loaded"))["first"]
    if earliest and earliest != new.when_loaded:
        changes["when_loaded"] = earliest  # update() leaves auto_now_add alone
    if changes:
        model.objects.filter(pk=new_pk).update(**changes)


def _carry_one_to_one(model, owner, old_pk, new_pk):
    old = model.objects.filter(**{f"{owner}_id": old_pk}).first()
    new = model.objects.filter(**{f"{owner}_id": new_pk}).first()
    if not _better(old, new):
        return
    if new is not None:
        new.delete()
    _clone(old, **{f"{owner}_id": new_pk})


def _carry_links(model, owner, old_pk, new_pk, unique_fields):
    existing = set(model.objects.filter(**{f"{owner}_id": new_pk}).values_list(*unique_fields))
    for link in model.objects.filter(**{f"{owner}_id": old_pk}):
        key = tuple(getattr(link, name) for name in unique_fields)
        if key not in existing:
            _clone(link, **{f"{owner}_id": new_pk})
            existing.add(key)


def _carry_track(old_pk, new_pk):
    lyrics = TrackLyrics.objects.filter(track_id=old_pk).first()
    new_lyrics = TrackLyrics.objects.filter(track_id=new_pk).first()
    if lyrics and (new_lyrics is None or (not new_lyrics.text and lyrics.text)):
        if new_lyrics is not None:
            new_lyrics.delete()
        lyrics.track_id = new_pk  # the old track is removed afterwards, so the row can move
        lyrics.save()


def _carry_album(old_pk, new_pk):
    _fill_fields(Album, old_pk, new_pk, ALBUM_FIELDS)
    _carry_one_to_one(AlbumProfile, "album", old_pk, new_pk)
    _carry_one_to_one(AlbumInsight, "album", old_pk, new_pk)
    _carry_links(AlbumFacet, "album", old_pk, new_pk, ("facet_id", "source"))
    _carry_links(AlbumDocument, "album", old_pk, new_pk, ("file_path",))


def _carry_artist(old_pk, new_pk):
    _fill_fields(Artist, old_pk, new_pk, ARTIST_FIELDS)
    _carry_one_to_one(ArtistProfile, "artist", old_pk, new_pk)
    _carry_links(ArtistFacet, "artist", old_pk, new_pk, ("facet_id", "source"))
