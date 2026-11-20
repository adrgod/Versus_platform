"""Finds and reads the text that comes with an album: liner notes, press sheets, biographies.

Looks in the album folder (any depth) for .txt, .nfo and .pdf files. Files that are
lyrics (.lrc, or a .txt named like a track or with "lyric" in its name) are left to
services/lyrics.py. A file is only read again when its size or modification time changes.
"""
import re
from datetime import datetime
from pathlib import Path

from library import repository
from library.models import AlbumDocument
from library.services.files_scanner import DISC_FOLDER, album_folder

NOTE_SUFFIXES = {".txt", ".nfo", ".pdf"}
MONTHS = {name: number for number, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
DATE_LABEL = r"(?:release(?:d)?(?:\s+date)?|street\s+date|out\s+(?:on|now))\s*(?:on)?\s*[:\-–]?\s*"
DATE_FORMS = [
    (re.compile(DATE_LABEL + r"(\d{4})-(\d{1,2})-(\d{1,2})", re.I), ("y", "m", "d")),
    (re.compile(DATE_LABEL + r"(\d{1,2})[./](\d{1,2})[./](\d{4})", re.I), ("d", "m", "y")),
    (re.compile(DATE_LABEL + r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?\s+(\d{4})", re.I), ("d", "month", "y")),
    (re.compile(DATE_LABEL + r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I), ("month", "d", "y")),
    (re.compile(DATE_LABEL + r"([a-z]{3,9})\.?\s+(\d{4})", re.I), ("month", "y")),
    (re.compile(DATE_LABEL + r"(\d{4})\b", re.I), ("y",)),
]


def album_folder_for(track_paths):
    """The folder holding an album's files: the batch-layout album folder, above any CD1/Disc 2 folder."""
    for path in track_paths:
        if not path:
            continue
        folder = album_folder(path)
        if DISC_FOLDER.match(folder.name.strip()):
            folder = folder.parent
        if folder.is_dir():
            return folder
    return None


def is_lyrics_file(path, track_stems):
    path = Path(path)
    if path.suffix.lower() == ".lrc":
        return True
    return path.suffix.lower() == ".txt" and (path.stem in track_stems or "lyric" in path.stem.casefold())


def read_text(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader  # imported here so the app runs without pypdf until a PDF is met

        reader = PdfReader(str(path))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    else:
        raw = path.read_bytes()
        encodings = ["cp437"] if suffix == ".nfo" else ["utf-8-sig", "cp1252", "latin-1"]
        for encoding in encodings:
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def find_release_date(text):
    """An ISO date ("1982-03-29", "1982-03" or "1982") written as "Release date: ..." in the text."""
    for pattern, parts in DATE_FORMS:
        match = pattern.search(text or "")
        if not match:
            continue
        values = dict(zip(parts, match.groups()))
        if "month" in values:
            name = values["month"].casefold()
            month = MONTHS.get(name) or MONTHS.get(name[:3])  # "March", "Mar" and "Sept" all work
            if not month:
                continue
            values["m"] = month
        year = int(values["y"])
        if not 1900 <= year <= datetime.now().year + 1:
            continue
        if "d" in values:
            try:
                return datetime(year, int(values["m"]), int(values["d"])).date().isoformat()
            except ValueError:
                continue
        if "m" in values:
            return f"{year}-{int(values['m']):02d}"
        return str(year)
    return ""


def load_album_documents(album, track_paths=None, report=None):
    """Store the album's note files; unchanged files are not read again, removed ones are dropped.

    ``report`` (a list) receives plain sentences about what was found, so the page can explain an empty result.
    """
    report = [] if report is None else report
    if track_paths is None:
        track_paths = list(album.track_set.values_list("track_file_path", flat=True))
    track_paths = [path for path in track_paths if path and repository.is_allowed_file(path)]
    folder = album_folder_for(track_paths)
    if folder is None:
        report.append("The album folder was not found: none of its tracks points to a file on disk "
                      "(try Read Local MP3 Tags, or check the music folder is available).")
        return []

    track_stems = {Path(path).stem for path in track_paths}
    try:
        files = sorted(
            path for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in NOTE_SUFFIXES and not is_lyrics_file(path, track_stems)
        )
    except OSError:
        files = []

    if not files:
        report.append(f"No .txt, .nfo or .pdf file found in {folder}.")
    existing = {document.file_path: document for document in album.documents.all()}
    kept = []
    for path in files:
        stat = path.stat()
        document = existing.pop(str(path), None)
        if document and (document.file_mtime_ns, document.file_size) == (stat.st_mtime_ns, stat.st_size):
            kept.append(document)
            continue
        try:
            text = read_text(path)
        except ImportError:
            text = ""
            report.append(f"{path.name}: reading PDFs needs the pypdf package (run: pip install pypdf).")
        except Exception as error:  # an unreadable or encrypted PDF is skipped, not fatal
            text = ""
            report.append(f"{path.name}: could not be read ({type(error).__name__}).")
        else:
            if not text:
                report.append(f"{path.name}: no text found in it (a scanned PDF is only pictures).")
        if not text:
            if document:
                document.delete()
            continue
        document = document or AlbumDocument(album=album, file_path=str(path))
        document.file_type = path.suffix.lower().lstrip(".")
        document.text = text
        document.file_mtime_ns, document.file_size = stat.st_mtime_ns, stat.st_size
        document.release_date = find_release_date(text)
        document.save()
        kept.append(document)
    for stale in existing.values():
        stale.delete()
    if kept:
        report.append(f"Read {len(kept)} file{'s' if len(kept) != 1 else ''} from {folder}: {', '.join(Path(d.file_path).name for d in kept)}.")
    return kept
