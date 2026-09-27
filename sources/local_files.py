
import re
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, ID3NoHeaderError

PROJECT_DIR = Path(__file__).resolve().parent.parent


def format_name(name):
    if not name:
        return ""
    return " ".join(name.split()).title()

def clean_track_name(trackname):
    return re.sub(r"^\s*\d+\s*[-\u2013\u2014]\s*", "", trackname)



def read_metadata(file_path, config):
    file_info = EasyID3(file_path)
    audio_info = MP3(file_path).info
    file_data = {"file_path": str(file_path)}

    file_attributes = config["file_attributes"]

    name_fields = config["name_fields"]

    for att in file_attributes:
        value = round(audio_info.length, 2) if att == "length" else file_info.get(att)
        if isinstance(value, list):
            file_data[att] = ", ".join(str(item) for item in value)
        else:
            file_data[att] = "" if value is None else str(value)

        if att in name_fields:
            file_data[att] = format_name(file_data[att])

        if att == "title":
            file_data[att] = clean_track_name(file_data[att])

    return file_data



def read_artwork(file_path):
    
    try:
        tags = ID3(file_path)
    except (ID3NoHeaderError, OSError):
        return None

    pictures = tags.getall("APIC")
    if not pictures:
        return None
    
    cover = next((picture for picture in pictures if picture.type == 3), pictures[0])
    return cover.data


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



def add_track_tag_data_quality_confidence(records, mp3_files=None):
    for track in records:
        track['dq_confidence'] = calculate_confidence_for_track(track, track["file_path"])

    return records


def check_empty_fields(records, quality_attributes):

    tracks_with_missing_tags = []
    
    for record in records:
        for tag, value in record.items():
            if tag in quality_attributes and not value:
                tracks_with_missing_tags.append(record["artist"])
                print(f"tag: {tag}; value: {value}")

    return tracks_with_missing_tags


def read_all_metadata(source_folder, config, errors=None):

    quality_attributes = config["quality_attributes"]

    mp3_files = sorted(
            file_path
            for file_path in source_folder.rglob("*")
            if file_path.is_file() and file_path.suffix.lower() == ".mp3"
        )
    
    records = []
    album_cover_cache = {}

    for file_path in mp3_files:
        try:
            record = read_metadata(file_path, config)
            album_key = (
                record["artist"],
                record["album"],
                record["discnumber"] or 1,
                record["date"],
            )
            if album_key not in album_cover_cache:
                album_cover_cache[album_key] = read_artwork(file_path)
            record["album_cover"] = album_cover_cache[album_key]
            records.append(record)
        except Exception as error:
            print(f"Could not read {file_path}: {error}")
            if errors is not None:
                errors.append(f"{file_path}: {error}")
            continue

    records_with_tags = add_track_tag_data_quality_confidence(records, mp3_files)

    tracks_with_empty_fields = check_empty_fields(records, quality_attributes)
    print(tracks_with_empty_fields)

    return records_with_tags