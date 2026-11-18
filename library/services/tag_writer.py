"""Fills empty ID3 tags in MP3 files from the batch folder layout.

Layout: ``.../YYYYMMDD/PUBLISHER/BAND/ALBUM/[CDn/...]/01 - Track Name.mp3``.
Only tags that are empty in the file are written (file by file), and only for
files whose path follows that layout. Nothing is backed up on disk: the file's
original tag block is kept in memory while it is written and put back at once
if the result does not verify. A small CSV log of what was written is kept.
"""
import csv
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.db import connection
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1, TPOS, TPUB, TRCK

from library import repository
from library.models import Track
from library.services.files_scanner import derived_tags, find_cover_image, parse_batch_path

FRAMES = {
    "artist": ("TPE1", TPE1),
    "album": ("TALB", TALB),
    "title": ("TIT2", TIT2),
    "tracknumber": ("TRCK", TRCK),
    "publisher": ("TPUB", TPUB),
    "discnumber": ("TPOS", TPOS),
}


def id3_header_length(head):
    """Length of the ID3v2 block (header + body + footer) from the first 10 bytes."""
    if len(head) < 10 or head[:3] != b"ID3":
        return 0
    size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
    return 10 + size + (10 if head[5] & 0x10 else 0)


def _tag_length(path):
    with open(path, "rb") as handle:
        return id3_header_length(handle.read(10))


class TagSnapshot:
    """The file's original ID3v2 block, held in memory, for an exact restore."""

    def __init__(self, path):
        self.path = Path(path)
        length = _tag_length(self.path)
        with open(self.path, "rb") as handle:
            self.tag_bytes = handle.read(length)
        self.audio_size = self.path.stat().st_size - length

    def audio_unchanged(self):
        return self.path.stat().st_size - _tag_length(self.path) == self.audio_size

    def restore(self):
        with open(self.path, "rb") as handle:
            handle.seek(_tag_length(self.path))
            audio = handle.read()
        with open(self.path, "wb") as handle:
            handle.write(self.tag_bytes)
            handle.write(audio)


@dataclass
class FilePlan:
    path: str
    changes: dict = field(default_factory=dict)  # tag name -> value to write
    cover: bytes | None = None

    @property
    def is_empty(self):
        return not self.changes and not self.cover


def _frame_text(tags, frame_id):
    return "; ".join(str(text) for frame in tags.getall(frame_id) for text in frame.text).strip()


def _read_tags(path):
    try:
        return ID3(path)
    except ID3NoHeaderError:
        return ID3()


def plan_file(path, image_cache=None):
    """What would be written to this file; None when its path is not in the batch layout."""
    derived = derived_tags(path)
    if not derived:
        return None
    tags = _read_tags(path)
    plan = FilePlan(str(path))
    for name, (frame_id, _) in FRAMES.items():
        if derived.get(name) and not _frame_text(tags, frame_id):
            plan.changes[name] = derived[name]
    if not tags.getall("APIC"):
        album = _frame_text(tags, "TALB") or derived.get("album", "")
        artist = _frame_text(tags, "TPE1") or derived.get("artist", "")
        plan.cover = find_cover_image(path, album, artist, image_cache)
    return plan


def write_file(plan):
    """Write the plan; restore the original tags and raise if it does not verify."""
    snapshot = TagSnapshot(plan.path)
    try:
        tags = _read_tags(plan.path)
        for name, value in plan.changes.items():
            frame_id, frame_class = FRAMES[name]
            tags.setall(frame_id, [frame_class(encoding=3, text=[value])])
        if plan.cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=plan.cover))
        version = 3 if tags.version[:2] == (2, 3) else 4
        tags.save(plan.path, v1=0, v2_version=version)

        written = ID3(plan.path)
        for name, value in plan.changes.items():
            if _frame_text(written, FRAMES[name][0]) != value:
                raise ValueError(f"{name} did not verify")
        if plan.cover and not written.getall("APIC"):
            raise ValueError("cover did not verify")
        if not snapshot.audio_unchanged():
            raise ValueError("audio data changed")
    except Exception:
        snapshot.restore()
        raise


def batch_files(day):
    """Existing files of the library whose path is in batch ``day`` (a date)."""
    wanted = day.strftime("%Y%m%d")
    stored = Track.objects.filter(track_file_path__contains=wanted).values_list("track_file_path", flat=True)
    return sorted(
        path for path in set(stored)
        if parse_batch_path(path)[0] == day and repository.is_allowed_file(path)
    )


@dataclass
class BatchWriteJob:
    day: object
    running: bool = True
    total: int = 0
    done: int = 0
    written: int = 0
    unchanged: int = 0
    covers: int = 0
    failed: list = field(default_factory=list)
    log_path: str = ""
    error: str = ""


_jobs = {}
_jobs_lock = threading.Lock()


def get_job(day):
    return _jobs.get(day)


def start_batch_write(day):
    """Start writing in the background; False when this batch is already being written."""
    with _jobs_lock:
        current = _jobs.get(day)
        if current and current.running:
            return False
        job = _jobs[day] = BatchWriteJob(day=day)
    threading.Thread(target=_run, args=(job,), name=f"tag-write-{day:%Y%m%d}", daemon=True).start()
    return True


def _run(job):
    rows = []
    try:
        files = batch_files(job.day)
        job.total = len(files)
        image_cache = {}
        for path in files:
            try:
                plan = plan_file(path, image_cache)
                if plan is None or plan.is_empty:
                    job.unchanged += 1
                else:
                    write_file(plan)
                    job.written += 1
                    job.covers += 1 if plan.cover else 0
                    rows.extend((path, name, value) for name, value in plan.changes.items())
                    if plan.cover:
                        rows.append((path, "cover", "embedded image"))
            except Exception as error:
                job.failed.append(f"{path}: {error}")
            job.done += 1
        job.log_path = _write_log(job, rows)
    except Exception as error:
        job.error = str(error)
    finally:
        job.running = False
        connection.close()  # this thread's own database connection


def _write_log(job, rows):
    if not rows:
        return ""
    folder = repository.get_log_directory()
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / f"batch-{job.day:%Y%m%d}-{datetime.now():%Y%m%dT%H%M%S}.csv"
    with open(log_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "tag", "value written (tag was empty before)"])
        writer.writerows(rows)
    return str(log_path)
