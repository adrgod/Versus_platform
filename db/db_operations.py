import sqlite3
from pathlib import Path

def db_create_schema(con):

    cur = con.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_artist(artist_id INTEGER PRIMARY KEY, artist_name TEXT UNIQUE, when_loaded DATETIME)")

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_album(album_id INTEGER PRIMARY KEY, artist_id, album_name, disc_number DEFAULT 1, release_year, genre, album_cover BLOB, band_photo BLOB, when_loaded DATETIME)")

    #An album can have the same name for different artists. An artist can have two albums with same name, but an album shouldn't have the same name, artist, year and disc number. makes sense?
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_album_artist_name_number_year
        ON tbl_album (artist_id, album_name, disc_number, release_year)
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_track(track_id INTEGER PRIMARY KEY, album_id, track_name, track_number, track_length INTEGER, dq_confidence FLOAT, file_path TEXT, when_loaded DATETIME)")

    track_columns = {row[1] for row in cur.execute("PRAGMA table_info(tbl_track)")}
    if "file_path" not in track_columns:
        cur.execute("ALTER TABLE tbl_track ADD COLUMN file_path TEXT")

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_track_album_name_number
        ON tbl_track (album_id, track_name, track_number)
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_track_file_path ON tbl_track (file_path)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tbl_library_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            source_folder TEXT,
            last_scan_started TEXT,
            last_scan_finished TEXT,
            last_scan_status TEXT NOT NULL DEFAULT 'never',
            scan_error TEXT,
            scanned_track_count INTEGER NOT NULL DEFAULT 0
        )
    """)

    
def db_load_data(con, records):

    cur = con.cursor()

    artists_data = [
        {"artist": record["artist"]}
        for record in records
    ]


    cur.executemany(
        """INSERT OR IGNORE INTO tbl_artist (artist_name, when_loaded)
        VALUES (:artist, CURRENT_TIMESTAMP)
        """,
        artists_data,
    )
    artist_ids = dict(
        cur.execute(
            "SELECT artist_name, artist_id FROM tbl_artist"
        ).fetchall()
    )

    albums = []
    tracks = []

    for record in records:
        artist_id = artist_ids[record["artist"]]

        albums.append({
            "artist_id": artist_id,
            "album": record["album"],
            "discnumber": record["discnumber"] or 1,
            "date": record["date"],
            "genre": record["genre"],
            "album_cover": record["album_cover"],
        })

    cur.executemany(
        """
        INSERT INTO tbl_album 
        (artist_id, album_name, disc_number, release_year, genre, album_cover, when_loaded)
        VALUES
            (:artist_id, :album, :discnumber, :date, :genre, :album_cover, CURRENT_TIMESTAMP)
        ON CONFLICT (artist_id, album_name, disc_number, release_year)
        DO UPDATE SET
            genre = excluded.genre,
            album_cover = COALESCE(tbl_album.album_cover, excluded.album_cover),
            when_loaded = CURRENT_TIMESTAMP
        """,
        albums
    )

    album_ids = {
        (artist_id, album_name, disc_number, release_year): album_id
        for album_id, artist_id, album_name, disc_number, release_year in cur.execute(
            """
            SELECT album_id, artist_id, album_name, disc_number, release_year
            FROM tbl_album
            """
        )
    }

    for record in records:
        disc_number = record["discnumber"] or 1
        album_id = album_ids[
            artist_ids[record["artist"]],
            record["album"],
            disc_number,
            record["date"],
        ]

        tracks.append({
            "album_id": album_id,
            "title": record["title"],
            "track_number": record["tracknumber"],
            "length": record["length"],
            "dq_confidence": record["dq_confidence"],
            "file_path": record["file_path"],
        })

    cur.executemany(
        """
        INSERT INTO tbl_track
            (album_id, track_name, track_number, track_length, dq_confidence, file_path, when_loaded)
        VALUES
            (:album_id, :title, :track_number, :length, :dq_confidence, :file_path, CURRENT_TIMESTAMP)
        ON CONFLICT (album_id, track_name, track_number)
        DO UPDATE SET
            track_length = excluded.track_length,
            dq_confidence = excluded.dq_confidence,
            file_path = excluded.file_path,
            when_loaded = CURRENT_TIMESTAMP
        """,
        tracks,
    )


def remove_missing_tracks(con, source_folder, scanned_paths):
    source_root = Path(source_folder).resolve()
    scanned_paths = {str(Path(path).resolve()) for path in scanned_paths}
    cur = con.cursor()

    for track_id, file_path in cur.execute(
        "SELECT track_id, file_path FROM tbl_track WHERE file_path IS NOT NULL"
    ).fetchall():
        existing_path = Path(file_path).resolve()
        if (existing_path == source_root or source_root in existing_path.parents) and str(existing_path) not in scanned_paths:
            cur.execute("DELETE FROM tbl_track WHERE track_id = ?", (track_id,))

    cur.execute("""
        DELETE FROM tbl_album
        WHERE NOT EXISTS (
            SELECT 1 FROM tbl_track WHERE tbl_track.album_id = tbl_album.album_id
        )
    """)
    cur.execute("""
        DELETE FROM tbl_artist
        WHERE NOT EXISTS (
            SELECT 1 FROM tbl_album WHERE tbl_album.artist_id = tbl_artist.artist_id
        )
    """)

def load_db_data(db_file_location, data):
    con = sqlite3.connect(db_file_location)
    try:
        db_create_schema(con)
        db_load_data(con, data)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()