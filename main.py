
import json
import re
from pathlib import Path
import sqlite3

from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3


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
    audio_info = MP3(file_path).info
    file_data = {"file_path": str(file_path)}

    for att in file_attribtes:
        value = round(audio_info.length, 2) if att == "length" else file_info.get(att)
        if isinstance(value, list):
            file_data[att] = ", ".join(str(item) for item in value)
        else:
            file_data[att] = "" if value is None else str(value)

        if att in NAME_FIELDS:
            file_data[att] = format_name(file_data[att])

        if att == "title":
            file_data[att] = clean_track_name(file_data[att])

    return file_data

def db_create_schema(con):

    cur = con.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_artist(artist_id INTEGER PRIMARY KEY, artist_name TEXT UNIQUE, when_loaded DATETIME)")

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_album(album_id INTEGER PRIMARY KEY, artist_id, album_name, disc_number DEFAULT 1, release_year, genre, when_loaded DATETIME)")

    #An album can have the same name for different artists. An artist can have two albums with same name, but an album shouldn't have the same name, artist, year and disc number. makes sense?
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_album_artist_name_number_year
        ON tbl_album (artist_id, album_name, disc_number, release_year)
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS tbl_track(track_id INTEGER PRIMARY KEY, album_id, track_name, track_number, track_length INTEGER, dq_confidence FLOAT, when_loaded DATETIME)")

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
        })

    cur.executemany(
        """
        INSERT OR IGNORE INTO tbl_album 
        (artist_id, album_name, disc_number, release_year, genre, when_loaded)
        VALUES
            (:artist_id, :album, :discnumber, :date, :genre, CURRENT_TIMESTAMP)
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
            "dq_confidence": record["dq_confidence"]
        })

    cur.executemany(
        """
        INSERT OR IGNORE INTO tbl_track
            (album_id, track_name, track_number, track_length, dq_confidence, when_loaded)
        VALUES
            (:album_id, :title, :track_number, :length, :dq_confidence ,CURRENT_TIMESTAMP)
        """,
        tracks,
    )

def normalize_for_path_match(value):
    return re.sub(r"[\s_./\\-]+", "", str(value).casefold())


def validate_if_tags_in_filepath(tag_text, filepath_text):
    return normalize_for_path_match(tag_text) in normalize_for_path_match(filepath_text)


def calculate_confidence_for_track(track, file_path):

    confidence_level = 0.0

    if validate_if_tags_in_filepath(track["artist"], file_path):
        confidence_level += 0.3
    if validate_if_tags_in_filepath(track["album"], file_path):
        confidence_level += 0.3
    if validate_if_tags_in_filepath(track["title"], file_path):
        confidence_level += 0.4

    return round(confidence_level, 2)



def add_track_tag_data_quality_confidence(records, mp3_files):
    for track, file_path in zip(records, mp3_files):
        track['dq_confidence'] = calculate_confidence_for_track(track, file_path)

    return records

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

    records_with_tags = add_track_tag_data_quality_confidence(records, mp3_files)

    #validate_tag_with_path(records, mp3_files)

    db_create_schema(con)
    db_load_data(con, records_with_tags)

    con.commit()
    con.close()
    

if __name__ == '__main__':
    start()
