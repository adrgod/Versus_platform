
import json
import re
from pathlib import Path
import sqlite3

from mutagen.easyid3 import EasyID3


PROJECT_DIR = Path(__file__).resolve().parent


def load_config():
    config_path = PROJECT_DIR / "config.json"
    with config_path.open(encoding="utf-8") as config_file:
        return json.load(config_file)


config = load_config()
source_folder = PROJECT_DIR / config["source_data_folder"]

file_attribtes = [
    'album', 'length', 'mood', 'title', 'artist', 'albumartist', 'discnumber', 'tracknumber', 'language', 'genre', 'date', 'originaldate'
    ]

NAME_FIELDS = {"artist", "album", "albumartist", "title"}

def format_name(name):
    if not name:
        return ""
    return " ".join(name.split()).title()

def clean_track_name(trackname):
    return re.sub(r"^\s*\d+\s*[-\u2013\u2014]\s*", "", trackname)


def read_metadata(file_path):
    file_info = EasyID3(file_path)
    file_data = {"file_path": str(file_path)}

    for att in file_attribtes:
        value = file_info.get(att)
        if isinstance(value, list):
            file_data[att] = ", ".join(str(item) for item in value)
        else:
            file_data[att] = "" if value is None else str(value)

        if att in NAME_FIELDS:
            file_data[att] = format_name(file_data[att])

        if att in NAME_FIELDS:
            file_data[att] = f'"{clean_track_name(file_data[att])}"'

    return file_data

def db_create_schema(con):

    cur = con.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_artist(artist_id INTEGER PRIMARY KEY, artist_name TEXT UNIQUE)")

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_album(album_id INTEGER PRIMARY KEY, artist_id, album_name, release_year, genre)")

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_track(track_id INTEGER PRIMARY KEY, album_id, track_name, track_number, track_length)")

    cur.execute("""
        UPDATE tbl_track
        SET album_id = (
            SELECT MIN(other.album_id)
            FROM tbl_album AS current
            JOIN tbl_album AS other
                ON other.artist_id = current.artist_id
                AND other.album_name = current.album_name
            WHERE current.album_id = tbl_track.album_id
        )
    """)
    cur.execute("""
        DELETE FROM tbl_album
        WHERE album_id NOT IN (
            SELECT MIN(album_id)
            FROM tbl_album
            GROUP BY artist_id, album_name
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_album_artist_name
        ON tbl_album (artist_id, album_name)
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_track_album_name_number
        ON tbl_track (album_id, track_name, track_number)
    """)

    
def db_load_data(con, records):

    cur = con.cursor()

    artists_data = [
        {"artist": record["artist"]}
        for record in records
    ]

    cur.executemany(
        """INSERT OR IGNORE INTO tbl_artist (artist_name)
        VALUES (:artist)
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
            "date": record["date"],
            "genre": record["genre"],
        })

    cur.executemany(
        """
        INSERT OR IGNORE INTO tbl_album 
        (artist_id, album_name, release_year, genre)
        VALUES
            (:artist_id, :album, :date, :genre)
        """,
        albums
    )

    album_ids = {
        (artist_id, album_name): album_id
        for artist_id, album_name, album_id in cur.execute(
            """
            SELECT artist_id, album_name, album_id
            FROM tbl_album
            """
        )
    }

    for record in records:
        album_id = album_ids[artist_ids[record["artist"]], record["album"]]

        tracks.append({
            "album_id": album_id,
            "title": record["title"],
            "track_number": record["tracknumber"],
            "length": record["length"],
        })

    cur.executemany(
        """
        INSERT OR IGNORE INTO tbl_track
            (album_id, track_name, track_number, track_length)
        VALUES
            (:album_id, :title, :track_number, :length)
        """,
        tracks,
    )

def start():
    mp3_files = sorted(
        file_path
        for file_path in source_folder.rglob("*")
        if file_path.is_file() and file_path.suffix.lower() == ".mp3"
    )

    records = []

    for file_path in mp3_files:
        try:
            records.append(read_metadata(file_path))
        except Exception as error:
            print(f"Could not read {file_path}: {error}")
            continue

    #DB Stuff
    con = sqlite3.connect("music_data.db")

    db_create_schema(con)
    db_load_data(con, records)

    con.commit()
    con.close()
    

if __name__ == '__main__':
    start()
