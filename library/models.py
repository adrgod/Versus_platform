from django.db import models

# Create your models here.
class Artist(models.Model):
    artist_name = models.CharField(max_length=100, blank=False)
    artist_country = models.CharField(max_length=30, blank=True)
    artist_formed_year = models.IntegerField(null=True, blank=True)
    artist_photo = models.BinaryField(editable=True, null=True, blank=True)
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
    MUSICIAN = "musician", "Album musician"


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
