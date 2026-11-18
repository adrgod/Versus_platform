"""Loads scanner records into the Django models.

Rows are matched on the model's unique-constraint fields, so loading the same
data again never duplicates anything, and a row (and its ``when_updated``) is
only written when a value really changed.
"""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from library.models import Album, Artist, Track

SUPPORTED_MODELS = ("Artist", "Album", "Track")
TWO_PLACES = Decimal("0.01")
DELETE_CHUNK = 500


def is_blank(value):
    return value is None or (isinstance(value, (str, bytes)) and len(value) == 0)


def clean_text(value):
    return str(value or "").strip()


def clean_int(value):
    """Leading digits of a tag such as '2001-05-03' or '3/12'; None when absent."""
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def clean_decimal(value):
    """Two decimal places, like the model's DecimalFields store them."""
    if is_blank(value):
        return None
    try:
        return Decimal(str(value)).quantize(TWO_PLACES)
    except InvalidOperation:
        return None


def clean_blob(value):
    if isinstance(value, (bytes, bytearray, memoryview)) and len(value) > 0:
        return bytes(value)
    return None


def import_records(records, model_name="Track", on_skip=None):
    """Import scanner records; returns a Counter keyed by (model name, outcome).

    ``model_name`` is the deepest model to load: "Artist", "Album" or "Track".
    Parents are created as needed. Records missing what that level requires are
    skipped (``on_skip(record, reason)`` is called) instead of failing the run.
    """
    if model_name not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported model: {model_name}")
    stats = Counter()
    for record in records:
        _import_record(record, model_name, stats, on_skip)
    return stats


def _import_record(record, model_name, stats, on_skip):
    artist_name = clean_text(record.get("artist") or record.get("albumartist"))
    album_name = clean_text(record.get("album"))
    track_title = clean_text(record.get("title"))

    reason = None
    if not artist_name:
        reason = "missing artist"
    elif model_name != "Artist" and not album_name:
        reason = "missing album"
    elif model_name == "Track" and not track_title:
        reason = "missing title"
    if reason:
        stats["skipped"] += 1
        if on_skip:
            on_skip(record, reason)
        return

    artist = _upsert(
        Artist,
        identity={
            "artist_name": artist_name,
            "artist_country": clean_text(record.get("artist_country") or record.get("country")),
            "artist_formed_year": clean_int(record.get("artist_formed_year")),
        },
        attributes={"artist_photo": clean_blob(record.get("artist_photo"))},
        fill_only={"artist_photo"},
        stats=stats,
    )
    if model_name == "Artist":
        return

    album = _upsert(
        Album,
        identity={
            "artist": artist,
            "album_name": album_name,
            "album_release_year": clean_int(record.get("date") or record.get("originaldate")),
            "album_publisher": clean_text(record.get("publisher")),
        },
        attributes={
            "album_genre": clean_text(record.get("genre")),
            "album_length_class": clean_text(record.get("album_length_class")),
            "album_cover": clean_blob(record.get("album_cover")),
        },
        fill_only={"album_cover"},
        defaults={"album_discnumber": clean_int(record.get("discnumber")) or 1},
        stats=stats,
    )
    if model_name == "Album":
        return

    _upsert(
        Track,
        identity={
            "album": album,
            "track_title": track_title,
            "track_number": clean_int(record.get("tracknumber")),
            "track_duration": clean_decimal(record.get("length") or record.get("track_duration")),
        },
        attributes={
            "track_file_path": clean_text(record.get("file_path")),
            "track_dq_confidence": clean_decimal(record.get("dq_confidence")),
        },
        stats=stats,
    )


def _upsert(model, identity, attributes, stats, fill_only=(), defaults=None):
    """Find the row by its unique-constraint fields; create it or update only what changed.

    Blank incoming values never overwrite stored ones. ``fill_only`` fields are
    only written while empty. ``when_updated`` (auto_now) is only bumped when a
    field really changes, because save() is skipped otherwise.
    """
    label = model.__name__
    obj = model.objects.filter(**identity).first()

    if obj is None:
        values = {key: value for key, value in attributes.items() if not is_blank(value)}
        obj = model.objects.create(**identity, **(defaults or {}), **values)
        stats[label, "created"] += 1
        return obj

    changed = []
    for field, value in attributes.items():
        if is_blank(value):
            continue
        current = getattr(obj, field)
        if isinstance(current, memoryview):
            current = bytes(current)
        if field in fill_only and not is_blank(current):
            continue
        if current != value:
            setattr(obj, field, value)
            changed.append(field)

    if changed:
        obj.save(update_fields=[*changed, "when_updated"])
        stats[label, "updated"] += 1
    else:
        stats[label, "unchanged"] += 1
    return obj


def prune_empty():
    """Delete albums without tracks, then artists without albums."""
    Album.objects.filter(track__isnull=True).delete()
    Artist.objects.filter(album__isnull=True).delete()


def remove_missing_tracks(source_folder, scanned_paths):
    """Delete tracks under ``source_folder`` whose files were not in the latest scan."""
    source_root = Path(source_folder).resolve()
    scanned = {str(Path(path).resolve()) for path in scanned_paths}

    stale_ids = []
    stored = Track.objects.exclude(track_file_path="").values_list("pk", "track_file_path")
    for track_id, file_path in stored:
        existing = Path(file_path).resolve()
        if (existing == source_root or source_root in existing.parents) and str(existing) not in scanned:
            stale_ids.append(track_id)

    for start in range(0, len(stale_ids), DELETE_CHUNK):
        Track.objects.filter(pk__in=stale_ids[start:start + DELETE_CHUNK]).delete()
    prune_empty()
    return len(stale_ids)
