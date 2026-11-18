import threading

from django.db import connection, transaction
from django.utils import timezone

from . import repository
from .models import LibraryState
from .services.files_scanner import read_all_metadata
from .services.importer import import_records, remove_missing_tracks


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
            import_records(records)
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
