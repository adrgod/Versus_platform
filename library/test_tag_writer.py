from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from mutagen.id3 import ID3, TALB
from PIL import Image

from library.models import Album, Track
from library.services import tag_writer
from library.services.files_scanner import derived_tags, parse_track_filename
from library.services.importer import import_records

MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)


def audio_bytes(path):
    data = Path(path).read_bytes()
    return data[tag_writer.id3_header_length(data[:10]):]


class PathRuleTests(TestCase):
    def test_track_filename_patterns(self):
        for name, expected in (
            ("01 - Track Name.mp3", (1, "Track Name")),
            ("01_Track_Name.mp3", (1, "Track Name")),
            ("7. Seven.mp3", (7, "Seven")),
            ("12-Twelve_Song.mp3", (12, "Twelve Song")),
            ("21 Guns.mp3", (None, "")),
            ("Just A Name.mp3", (None, "")),
            ("00 - Zero.mp3", (None, "")),
            ("1999.mp3", (None, "")),
        ):
            self.assertEqual(parse_track_filename(name), expected, name)

    def test_derived_tags_need_the_batch_layout(self):
        path = "/m/20240315/Label/Band/Album/CD2/03 - Song.mp3"
        self.assertEqual(derived_tags(path), {
            "publisher": "Label", "artist": "Band", "album": "Album",
            "tracknumber": "3", "title": "Song", "discnumber": "2",
        })
        self.assertEqual(derived_tags("/m/Band/Album/03 - Song.mp3"), {})
        self.assertEqual(derived_tags("/m/20240315/Band/Album/03 - Song.mp3"), {})


class TagWriterTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        roots = patch("library.repository.get_allowed_roots", return_value=[self.root])
        roots.start()
        self.addCleanup(roots.stop)
        self.album_dir = self.root / "20240315" / "Label" / "Band" / "My Album"
        (self.album_dir / "mp3s").mkdir(parents=True)
        (self.album_dir / "photos").mkdir()
        Image.new("RGB", (800, 800), "blue").save(self.album_dir / "photos" / "cover.jpg")
        self.track = self.album_dir / "mp3s" / "01 - Intro_Song.mp3"
        self.track.write_bytes(MP3_FRAME * 10)

    def tag_file(self, **frames):
        tags = ID3()
        for frame in frames.values():
            tags.add(frame)
        tags.save(self.track, v2_version=3)

    def test_only_empty_tags_are_filled(self):
        self.tag_file(album=TALB(encoding=3, text=["Existing Album"]))
        audio_before = audio_bytes(self.track)

        plan = tag_writer.plan_file(self.track)
        self.assertEqual(plan.changes, {
            "artist": "Band", "title": "Intro Song", "tracknumber": "1", "publisher": "Label",
        })
        self.assertTrue(plan.cover)
        tag_writer.write_file(plan)

        tags = ID3(self.track)
        self.assertEqual(str(tags["TALB"]), "Existing Album")
        self.assertEqual(str(tags["TPE1"]), "Band")
        self.assertEqual(str(tags["TIT2"]), "Intro Song")
        self.assertEqual(str(tags["TRCK"]), "1")
        self.assertEqual(str(tags["TPUB"]), "Label")
        self.assertTrue(tags.getall("APIC"))
        self.assertEqual(tags.version[:2], (2, 3))
        self.assertEqual(audio_bytes(self.track), audio_before)

    def test_second_run_finds_nothing_to_do(self):
        tag_writer.write_file(tag_writer.plan_file(self.track))
        self.assertTrue(tag_writer.plan_file(self.track).is_empty)

    def test_file_outside_layout_is_left_alone(self):
        other = self.root / "loose" / "01 - Song.mp3"
        other.parent.mkdir()
        other.write_bytes(MP3_FRAME * 10)
        self.assertIsNone(tag_writer.plan_file(other))

    def test_failed_verification_puts_original_file_back(self):
        self.tag_file(album=TALB(encoding=3, text=["Existing Album"]))
        original = self.track.read_bytes()
        plan = tag_writer.plan_file(self.track)
        with patch.object(tag_writer.TagSnapshot, "audio_unchanged", return_value=False):
            with self.assertRaises(ValueError):
                tag_writer.write_file(plan)
        self.assertEqual(self.track.read_bytes(), original)

    def test_failed_verification_on_file_without_tags_removes_what_was_added(self):
        original = self.track.read_bytes()
        plan = tag_writer.plan_file(self.track)
        with patch.object(tag_writer.TagSnapshot, "audio_unchanged", return_value=False):
            with self.assertRaises(ValueError):
                tag_writer.write_file(plan)
        self.assertEqual(self.track.read_bytes(), original)

    def import_track(self):
        import_records([{
            "artist": "Band", "album": "My Album", "title": "Intro Song", "tracknumber": "1",
            "date": "2000", "publisher": "Label", "batch_date": date(2024, 3, 15),
            "length": "10", "file_path": str(self.track),
        }])

    def test_batch_job_writes_files_and_logs_without_backups(self):
        self.import_track()
        job = tag_writer.BatchWriteJob(day=date(2024, 3, 15))
        log_dir = self.root / "logs"
        with patch.object(tag_writer, "connection", MagicMock()), \
                patch("library.repository.get_log_directory", return_value=log_dir):
            tag_writer._run(job)
        self.assertEqual((job.total, job.written, job.unchanged, job.failed), (1, 1, 0, []))
        self.assertEqual(str(ID3(self.track)["TPE1"]), "Band")
        self.assertEqual(len(list(log_dir.glob("batch-20240315-*.csv"))), 1)
        self.assertEqual(list(self.root.rglob("*.bak")), [])

    def test_batch_pages(self):
        self.import_track()
        user = get_user_model().objects.create_user(username="u", password="x")
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)

        index = client.get(reverse("library:batch-index"))
        self.assertContains(index, "15 Mar 2024")
        detail = client.get(reverse("library:batch-detail", args=("20240315",)))
        self.assertContains(detail, "My Album")
        self.assertEqual(client.get(reverse("library:batch-detail", args=("20990101",))).status_code, 404)
        self.assertEqual(client.get(reverse("library:batch-detail", args=("abc",))).status_code, 404)

        before = self.track.read_bytes()
        preview = client.get(reverse("library:batch-preview", args=("20240315",)))
        self.assertContains(preview, "Intro Song")
        self.assertEqual(self.track.read_bytes(), before)  # a preview writes nothing

        with patch("library.views.start_batch_write", return_value=True) as start:
            response = client.post(reverse("library:batch-write", args=("20240315",)))
        self.assertEqual(response.status_code, 302)
        start.assert_called_once_with(date(2024, 3, 15))
        self.assertEqual(client.get(reverse("library:batch-write", args=("20240315",))).status_code, 405)

        album = Album.objects.get()
        page = client.get(reverse("library:album-detail", args=(album.pk,)))
        self.assertContains(page, "LAST UPDATED")
        self.assertContains(page, "BATCH 15 Mar 2024")


class UntaggedFileTests(TestCase):
    def test_file_without_any_tags_is_read_and_filled_from_the_path(self):
        from library import repository
        from library.services.files_scanner import read_all_metadata

        with TemporaryDirectory() as directory:
            folder = Path(directory) / "20240315" / "Label" / "Band" / "My Album" / "CD2"
            folder.mkdir(parents=True)
            track = folder / "03_Some_Song.mp3"
            track.write_bytes(MP3_FRAME * 10)
            record = read_all_metadata(str(track), repository.get_config())[0]
        self.assertEqual(
            {key: record[key] for key in ("artist", "album", "title", "tracknumber", "publisher", "discnumber")},
            {"artist": "Band", "album": "My Album", "title": "Some Song", "tracknumber": "3",
             "publisher": "Label", "discnumber": "2"},
        )
        self.assertEqual(str(record["batch_date"]), "2024-03-15")
