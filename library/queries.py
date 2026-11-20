"""ORM queries for the library pages.

The annotations expose the attribute names the templates already use
(album_id, track_name, release_year, ...), so the templates stay unchanged.
"""
from django.db.models import Avg, Count, F, Q
from django.db.models.functions import Lower

from .models import Album, AlbumProfile, Artist, ArtistProfile, Track


def albums(track_filter=None, *, tag_year_only=False):
    """Albums with their track count; ``track_filter`` (a Q on track__*) limits both.

    Year, genre, label and country are the resolved values (the file tag, else what lookups
    found; see services/resolver.py), the same everywhere. ``tag_year_only`` gives the
    year tag alone (for the tag editors).
    """
    queryset = Album.objects.all()
    if track_filter is not None:
        # Filter before annotating so the filter and the count share one join.
        queryset = queryset.filter(track_filter)
    return queryset.annotate(
        album_id=F("id"),
        release_year=F("album_release_year") if tag_year_only else F("resolved_year"),
        genre=F("resolved_genre"),
        label=F("resolved_label"),
        country=F("resolved_country"),
        disc_number=F("album_discnumber"),
        artist_name=F("artist__artist_name"),
        track_count=Count("track"),
    )


def tracks():
    return Track.objects.annotate(
        track_id=F("id"),
        track_name=F("track_title"),
        number=F("resolved_number"),
        track_length=F("track_duration"),
        file_path=F("track_file_path"),
        dq_confidence=F("track_dq_confidence"),
        album_name=F("album__album_name"),
        artist_id=F("album__artist_id"),
        artist_name=F("album__artist__artist_name"),
    )


def artists():
    return Artist.objects.annotate(
        artist_id=F("id"),
        album_count=Count("album", distinct=True),
        track_count=Count("album__track"),
    )


def album_order():
    return [Lower("artist__artist_name"), "resolved_year", Lower("album_name")]


def track_order():
    return [
        Lower("album__artist__artist_name"),
        Lower("album__album_name"),
        "resolved_number",
        Lower("track_title"),
    ]


def dashboard_stats():
    average = Track.objects.aggregate(value=Avg("track_dq_confidence"))["value"]
    return {
        "artist_count": Artist.objects.count(),
        "album_count": Album.objects.count(),
        "track_count": Track.objects.count(),
        "missing_artwork_count": Album.objects.filter(
            Q(album_cover__isnull=True) | Q(album_cover=b"")
        ).count(),
        "missing_year_count": Album.objects.filter(resolved_year__isnull=True).count(),
        "missing_genre_count": Album.objects.filter(resolved_genre="").count(),
        "average_quality": round(average, 2) if average is not None else None,
        "enriched_artist_count": ArtistProfile.objects.filter(status="matched").count(),
        "enriched_album_count": AlbumProfile.objects.filter(status="matched").count(),
    }
