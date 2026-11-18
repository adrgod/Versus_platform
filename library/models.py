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
    album_publisher = models.CharField(max_length=30, blank=True)
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
    track_number = models.IntegerField(null=True, blank=True)
    track_duration = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    track_dq_confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    track_file_path = models.CharField(max_length=500, blank=False)
    when_loaded = models.DateTimeField(auto_now_add=True)
    when_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields= ["album", "track_title", "track_number", "track_duration"],
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
