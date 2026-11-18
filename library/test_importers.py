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


class BatchAndPublisherTests(TestCase):
    def test_parse_batch_path_reads_batch_and_publisher(self):
        from datetime import date
        from library.services.files_scanner import parse_batch_path

        path = "/data/20240315/Some Label/Band/Album/01.mp3"
        self.assertEqual(parse_batch_path(path), (date(2024, 3, 15), "Some Label"))
        self.assertEqual(
            parse_batch_path("/data/20240315/Independent/Band/Album/CD1/01.mp3"),
            (date(2024, 3, 15), "Independent"),
        )

    def test_parse_batch_path_ignores_other_layouts(self):
        from library.services.files_scanner import parse_batch_path

        self.assertEqual(parse_batch_path("/data/Band/Album/01.mp3"), (None, ""))
        self.assertEqual(parse_batch_path("/data/20240315/Band/01.mp3"), (None, ""))
        self.assertEqual(parse_batch_path("/data/20241345/Label/Band/Album/01.mp3"), (None, ""))

    def test_folder_publisher_fills_gap_but_tag_leads(self):
        from unittest.mock import MagicMock
        from library.services import files_scanner

        config = {"file_attributes": ["publisher", "title"], "name_fields": []}
        path = "/data/20240315/Folder Label/Band/Album/01.mp3"
        for tag, expected in ((None, "Folder Label"), ("Tag Label", "Tag Label")):
            tags = {"title": ["Song"]}
            if tag:
                tags["publisher"] = [tag]
            with patch.object(files_scanner, "EasyID3", return_value=tags), \
                    patch.object(files_scanner, "MP3", return_value=MagicMock()):
                record = files_scanner.read_file_metadata(path, config)
            self.assertEqual(record["publisher"], expected)
            self.assertEqual(str(record["batch_date"]), "2024-03-15")

    def test_import_stores_batch_date_and_publisher(self):
        from datetime import date
        from library.services.importer import import_records

        import_records([make_record(publisher="Independent", batch_date=date(2024, 3, 15))])
        album = Album.objects.get()
        self.assertEqual(album.album_publisher, "Independent")
        self.assertEqual(album.album_batch_date, date(2024, 3, 15))

    def test_batch_date_is_kept_once_set_and_rescan_touches_nothing(self):
        from datetime import date
        from library.services.importer import import_records

        import_records([make_record(batch_date=date(2024, 3, 15))])
        updated = Album.objects.get().when_updated
        stats = import_records([make_record(batch_date=date(2024, 4, 1))])
        album = Album.objects.get()
        self.assertEqual(album.album_batch_date, date(2024, 3, 15))
        self.assertEqual(album.when_updated, updated)
        self.assertEqual(stats["Album", "unchanged"], 1)

    def test_long_publisher_fits(self):
        from library.services.importer import import_records

        name = "P" * 100
        import_records([make_record(publisher=name)])
        self.assertEqual(Album.objects.get().album_publisher, name)
