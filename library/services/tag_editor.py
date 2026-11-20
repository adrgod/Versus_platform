"""Edit Tags: write tags typed by hand to the MP3 files of an album or artist.

All files of one save are written together: each file's original tag block is
kept in memory, every written value is read back, and if any file fails (or the
catalogue cannot be updated afterwards) every file written so far is put back.
The catalogue is then updated from the files with ``import_and_carry_over``, so
an album or artist whose identity changed keeps its details on the new entry.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

from library import repository, scan_service
from library.services import files_scanner  # registers the publisher / artist TXXX keys
from library.services.carry_over import import_and_carry_over
from library.services.tag_writer import TagSnapshot

# Tags shown in the editors, in display order: (EasyID3 key, label).
ALBUM_FIELDS = [
    ("album", "Album"),
    ("albumartist", "Album artist"),
    ("artist", "Artist"),
    ("date", "Year"),
    ("originaldate", "Original date"),
    ("genre", "Genre"),
    ("publisher", "Label"),
    ("composer", "Composer"),
    ("lyricist", "Lyricist"),
]
TRACK_FIELDS = [
    ("discnumber", "Disc"),
    ("tracknumber", "#"),
    ("title", "Title"),
    ("artist", "Artist"),
    ("genre", "Genre"),
    ("date", "Year"),
    ("mood", "Mood"),
    ("language", "Language"),
]
ARTIST_FIELDS = [
    ("artist", "Name"),
    ("artist_country", "Country"),
    ("artist_formed_year", "Formed"),
]
# Tags that live in the album header only (written to every track of the album).
ALBUM_ONLY = [key for key, _ in ALBUM_FIELDS if key not in {key for key, _ in TRACK_FIELDS}]
EDITABLE = sorted({key for key, _ in ALBUM_FIELDS + TRACK_FIELDS + ARTIST_FIELDS})
REQUIRED = {"artist": "Artist", "album": "Album", "title": "Title"}

NUMBER = re.compile(r"^\d{1,3}(?:/\d{1,3})?$")
YEAR = re.compile(r"^\d{4}$")
DATE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")


class TagEditError(ValueError):
    """A save that could not be done; no file was left changed."""


@dataclass
class SaveResult:
    files_written: int = 0
    track_ids: set = field(default_factory=set)
    albums_carried: int = 0
    artists_carried: int = 0


def read_tags(path):
    """Current values of every editable tag of one file ("" when absent)."""
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        return {key: "" for key in EDITABLE}
    return {key: "; ".join(tags.get(key) or []) for key in EDITABLE}


def validate(key, value):
    """An error message for a bad value, else None."""
    value = value.strip()
    if key in REQUIRED and not value:
        return f"{REQUIRED[key]} cannot be empty."
    if not value:
        return None
    if key in ("tracknumber", "discnumber") and not NUMBER.match(value):
        return "Use a number, like 3 or 3/12."
    if key == "artist_formed_year" and not YEAR.match(value):
        return "Use a year, like 1994."
    if key in ("date", "originaldate") and not DATE.match(value):
        return "Use a year (1994) or a date (1994-05-17)."
    return None


def save(edits, *, reimport_paths=None):
    """Write ``edits`` ({path: {tag: value}}) and update the catalogue from the files.

    Only tags that differ from the file are written; "" removes a tag. Raises
    :class:`TagEditError` (with every file put back) on any problem.
    ``reimport_paths`` are the files read back into the catalogue afterwards
    (defaults to the edited files; pass the whole album for its length class).
    """
    for path in edits:
        if not repository.is_allowed_file(path):
            raise TagEditError(f"{Path(path).name} is missing or outside the music library.")
    # Same lock as Import Data / Read Local MP3 Tags: never write files while they are being read.
    if not scan_service._scan_lock.acquire(blocking=False):
        raise TagEditError("Import Data is running. Save again when it finishes.")
    try:
        return _save(edits, reimport_paths)
    finally:
        scan_service._scan_lock.release()


def _save(edits, reimport_paths):
    snapshots = []
    try:
        written = 0
        for path, values in edits.items():
            current = read_tags(path)
            changes = {key: value.strip() for key, value in values.items() if value.strip() != current.get(key, "")}
            if not changes:
                continue
            snapshots.append(TagSnapshot(path))
            _write(path, changes)
            written += 1

        result = SaveResult(files_written=written)
        if not written:
            return result

        records = files_scanner.read_metadata_for_files(sorted(reimport_paths or edits), repository.get_config())
        with transaction.atomic():
            stats, carried = import_and_carry_over(records, touched=result.track_ids)
            if stats["skipped"]:
                raise TagEditError("Artist, album and title cannot be empty.")
        result.albums_carried, result.artists_carried = carried.albums, carried.artists
        _refresh_after_save(result.track_ids)
        return result
    except Exception as error:
        for snapshot in reversed(snapshots):
            snapshot.restore()
        if isinstance(error, TagEditError):
            raise
        raise TagEditError(f"Nothing was saved; every file was put back. ({error})") from error


def _refresh_after_save(track_ids):
    """Composer / lyricist tags just written count as "MP3 tags" credits: read them, then re-resolve."""
    from library.models import Album, Track
    from library.services import local_credits, resolver

    album_ids = set(Track.objects.filter(pk__in=track_ids).values_list("album_id", flat=True))
    for album in Album.objects.filter(pk__in=album_ids):
        local_credits.refresh_tag_credits(album)
    resolver.resolve_for_albums(album_ids)


def _write(path, changes):
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        tags = EasyID3()
    for key, value in changes.items():
        if value:
            tags[key] = [value]
        elif key in tags:
            del tags[key]
    tags.save(path)
    after = read_tags(path)
    for key, value in changes.items():
        if after.get(key, "") != value:
            raise TagEditError(f"{Path(path).name}: {key} did not verify.")
