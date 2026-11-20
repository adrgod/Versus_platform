"""Import Data (whole source folder, background) and Read Local MP3 Tags (one artist or album)."""
import threading
from dataclasses import dataclass, field

from django.db import connection, transaction
from django.utils import timezone

from . import repository
from .models import LibraryState, Track
from .services.files_scanner import read_all_metadata, read_metadata_for_files
from .services.carry_over import import_and_carry_over
from .services.importer import remove_missing_tracks


_scan_lock = threading.Lock()
_scan_thread = None


def _update_scan_state(source_folder, status, *, started=None, finished=None, error=None, count=None):
    state = LibraryState.load()
    state.source_folder = str(source_folder)
    state.last_scan_status = status
    if started is not None:
        state.last_scan_started = started
    if finished is not None:
        state.last_scan_finished = finished
    state.scan_error = error
    if count is not None:
        state.scanned_track_count = count
    state.save()


def scan_library(source_folder):
    source_folder = repository.resolve_allowed_folder(source_folder)
    _update_scan_state(source_folder, "scanning", started=timezone.now(), error=None)
    errors = []

    try:
        records = read_all_metadata(str(source_folder), repository.get_config(), errors=errors)

        # Importing and cleaning up missing tracks succeed or fail together.
        with transaction.atomic():
            import_and_carry_over(records)
            if not errors:
                remove_missing_tracks(source_folder, (record["file_path"] for record in records))

        _update_scan_state(
            source_folder,
            "partial" if errors else "complete",
            finished=timezone.now(),
            error="\n".join(errors[:30]) or None,
            count=len(records),
        )
        return len(records), errors
    except Exception as error:
        _update_scan_state(source_folder, "failed", finished=timezone.now(), error=str(error))
        raise


def _scan_worker(source_folder):
    try:
        scan_library(source_folder)
    finally:
        _scan_lock.release()
        connection.close()  # this thread's own database connection


def start_scan(source_folder):
    global _scan_thread
    if not _scan_lock.acquire(blocking=False):
        return False
    try:
        source_folder = repository.resolve_allowed_folder(source_folder)
    except Exception:
        _scan_lock.release()
        raise
    _scan_thread = threading.Thread(
        target=_scan_worker,
        args=(source_folder,),
        name="music-library-scan",
        daemon=True,
    )
    _scan_thread.start()
    return True


def is_importing():
    return _scan_lock.locked()


@dataclass
class TagReadResult:
    files_read: int = 0
    missing: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    replaced: int = 0
    albums_carried: int = 0
    artists_carried: int = 0
    track_ids: set = field(default_factory=set)


def read_local_tags(*, album_ids=None, artist_ids=None):
    """Read Local MP3 Tags again for the tracks of these albums or artists.

    Only the files already in the catalogue are read (new files arrive with Import
    Data). A file whose tags changed so much that it now maps to another track (or
    album, or artist) row replaces its old row, and the old entry's details are
    carried over (see services.carry_over). Files that are gone are reported, never
    deleted here.
    Returns a :class:`TagReadResult`, or None when an import is already running.
    """
    if not album_ids and not artist_ids:
        raise ValueError("Choose an artist or an album.")
    if not _scan_lock.acquire(blocking=False):
        return None
    try:
        tracks = Track.objects.all()
        tracks = tracks.filter(album_id__in=album_ids) if album_ids else tracks.filter(album__artist_id__in=artist_ids)
        scoped = list(tracks.exclude(track_file_path="").values_list("pk", "track_file_path"))
        result = TagReadResult()
        readable = []
        for _, file_path in scoped:
            if repository.is_allowed_file(file_path):
                readable.append(file_path)
            else:
                result.missing.append(file_path)
        readable = sorted(set(readable))

        records = read_metadata_for_files(readable, repository.get_config(), errors=result.errors)
        result.files_read = len(records)
        with transaction.atomic():
            stats, carried = import_and_carry_over(records, touched=result.track_ids)
        result.created = stats["Track", "created"]
        result.updated = stats["Track", "updated"]
        result.unchanged = stats["Track", "unchanged"]
        result.replaced = carried.tracks_replaced
        result.albums_carried = carried.albums
        result.artists_carried = carried.artists
        return result
    finally:
        _scan_lock.release()
