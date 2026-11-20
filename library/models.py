from pathlib import Path

from django.db import models

# Create your models here.
class Artist(models.Model):
    artist_name = models.CharField(max_length=100, blank=False)
    artist_country = models.CharField(max_length=30, blank=True)
    artist_formed_year = models.IntegerField(null=True, blank=True)
    artist_photo = models.BinaryField(editable=True, null=True, blank=True)
    # The value every page shows: the file tag, else what lookups found. Rebuilt by
    # services/resolver.py; ``resolved_sources`` says where each value came from.
    resolved_country = models.CharField(max_length=100, blank=True)
    resolved_formed_year = models.IntegerField(null=True, blank=True)
    resolved_sources = models.JSONField(default=dict, blank=True)
    when_loaded = models.DateTimeField(auto_now_add=True)
    when_updated = models.DateTimeField(auto_now=True)

    class Meta:
                constraints = [
                    models.UniqueConstraint(
                        fields= ["artist_name", "artist_country", "artist_formed_year"],
                        name="unique_artist_country_year",
                    ),
                ]

class Album(models.Model):
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE)
    album_name = models.CharField(max_length=200, blank=False)
    album_discnumber = models.IntegerField(default=1)
    album_release_year = models.IntegerField(null=True, blank=True)
    album_genre = models.CharField(max_length=50, blank=True)
    album_cover = models.BinaryField(editable=True, null=True, blank=True)
    album_publisher = models.CharField(max_length=100, blank=True)
    album_batch_date = models.DateField(null=True, blank=True)
    album_length_class = models.CharField(max_length=10, blank=True)
    # What every page shows (see Artist.resolved_*): the file tag, else what lookups found.
    resolved_year = models.IntegerField(null=True, blank=True)
    resolved_genre = models.CharField(max_length=200, blank=True)
    resolved_label = models.CharField(max_length=100, blank=True)
    resolved_country = models.CharField(max_length=100, blank=True)
    resolved_credits = models.JSONField(default=dict, blank=True)
    resolved_sources = models.JSONField(default=dict, blank=True)
    when_loaded = models.DateTimeField(auto_now_add=True)
    when_updated = models.DateTimeField(auto_now=True)

    class Meta:
            constraints = [
                models.UniqueConstraint(
                    fields= ["artist", "album_name", "album_release_year", "album_publisher"],
                    name="unique_album_year_publisher",
                ),
            ]

class Track(models.Model):
    album = models.ForeignKey(Album, on_delete=models.CASCADE)
    track_title = models.CharField(max_length=200, blank=False)
    track_discnumber = models.IntegerField(default=1)
    track_number = models.IntegerField(null=True, blank=True)
    track_duration = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    track_dq_confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    track_file_path = models.CharField(max_length=500, blank=False)
    resolved_number = models.IntegerField(null=True, blank=True)  # tag, else the file name
    resolved_sources = models.JSONField(default=dict, blank=True)
    when_loaded = models.DateTimeField(auto_now_add=True)
    when_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields= ["album", "track_discnumber", "track_title", "track_number", "track_duration"],
                name="unique_track_title_number_duration_per_album",
            ),
        ]

class LibraryState(models.Model):
    """Single-row record of the chosen source folder and the last scan's outcome."""

    source_folder = models.CharField(max_length=1000, blank=True)
    last_scan_started = models.DateTimeField(null=True, blank=True)
    last_scan_finished = models.DateTimeField(null=True, blank=True)
    last_scan_status = models.CharField(max_length=20, default="never")
    scan_error = models.TextField(null=True, blank=True)
    scanned_track_count = models.IntegerField(default=0)

    @classmethod
    def load(cls):
        from library import repository

        state, _ = cls.objects.get_or_create(
            pk=1, defaults={"source_folder": str(repository.default_source_folder())}
        )
        return state


# ---------------------------------------------------------------------------
# Online enrichment (MusicBrainz). Kept apart from the tag-derived fields above:
# Artist.artist_country / artist_formed_year are part of the importer's identity
# key, so online data never writes to them.
# ---------------------------------------------------------------------------

class EnrichmentStatus(models.TextChoices):
    PENDING = "", "Not looked up"
    MATCHED = "matched", "Matched"
    NOT_FOUND = "not_found", "No match"
    ERROR = "error", "Lookup failed"


class ArtistProfile(models.Model):
    """What an online source says about an artist."""

    artist = models.OneToOneField(Artist, on_delete=models.CASCADE, related_name="profile")
    source = models.CharField(max_length=20, default="musicbrainz")
    external_id = models.CharField(max_length=36, blank=True, db_index=True)
    id_set_manually = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    match_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    name = models.CharField(max_length=200, blank=True)
    disambiguation = models.CharField(max_length=200, blank=True)
    artist_type = models.CharField(max_length=30, blank=True)
    country = models.CharField(max_length=100, blank=True)
    country_code = models.CharField(max_length=2, blank=True)
    begin_area = models.CharField(max_length=100, blank=True)
    begin_year = models.IntegerField(null=True, blank=True)
    end_year = models.IntegerField(null=True, blank=True)
    rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    rating_votes = models.IntegerField(default=0)
    studio_album_count = models.IntegerField(null=True, blank=True)
    discography = models.JSONField(default=list, blank=True)
    lineup = models.JSONField(default=list, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True)


class AlbumProfile(models.Model):
    """What an online source says about an album (a MusicBrainz release group)."""

    album = models.OneToOneField(Album, on_delete=models.CASCADE, related_name="profile")
    source = models.CharField(max_length=20, default="musicbrainz")
    external_id = models.CharField(max_length=36, blank=True, db_index=True)
    release_id = models.CharField(max_length=36, blank=True)
    id_set_manually = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    match_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    title = models.CharField(max_length=300, blank=True)
    primary_type = models.CharField(max_length=30, blank=True)
    secondary_types = models.CharField(max_length=200, blank=True)
    first_release_date = models.CharField(max_length=10, blank=True)
    release_year = models.IntegerField(null=True, blank=True)
    rating = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    rating_votes = models.IntegerField(default=0)
    credits = models.JSONField(default=list, blank=True)
    country = models.CharField(max_length=100, blank=True)  # copied from the artist
    enriched_at = models.DateTimeField(null=True, blank=True)


class MetalArchivesArtist(models.Model):
    """What Metal Archives (metal-archives.com) says about a band. Kept apart from the MusicBrainz profile."""

    artist = models.OneToOneField(Artist, on_delete=models.CASCADE, related_name="metal_archives")
    ma_id = models.IntegerField(null=True, blank=True)
    url = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    note = models.CharField(max_length=300, blank=True)
    name = models.CharField(max_length=200, blank=True)
    country = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=200, blank=True)
    formed_year = models.IntegerField(null=True, blank=True)
    band_status = models.CharField(max_length=30, blank=True)
    genre_text = models.CharField(max_length=300, blank=True)
    label = models.CharField(max_length=100, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True)


class MetalArchivesAlbum(models.Model):
    """What Metal Archives says about an album (a release of the matched band)."""

    album = models.OneToOneField(Album, on_delete=models.CASCADE, related_name="metal_archives")
    ma_id = models.IntegerField(null=True, blank=True)
    url = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    note = models.CharField(max_length=300, blank=True)
    title = models.CharField(max_length=300, blank=True)
    release_type = models.CharField(max_length=30, blank=True)
    release_date = models.CharField(max_length=40, blank=True)
    release_year = models.IntegerField(null=True, blank=True)
    label = models.CharField(max_length=100, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True)


class DiscogsAlbum(models.Model):
    """What Discogs says about an album: the matched release, its credits, year and label."""

    album = models.OneToOneField(Album, on_delete=models.CASCADE, related_name="discogs")
    release_id = models.IntegerField(null=True, blank=True)
    url = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    note = models.CharField(max_length=300, blank=True)
    title = models.CharField(max_length=300, blank=True)
    release_year = models.IntegerField(null=True, blank=True)
    label = models.CharField(max_length=100, blank=True)
    credits = models.JSONField(default=list, blank=True)  # same shape as AlbumProfile.credits
    enriched_at = models.DateTimeField(null=True, blank=True)


class FacetKind(models.TextChoices):
    GENRE = "genre", "Genre"
    MEMBER = "member", "Band member"
    INSTRUMENT = "instrument", "Instrument"
    STUDIO = "studio", "Recording studio"
    PRODUCER = "producer", "Producer"
    ENGINEER = "engineer", "Sound engineer"
    ARTWORK = "artwork", "Artwork"
    LABEL = "label", "Label"
    COMPOSER = "composer", "Composer"
    LYRICIST = "lyricist", "Lyricist"
    MUSICIAN = "musician", "Album musician"
    # From liner notes, biographies and lyrics (see services/insights.py)
    KEYWORD = "keyword", "Album keyword"
    MOOD = "mood", "Mood"
    LYRIC_THEME = "lyric_theme", "Lyric theme"
    BAND_EVENT = "band_event", "Band history"
    CAUSE = "cause", "Reason for change"
    HIATUS = "hiatus", "Hiatus length"


class Facet(models.Model):
    """A value people can browse by: one genre, one studio, one person in one role..."""

    kind = models.CharField(max_length=20, choices=FacetKind.choices)
    name = models.CharField(max_length=200)
    external_id = models.CharField(max_length=36, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "name"], name="unique_facet_kind_name"),
        ]


class ArtistFacet(models.Model):
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="facet_links")
    facet = models.ForeignKey(Facet, on_delete=models.CASCADE, related_name="artist_links")
    source = models.CharField(max_length=20, default="musicbrainz")
    detail = models.CharField(max_length=300, blank=True)
    weight = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["artist", "facet", "source"], name="unique_artist_facet_source"),
        ]


class AlbumFacet(models.Model):
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="facet_links")
    facet = models.ForeignKey(Facet, on_delete=models.CASCADE, related_name="album_links")
    source = models.CharField(max_length=20, default="musicbrainz")
    detail = models.CharField(max_length=300, blank=True)
    weight = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["album", "facet", "source"], name="unique_album_facet_source"),
        ]


# ---------------------------------------------------------------------------
# Liner notes, lyrics and what a local language model reads out of them
# ---------------------------------------------------------------------------

class AlbumDocument(models.Model):
    """A text, PDF or NFO file found in the album folder (liner notes, press sheet, biography)."""

    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="documents")
    file_path = models.CharField(max_length=1000)
    file_type = models.CharField(max_length=10)
    text = models.TextField(blank=True)
    file_mtime_ns = models.BigIntegerField(default=0)
    file_size = models.BigIntegerField(default=0)
    release_date = models.CharField(max_length=10, blank=True)
    when_loaded = models.DateTimeField(auto_now_add=True)
    when_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["album", "file_path"], name="unique_album_document_path"),
        ]

    @property
    def file_name(self):
        return Path(self.file_path).name


class TrackLyrics(models.Model):
    track = models.OneToOneField(Track, on_delete=models.CASCADE, related_name="lyrics")
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    source = models.CharField(max_length=20, blank=True)  # tag, file or lrclib
    text = models.TextField(blank=True)
    synced = models.BooleanField(default=False)
    instrumental = models.BooleanField(default=False)
    note = models.CharField(max_length=300, blank=True)
    when_updated = models.DateTimeField(auto_now=True)


class AlbumInsight(models.Model):
    """Facts a local language model read out of an album's notes and lyrics."""

    album = models.OneToOneField(Album, on_delete=models.CASCADE, related_name="insight")
    status = models.CharField(max_length=20, choices=EnrichmentStatus.choices, blank=True, default="")
    note = models.CharField(max_length=300, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    input_hash = models.CharField(max_length=64, blank=True)
    summary = models.TextField(blank=True)
    release_date = models.CharField(max_length=10, blank=True)
    keywords = models.JSONField(default=list, blank=True)
    moods = models.JSONField(default=list, blank=True)
    lyric_themes = models.JSONField(default=list, blank=True)
    events = models.JSONField(default=list, blank=True)
    extracted_at = models.DateTimeField(null=True, blank=True)
    llm_duration_seconds = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)


class AlbumCredit(models.Model):
    """A credit found in the user's own files (MP3 tags, liner notes), with where it came from."""

    SOURCE_TAGS, SOURCE_NOTES = "tags", "notes"

    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="local_credits")
    kind = models.CharField(max_length=20, choices=FacetKind.choices)
    name = models.CharField(max_length=200)
    detail = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=20)
    evidence = models.CharField(max_length=300, blank=True)  # the quote or tag it was read from
    count = models.IntegerField(default=0)


# -- per user: preferences, play history, ratings ------------------------------------

class UserPreference(models.Model):
    """What one logged-in user chose, so it follows them to every browser and PC."""
    user = models.OneToOneField("auth.User", on_delete=models.CASCADE, related_name="library_preference")
    display = models.JSONField(default=dict, blank=True)  # theme, accent, grid, corners, motion
    eq = models.JSONField(default=dict, blank=True)  # {"60": 0, "250": 3, ...} in dB
    volume = models.FloatField(null=True, blank=True)  # 0..1
    display_name = models.CharField(max_length=100, blank=True, default="")  # shown on reviews
    updated_at = models.DateTimeField(auto_now=True)


class PlayEvent(models.Model):
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="library_plays")
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="plays")
    played_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-played_at", "-id"]


class TrackRating(models.Model):
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="library_track_ratings")
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="ratings")
    stars = models.PositiveSmallIntegerField()  # 1..5
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "track"], name="one_rating_per_user_track")]


class AlbumRating(models.Model):
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="library_album_ratings")
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="ratings")
    stars = models.PositiveSmallIntegerField()  # 1..5
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "album"], name="one_rating_per_user_album")]


class Review(models.Model):
    """One user's written review and 1-10 score of an album."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="library_reviews")
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name="reviews")
    author_name = models.CharField(max_length=100)  # the name shown with the review
    score = models.PositiveSmallIntegerField()  # 1..10
    body_html = models.TextField(blank=True)  # sanitized, see library/reviews.py
    body_text = models.TextField(blank=True)  # the same without formatting, for searching
    written_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "album"], name="one_review_per_user_album")]
