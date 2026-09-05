
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

    cur.execute("CREATE TABLE tbl_artist(artisit_id, artist_name)")

    cur.execute("CREATE TABLE tbl_album(album_id, artisti_id, album_name, release_year, genre)")

    cur.execute("CREATE TABLE tbl_track(track_id, album_id, track_name, track_number, track_length)")

def db_load_data(con):
    cur = con.cursor()

def start():
    mp3_files = sorted(
        file_path
        for file_path in source_folder.rglob("*")
        if file_path.is_file() and file_path.suffix.lower() == ".mp3"
    )

    for file_path in mp3_files:
        try:
            file_data = read_metadata(file_path)
        except Exception as error:
            print(f"Could not read {file_path}: {error}")
            continue

        print(f"\nFile: {file_data['file_path']}")
        for elem, value in file_data.items():
            if elem == "file_path":
                continue
            print(f" Value for {elem} is {value or '[empty]'}")

    con = sqlite3.connect("music_data.db")
    
    db_create_schema(con)

if __name__ == '__main__':
    start()
