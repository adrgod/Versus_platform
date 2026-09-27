import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from db import db_operations
from sources import local_files

from . import repository


_scan_lock = threading.Lock()
_scan_thread = None


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _update_scan_state(source_folder, status, *, started=None, finished=None, error=None, count=None, connection=None):
    owned_connection = connection is None
    connection = connection or repository.connect()
    try:
        if owned_connection:
            repository.ensure_schema(connection)
        repository.get_library_state(connection)
        connection.execute(
            """
            UPDATE tbl_library_state
            SET source_folder = ?, last_scan_status = ?,
                last_scan_started = COALESCE(?, last_scan_started),
                last_scan_finished = COALESCE(?, last_scan_finished),
                scan_error = ?, scanned_track_count = COALESCE(?, scanned_track_count)
            WHERE singleton_id = 1
            """,
            (str(source_folder), status, started, finished, error, count),
        )
        if owned_connection:
            connection.commit()
    finally:
        if owned_connection:
            connection.close()


def scan_library(source_folder):
    source_folder = repository.resolve_allowed_folder(source_folder)
    started = _timestamp()
    _update_scan_state(source_folder, "scanning", started=started, error=None)
    errors = []

    try:
        with (repository.PROJECT_ROOT / "config.json").open(encoding="utf-8") as config_file:
            config = json.load(config_file)

        records = local_files.read_all_metadata(source_folder, config, errors=errors)
        connection = repository.connect()
        try:
            db_operations.db_create_schema(connection)
            db_operations.db_load_data(connection, records)
            if not errors:
                db_operations.remove_missing_tracks(
                    connection,
                    source_folder,
                    (record["file_path"] for record in records),
                )
            status = "partial" if errors else "complete"
            _update_scan_state(
                source_folder,
                status,
                finished=_timestamp(),
                error="\n".join(errors[:30]) or None,
                count=len(records),
                connection=connection,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return len(records), errors
    except Exception as error:
        _update_scan_state(
            source_folder,
            "failed",
            finished=_timestamp(),
            error=str(error),
        )
        raise


def _scan_worker(source_folder):
    try:
        scan_library(source_folder)
    finally:
        _scan_lock.release()


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