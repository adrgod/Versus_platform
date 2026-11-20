from django.core.management.base import BaseCommand

from library.models import Artist
from library.services import insights


class Command(BaseCommand):
    help = (
        "Enrich Data with AI: read liner notes (.txt/.nfo/.pdf in album folders) and lyrics (MP3 tag, lyrics file, LRCLIB), "
        "then let a local Ollama model extract keywords, moods, lyric themes and band history."
    )

    def add_arguments(self, parser):
        parser.add_argument("--refresh", action="store_true",
                            help="Read and extract everything again, even albums whose text has not changed.")
        parser.add_argument("--artist", action="append", default=[],
                            help="Only albums of this artist (name contains the text). Can be repeated.")
        parser.add_argument("--no-extract", action="store_true",
                            help="Only read notes and lyrics; do not call the local model.")
        parser.add_argument("--no-online-lyrics", action="store_true",
                            help="Do not look lyrics up on LRCLIB.")

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
                self.stdout.write(f"[{stats.albums_done + 1}/{stats.albums_total}] {stats.current}")

        stats = insights.run(
            refresh=options["refresh"],
            artist_ids=artist_ids,
            extract=not options["no_extract"],
            online_lyrics=not options["no_online_lyrics"],
            progress=progress,
        )
        if stats.extraction_skipped:
            self.stderr.write(f"Facts not extracted: {stats.extraction_skipped}")
        self.stdout.write(self.style.SUCCESS(
            f"{stats.albums_done} albums: {stats.albums_with_notes} with notes, "
            f"{stats.tracks_with_lyrics} tracks with lyrics ({stats.lyrics_found_online} from LRCLIB), "
            f"{stats.albums_extracted} read by the model."
        ))
        for error in stats.errors:
            self.stderr.write(error)
