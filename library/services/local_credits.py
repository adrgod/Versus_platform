"""Credits read from the user's own files, no network: MP3 tags and (via insights) liner notes.

Each value is stored with its source so the album page can say where it came from.
"""
import re
from collections import Counter
from pathlib import Path

from mutagen.easyid3 import EasyID3

from library.models import AlbumCredit, FacetKind, Track

SOURCE_LABELS = {"musicbrainz": "MusicBrainz", "tags": "MP3 tags", "notes": "Liner notes"}
# EasyID3 key -> credit kind
TAG_KINDS = {"composer": FacetKind.COMPOSER, "lyricist": FacetKind.LYRICIST}
SPLIT = re.compile(r"\s*[;/]\s*")


def _tag_values(path, key):
    try:
        values = EasyID3(path).get(key) or []
    except Exception:  # unreadable or not an MP3: just no data
        return []
    names = []
    for value in values:
        names += [part.strip() for part in SPLIT.split(str(value)) if part.strip()]
    return names


def refresh_tag_credits(album, file_paths=None):
    """Replace the album's tag-sourced credits with what its files say; returns how many."""
    if file_paths is None:
        file_paths = list(Track.objects.filter(album=album).values_list("track_file_path", flat=True))
    counters = {kind: Counter() for kind in TAG_KINDS.values()}
    for path in file_paths:
        if not path or not Path(path).exists():
            continue
        for key, kind in TAG_KINDS.items():
            for name in set(_tag_values(path, key)):
                counters[kind][name] += 1
    AlbumCredit.objects.filter(album=album, source=AlbumCredit.SOURCE_TAGS).delete()
    rows = [
        AlbumCredit(album=album, kind=kind, name=name, source=AlbumCredit.SOURCE_TAGS,
                    evidence=f"{key.title()} tag on {count} track{'s' if count != 1 else ''}", count=count)
        for key, kind in TAG_KINDS.items()
        for name, count in counters[kind].items()
    ]
    AlbumCredit.objects.bulk_create(rows)
    return len(rows)
