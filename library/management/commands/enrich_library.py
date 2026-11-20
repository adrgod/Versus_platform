from django.core.management.base import BaseCommand

from library.models import Artist
from library.services.enrichment import enrich_library
from library.services.musicbrainz import MusicBrainzClient


class Command(BaseCommand):
    help = (
        "Lookup MusicBrainz: look artists and albums up on MusicBrainz and store genres, country, formation year, "
        "line-up, studios, credits, discography and ratings. Tags read from the files are never changed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--refresh", action="store_true",
                            help="Look everything up again, not only artists and albums not looked up yet.")
        parser.add_argument("--artist", action="append", default=[],
                            help="Only this artist (name contains the text). Can be repeated.")
        parser.add_argument("--no-cache", action="store_true",
                            help="Ignore saved MusicBrainz answers and ask the server again.")

    def handle(self, *args, **options):
        artist_ids = None
        if options["artist"]:
            artist_ids = []
            for text in options["artist"]:
                artist_ids += list(Artist.objects.filter(artist_name__icontains=text).values_list("pk", flat=True))
            if not artist_ids:
                self.stderr.write("No artist matches that name.")
                return

        def progress(stats):
            if stats.current:
                self.stdout.write(f"[{stats.artists_done + 1}/{stats.artists_total}] {stats.current}")

        stats = enrich_library(
            MusicBrainzClient(use_cache=not options["no_cache"]),
            refresh=options["refresh"],
            artist_ids=artist_ids,
            progress=progress,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Artists: {stats.artists_matched} matched, {stats.artists_not_found} not found. "
            f"Albums: {stats.albums_matched} matched, {stats.albums_not_found} not found."
        ))
        for error in stats.errors:
            self.stderr.write(error)
