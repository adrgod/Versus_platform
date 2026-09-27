import json
import sqlite3
from pathlib import Path

from django.conf import settings


PROJECT_ROOT = Path(settings.BASE_DIR)


def get_config():
    with (PROJECT_ROOT / "config.json").open(encoding="utf-8") as config_file:
        return json.load(config_file)


def get_database_path():
    configured_path = Path(get_config()["db_file_location"]).expanduser()
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return configured_path.resolve()


def get_backup_directory():
    return PROJECT_ROOT / "db" / "metadata_backups"


def connect():
    connection = sqlite3.connect(get_database_path(), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_schema(connection=None):
    from db.db_operations import db_create_schema

    owned_connection = connection is None
    connection = connection or connect()
    try:
        db_create_schema(connection)
        connection.commit()
    finally:
        if owned_connection:
            connection.close()


def get_library_state(connection):
    config = get_config()
    source_folder = Path(config["source_data_folder"]).expanduser().resolve()
    connection.execute(
        """
        INSERT OR IGNORE INTO tbl_library_state (singleton_id, source_folder)
        VALUES (1, ?)
        """,
        (str(source_folder),),
    )
    return connection.execute(
        "SELECT * FROM tbl_library_state WHERE singleton_id = 1"
    ).fetchone()


def get_allowed_roots():
    config = get_config()
    configured_roots = config.get("allowed_library_roots")
    if not configured_roots:
        configured_roots = [str(Path(config["source_data_folder"]).expanduser().resolve().parent)]
    roots = []
    for configured_root in configured_roots:
        root = Path(configured_root).expanduser().resolve()
        if root.is_dir():
            roots.append(root)
    return roots


def resolve_allowed_folder(folder_path):
    path = Path(folder_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("That folder does not exist or is not accessible.")
    roots = [Path(root).expanduser().resolve() for root in get_allowed_roots()]
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("That folder is outside the configured music-library roots.")
    return path


def is_allowed_file(file_path):
    path = Path(file_path).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in get_allowed_roots()]
    return path.is_file() and any(
        path == root or root in path.parents for root in roots
    )


def dashboard_stats(connection):
    return connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM tbl_artist) AS artist_count,
            (SELECT COUNT(*) FROM tbl_album) AS album_count,
            (SELECT COUNT(*) FROM tbl_track) AS track_count,
            (SELECT COUNT(*) FROM tbl_album WHERE album_cover IS NULL OR length(album_cover) = 0) AS missing_artwork_count,
            (SELECT COUNT(*) FROM tbl_album WHERE release_year IS NULL OR release_year = '') AS missing_year_count,
            (SELECT COUNT(*) FROM tbl_album WHERE genre IS NULL OR genre = '') AS missing_genre_count,
            (SELECT ROUND(AVG(dq_confidence), 2) FROM tbl_track) AS average_quality
        """
    ).fetchone()


def album_cover_data_uri(image_data):
    if not image_data:
        return None
    import base64

    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        mime_type = "image/jpeg"
    return f"data:{mime_type};base64,{base64.b64encode(image_data).decode('ascii')}"