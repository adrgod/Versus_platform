import re
from dataclasses import dataclass
from collections import Counter
from datetime import datetime
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, ID3NoHeaderError

from io import BytesIO
from PIL import Image, ImageOps

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent


# EasyID3 has no "publisher" key by default; it is the TPUB frame.
EasyID3.RegisterTextKey("publisher", "TPUB")
# Artist details have no standard ID3 frame, so they are kept in user text (TXXX) frames.
# They are part of an artist's identity in the catalogue (see importer), so they are read like any tag.
EasyID3.RegisterTXXXKey("artist_country", "ARTIST_COUNTRY")
EasyID3.RegisterTXXXKey("artist_formed_year", "ARTIST_FORMED_YEAR")


def format_name(name):
    if not name:
        return ""
    return " ".join(name.split()).title()


def clean_track_name(trackname):
    return re.sub(r"^\s*\d+\s*\s*", "", trackname)


def clean_tracknumber(tracknumber):
    match = re.match(r"^\s*(\d+)", tracknumber)
    return match.group(1) if match else ""


def read_artwork_from_id3(file_path):
    try:
        tags = ID3(file_path)
    except (ID3NoHeaderError, OSError):
        return None

    pictures = tags.getall("APIC")
    if not pictures:
        return None

    cover = next((picture for picture in pictures if picture.type == 3), pictures[0])

    return compress_image(cover.data)


def compress_image(data):
    """Image bytes -> JPEG of at most 600x600; None when the data is not an image."""
    try:
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((600, 600), Image.Resampling.LANCZOS)

        output = BytesIO()
        image.convert("RGB").save(
            output,
            format="JPEG",
            quality=85,
            optimize=True,
        )
    except Exception:
        return None
    return output.getvalue()


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class BatchLayout:
    """Where a file sits in a batch folder layout (see :func:`batch_layout`)."""

    index: int        # position of the YYYYMMDD folder in the path parts
    publisher: str
    artist: str
    album: str
    album_depth: int  # number of path parts up to and including the album folder


# "Artist - Album", "Artist_-_Album", "Artist – Album" (spaces or underscores around the dash).
ARTIST_ALBUM_SEPARATOR = re.compile(r"(?:\s+|_+)[-–—](?:\s+|_+)")


def split_artist_album(folder_name):
    """("Artist", "Album") from an "Artist - Album" folder name; None without a separator."""
    parts = ARTIST_ALBUM_SEPARATOR.split(folder_name.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    artist, album = (" ".join(part.replace("_", " ").split()) for part in parts)
    return (artist, album) if artist and album else None


def _is_batch_date(name):
    if len(name) != 8 or not name.isdigit():
        return False
    try:
        datetime.strptime(name, "%Y%m%d")
    except ValueError:
        return False
    return True


def batch_layout(file_path):
    """The batch layout of a file, or None. Two layouts are recognised:

    * ``YYYYMMDD/LABEL/ARTIST - ALBUM/[CDn/...]/track.mp3`` (the most common one), and
    * ``YYYYMMDD/LABEL/ARTIST/ALBUM/[CDn/...]/track.mp3``.

    The closest ``YYYYMMDD`` folder that gives one of them wins. A folder right below
    the label whose name has an "Artist - Album" separator is the combined form.
    """
    parts = Path(file_path).parts
    for index in range(len(parts) - 4, -1, -1):
        if not _is_batch_date(parts[index]):
            continue
        below = parts[index + 1:-1]  # folders between the date folder and the file
        publisher, rest = below[0].strip(), below[1:]
        combined = split_artist_album(rest[0])
        if combined:
            return BatchLayout(index, publisher, combined[0], combined[1], index + 3)
        if len(rest) >= 2:
            return BatchLayout(index, publisher, rest[0].strip(), rest[1].strip(), index + 4)
    return None


def _batch_index(parts):
    """Index of the batch date folder when the path follows a batch layout, else None."""
    layout = batch_layout(Path(*parts)) if parts else None
    return layout.index if layout else None


def album_folder(file_path):
    """The album folder of a batch layout path (see :func:`batch_layout`); else the file's folder."""
    path = Path(file_path)
    layout = batch_layout(path)
    return Path(*path.parts[: layout.album_depth]) if layout else path.parent


DISC_FOLDER = re.compile(r"^(?:cd|dis[ck])[\s._-]*0*(\d{1,2})(?!\d)", re.IGNORECASE)


def disc_from_path(file_path):
    """Disc number from a folder such as CD2, Disc 02 or disk-3 between the album folder and the file.

    Returns None when there is no such folder (the deepest one wins).
    """
    path = Path(file_path)
    album = album_folder(path)
    try:
        names = path.parent.relative_to(album).parts if album != path.parent else ()
    except ValueError:
        names = ()
    if not names and _batch_index(path.parts) is None:
        names = (path.parent.name,)
    for name in reversed(names):
        match = DISC_FOLDER.match(name.strip())
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


TRACK_FILENAME = re.compile(r"^\s*(\d{1,3})\s*[-._]\s*(\S.*?)\s*$")


def parse_track_filename(file_path):
    """(track number, title) from '01 - Track Name.mp3' or '01_Track_Name.mp3'; (None, '') otherwise."""
    match = TRACK_FILENAME.match(Path(file_path).stem)
    if not match or int(match.group(1)) == 0:
        return None, ""
    title = " ".join(match.group(2).replace("_", " ").split())
    return int(match.group(1)), title


def derived_tags(file_path):
    """Tag values the path implies; empty unless it follows a batch layout (:func:`batch_layout`)."""
    path = Path(file_path)
    layout = batch_layout(path)
    if layout is None:
        return {}
    values = {"publisher": layout.publisher, "artist": layout.artist, "album": layout.album}
    number, title = parse_track_filename(path)
    if number is not None:
        values["tracknumber"] = str(number)
        values["title"] = title
    disc = disc_from_path(path)
    if disc is not None:
        values["discnumber"] = str(disc)
    return values


def find_cover_image(file_path, album, artist, cache=None):
    """Compressed cover from an image file inside the album folder (any depth).

    Only images whose name contains "cover", the album name or the artist name
    qualify; "cover" beats the album name, which beats the artist name. Folders
    such as "photos" or "artwork" are searched like any other.
    """
    cache = {} if cache is None else cache
    folder = album_folder(file_path)
    key = (folder, album, artist)
    if key in cache:
        return cache[key]

    if folder not in cache:
        try:
            cache[folder] = sorted(
                path
                for path in folder.rglob("*")
                if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file()
            )
        except OSError:
            cache[folder] = []

    album_key = normalize_for_path_match(album)
    artist_key = normalize_for_path_match(artist)
    best, best_rank = None, None
    for image_path in cache[folder]:
        name = normalize_for_path_match(image_path.stem)
        if "cover" in name:
            score = 3
        elif len(album_key) >= 3 and album_key in name:
            score = 2
        elif len(artist_key) >= 3 and artist_key in name:
            score = 1
        else:
            continue
        rank = (-score, len(image_path.relative_to(folder).parts), str(image_path))
        if best_rank is None or rank < best_rank:
            best, best_rank = image_path, rank

    data = None
    if best is not None:
        try:
            data = compress_image(best.read_bytes())
        except OSError:
            data = None
    cache[key] = data
    return data


def parse_batch_path(file_path):
    """Batch date and publisher from ``.../YYYYMMDD/PUBLISHER/ARTIST - ALBUM/file`` (or ``.../ARTIST/ALBUM/file``).

    Returns ``(date | None, str)``. The closest ``YYYYMMDD`` folder that still
    leaves room for publisher, band, album and file is the batch; the folder
    after it is the publisher. Paths that do not follow the layout give
    ``(None, "")``.
    """
    layout = batch_layout(file_path)
    if layout is None:
        return None, ""
    return datetime.strptime(Path(file_path).parts[layout.index], "%Y%m%d").date(), layout.publisher


def batch_date_from_path(file_path):
    """Batch date of a file: the closest ``YYYYMMDD`` folder above it, at any depth.

    Looser than :func:`parse_batch_path` on purpose: ``20261013/Label/Band - Album/01.mp3``
    still belongs to the 13 Oct 2026 batch even though band and album share a folder
    (artist/album are then not derived from the path). Returns None without such a folder.
    """
    for name in reversed(Path(file_path).parent.parts):
        if len(name) == 8 and name.isdigit():
            try:
                return datetime.strptime(name, "%Y%m%d").date()
            except ValueError:
                continue
    return None


def read_file_metadata(file_path, config, image_cache=None):
    try:
        file_info = EasyID3(file_path)
    except ID3NoHeaderError:
        file_info = {}  # no tags at all: everything is empty and can be filled from the path
    audio_info = MP3(file_path).info
    file_data: dict[str, str | bytes | None] = {
        "file_path": str(file_path)
    }

    file_attributes = config["file_attributes"]
    name_fields = config["name_fields"]

    for att in file_attributes:
        if att == "album_cover":
            file_data[att] = read_artwork_from_id3(file_path)
            continue

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

    # A CD2 / Disc 2 folder is explicit, so it beats the tag (often left at 1).
    folder_disc = disc_from_path(file_path)
    if folder_disc is not None:
        file_data["discnumber"] = str(folder_disc)

    # Empty tags are filled from the folder layout / file name (same rules as the file writer).
    derived = derived_tags(file_path)
    for name in ("artist", "album", "title", "tracknumber", "publisher"):
        if derived.get(name) and not str(file_data.get(name) or "").strip():
            file_data[name] = format_name(derived[name]) if name in name_fields else derived[name]

    # The embedded cover leads; otherwise look for an image file in the album folder.
    if "album_cover" in file_attributes and not file_data.get("album_cover"):
        file_data["album_cover"] = find_cover_image(
            file_path, file_data.get("album", ""), file_data.get("artist", ""), image_cache
        )

    file_data["batch_date"] = batch_date_from_path(file_path)

    return file_data


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
        track["dq_confidence"] = calculate_confidence_for_track(track, track["file_path"])
    return tracks


def check_empty_fields(tracks, quality_attributes):
    tracks_with_missing_tags = {}

    for track in tracks:
        for tag, value in track.items():
            if tag in quality_attributes and not value:
                tag_key = (
                    track["artist"],
                    track["album"],
                    track["title"],
                    track["tracknumber"],
                )
                tracks_with_missing_tags[tag_key] = tag

    return tracks_with_missing_tags


def standardize_release_year(tracks):
    dates_by_album = {}
    for track in tracks:
        album_key = (track["artist"], track["album"])
        date = str(track.get("date", "")).strip()
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

    return tracks


def classify_release_length(tracks):
    df = pd.DataFrame(tracks)

    df["length"] = pd.to_numeric(df["length"], errors="coerce").fillna(0)

    album_fields = ["artist", "album", "date"]  # all discs together
    df["album_length"] = df.groupby(album_fields)["length"].transform("sum")

    df["album_length_class"] = (
        df["album_length"]
        .ge(1800)
        .map({True: "LP", False: "EP"})
    )

    return df.to_dict(orient="records")


def read_all_metadata(source_folder, config, errors=None):
    source_folder = Path(source_folder).expanduser()

    if source_folder.is_file():
        mp3_files = [source_folder] if source_folder.suffix.lower() == ".mp3" else []
    else:
        mp3_files = sorted(
            file_path
            for file_path in source_folder.rglob("*")
            if file_path.is_file() and file_path.suffix.lower() == ".mp3"
        )

    return read_metadata_for_files(mp3_files, config, errors=errors)


def read_metadata_for_files(mp3_files, config, errors=None):
    """Read and post-process the given MP3 files (whole albums give the best length class)."""
    mp3_files = [Path(file_path) for file_path in mp3_files]
    tracks = []
    image_cache = {}
    for file_path in mp3_files:
        try:
            track = read_file_metadata(file_path, config, image_cache)
            tracks.append(track)
        except Exception as error:
            if errors is not None:
                errors.append(f"{file_path}: {error}")
            continue

    if not tracks:
        return []

    tracks_with_tags = add_track_tag_data_quality_confidence(tracks, mp3_files)
    tracks_with_empty_fields = check_empty_fields(tracks_with_tags, config["quality_attributes"])
    standardize_release_year(tracks_with_tags)

    return classify_release_length(tracks_with_tags)