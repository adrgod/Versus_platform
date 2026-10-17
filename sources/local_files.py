
import re
from collections import Counter
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
    return re.sub(r"^\s*\d+\s*\s*", "", trackname)

def clean_tracknumber(tracknumber):
    match = re.match(r"^\s*(\d+)", tracknumber)
    return match.group(1) if match else ""
    

def read_file_metadata(file_path, config):
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

        if att == "tracknumber":
            file_data[att] = clean_tracknumber(file_data[att])


    return file_data



def read_artwork_from_id3(file_path):
    
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



def add_track_tag_data_quality_confidence(tracks, mp3_files=None):
    for track in tracks:
        track['dq_confidence'] = calculate_confidence_for_track(track, track["file_path"])

    return tracks


def check_empty_fields(tracks, quality_attributes):

    tracks_with_missing_tags = dict()
    
    for track in tracks:
        for tag, value in track.items():
            if tag in quality_attributes and not value:
                
                tag_key = (
                    track["artist"],
                    track["album"],
                    track["title"],
                    track["tracknumber"]
                )
                
                tracks_with_missing_tags[tag_key] = tag

    return tracks_with_missing_tags


def standardize_release_year(tracks):

    dates_by_album = {}
    for track in tracks:
        album_key = (track["artist"], track["album"])
        date = track["date"].strip()
        if date:
            dates_by_album.setdefault(album_key, []).append(date)

    standard_dates = {
        album_key: Counter(dates).most_common(1)[0][0]
        for album_key, dates in dates_by_album.items()
    }
    
    for track in tracks:
        album_key = (track["artist"], track["album"])
        if album_key in standard_dates:
            track["date"] = standard_dates[album_key]

    #print("Standard release dates by artist and album:", standard_dates)
    return tracks

def read_all_metadata(source_folder, config, errors=None):

    quality_attributes = config["quality_attributes"]

    mp3_files = sorted(
            file_path
            for file_path in source_folder.rglob("*")
            if file_path.is_file() and file_path.suffix.lower() == ".mp3"
        )
    
    tracks = []
    album_cover_cache = {}

    for file_path in mp3_files:
        try:
            track = read_file_metadata(file_path, config)
            album_key = (
                track["artist"],
                track["album"],
                track["discnumber"] or 1,
                track["date"],
            )
            if album_key not in album_cover_cache:
                album_cover_cache[album_key] = read_artwork_from_id3(file_path)
            track["album_cover"] = album_cover_cache[album_key]
            tracks.append(track)
        except Exception as error:
            print(f"Could not read {file_path}: {error}")
            if errors is not None:
                errors.append(f"{file_path}: {error}")
            continue

    tracks_with_tags = add_track_tag_data_quality_confidence(tracks, mp3_files)

    tracks_with_empty_fields = check_empty_fields(tracks_with_tags, quality_attributes)

    standardize_release_year(tracks)
    
    print("Report with empty fields:")
    print(tracks_with_empty_fields)

    return tracks_with_tags