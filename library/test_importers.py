from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from library.models import Album, Artist, Track


def make_record(**overrides):
    record = {
        "file_path": "/music/a.mp3",
        "artist": "Artist1",
        "artist_country": "PT",
        "artist_formed_year": "1990",
        "artist_photo": "",
        "album": "Album1",
        "date": "2000-05-01",
        "publisher": "Label",
        "genre": "Rock",
        "discnumber": "1",
        "album_cover": None,
        "album_length_class": "LP",
        "title": "Song",
        "tracknumber": "1",
        "length": 123.456,
        "dq_confidence": 0.9,
    }
    record.update(overrides)
    return record


class ImportMusicCommandTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def run_import(self, records, model="library.Track", **options):
        target = "library.management.commands.import_music.read_all_metadata"
        with patch(target, return_value=records):
            call_command("import_music", self.directory.name, model=model, verbosity=0, **options)

    def counts(self):
        return Artist.objects.count(), Album.objects.count(), Track.objects.count()

    def stamps(self):
        return [
            (obj.when_loaded, obj.when_updated)
            for obj in (Artist.objects.get(), Album.objects.get(), Track.objects.get())
        ]

    def test_second_load_creates_nothing_and_touches_no_dates(self):
        self.run_import([make_record()])
        before = self.stamps()
        self.run_import([make_record()])
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.stamps(), before)

    def test_missing_year_and_track_number_do_not_duplicate(self):
        record = make_record(date="", tracknumber="", artist_formed_year="")
        for _ in range(3):
            self.run_import([record])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_changed_value_updates_only_that_row(self):
        self.run_import([make_record()])
        before = self.stamps()
        self.run_import([make_record(genre="Jazz")])
        after = self.stamps()
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[2], before[2])
        self.assertEqual(Album.objects.get().album_genre, "Jazz")
        self.assertEqual(after[1][0], before[1][0])
        self.assertGreater(after[1][1], before[1][1])

    def test_blank_tag_does_not_erase_stored_value(self):
        self.run_import([make_record()])
        self.run_import([make_record(genre="")])
        self.assertEqual(Album.objects.get().album_genre, "Rock")

    def test_dry_run_saves_nothing(self):
        self.run_import([make_record()], dry_run=True)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_record_without_album_is_skipped_not_fatal(self):
        self.run_import([make_record(album=""), make_record(title="Other", tracknumber="2")])
        self.assertEqual(self.counts(), (1, 1, 1))
