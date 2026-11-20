"""Fill in Missing Data: raise the completeness percentage using only what can be found.

What the Completeness page counts as filled is the album's *resolved* year and genre (stored
by services/resolver.py: the MP3 tag, else what a lookup or the liner notes found). This
module runs the fill, local sources first and the network after:

1. genres the artist already has are copied to its albums (no request),
2. liner notes (and MP3 tag credits) are read, the local model extracting the release date,
3. whatever is still missing is looked up online (artist first, to save requests).

Tags that no lookup can know (title, track number...) are never touched.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.db.models import Q

from library import repository
from library.models import Album
from library.services import enrichment, insights, resolver

FILLABLE = {"date": "year", "genre": "genre"}


# "01 - Title", "01. Title", "01_Title", "(01) Title", "Band - 01 - Title", "2-03 Title" (disc-track)
_NUMBER_AFTER_SEPARATOR = re.compile(r"^\s*\(?(\d{1,3})\)?\s*[-._)\]–]\s*\S")
_NUMBER_BETWEEN_DASHES = re.compile(r"\S\s+[-–]\s*(\d{1,3})\s*[-–.]\s*\S")
_DISC_TRACK = re.compile(r"^\s*\d[-.](\d{2})\b\s*\S")
_NUMBER_THEN_SPACE = re.compile(r"^\s*(\d{1,3})\s+\S")


def track_number_from_filename(file_path, album_track_count=None):
    """The track number a file name carries, or None. A bare "07 Title" counts only when the
    number is plausible for the album ("99 Luftballons" on a 10-track album is a title)."""
    stem = Path(file_path or "").stem
    for pattern in (_DISC_TRACK, _NUMBER_AFTER_SEPARATOR, _NUMBER_BETWEEN_DASHES):
        match = pattern.search(stem)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    match = _NUMBER_THEN_SPACE.match(stem)
    if match and 0 < int(match.group(1)) <= max(album_track_count or 0, 1) + 5:
        return int(match.group(1))
    return None


def fillable_album_ids():
    """Albums missing a value that a lookup could supply (only for attributes being checked)."""
    checked = set(repository.get_config()["quality_attributes"]) & set(FILLABLE)
    missing = Q(pk__in=[])
    if "date" in checked:
        missing |= Q(resolved_year__isnull=True)
    if "genre" in checked:
        missing |= Q(resolved_genre="")
    return list(Album.objects.filter(missing).values_list("pk", flat=True)) if checked else []


@dataclass
class FillStats:
    albums_total: int = 0
    albums_done: int = 0
    albums_before: int = 0
    albums_after: int = 0
    stage: str = ""
    current: str = ""
    extraction_skipped: str = ""
    errors: list = field(default_factory=list)

    @property
    def albums_filled(self):
        return max(0, self.albums_before - self.albums_after)


def run(*, progress=None, should_stop=None, **_ignored):
    stats = FillStats()
    ids = fillable_album_ids()
    stats.albums_total = stats.albums_before = stats.albums_after = len(ids)
    if not ids:
        return stats

    def report(sub):
        stats.current = sub.current
        stats.errors = list(getattr(sub, "errors", []))
        if progress:
            progress(stats)

    stats.stage = "Copying the artists' genres"
    report(stats)
    for album in Album.objects.filter(pk__in=ids).select_related("artist"):
        enrichment.inherit_artist_tags(album)
    resolver.resolve_for_albums(ids)

    ids = fillable_album_ids()
    if ids and not (should_stop and should_stop()):
        stats.stage = "Reading liner notes and tags"
        notes = insights.run(album_ids=ids, extract=True, online_lyrics=False, progress=report, should_stop=should_stop)
        stats.extraction_skipped = notes.extraction_skipped
        stats.errors = list(notes.errors)

    ids = fillable_album_ids()
    if ids and not (should_stop and should_stop()):
        stats.stage = "Looking details up online"
        online = enrichment.enrich_library(album_ids=ids, progress=report, should_stop=should_stop)
        stats.errors += online.errors

    stats.albums_after = len(fillable_album_ids())
    stats.albums_done = stats.albums_total
    stats.stage, stats.current = "", ""
    if progress:
        progress(stats)
    return stats
