from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import StreamingHttpResponse
from django.test import Client, TestCase
from django.urls import reverse

from . import repository, views
from .models import Album, Artist, LibraryState, Track
from .scan_service import scan_library
from .services.importer import import_records


class LibraryWorkflowTests(TestCase):
    def setUp(self):
        self.temp_directory = TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name) / "music"
        self.root.mkdir()
        self.track_path = self.root / "track.mp3"
        self.track_path.write_bytes(b"0123456789abcdef")

        roots_patch = patch("library.repository.get_allowed_roots", return_value=[self.root])
        roots_patch.start()
        self.addCleanup(roots_patch.stop)

        import_records([self.record()])
        state = LibraryState.load()
        state.source_folder = str(self.root)
        state.save()

        self.user = get_user_model().objects.create_user(
            username="library-test-user",
            password="not-a-real-password",
        )
        self.client = Client(HTTP_HOST="localhost")
        self.client.force_login(self.user)

    def record(self, **overrides):
        record = {
            "artist": "Test Artist",
            "album": "Test Album",
            "discnumber": "1",
            "date": "2001",
            "genre": "Test Genre",
            "album_cover": None,
            "title": "Test Track",
            "tracknumber": "1",
            "length": "120.0",
            "dq_confidence": 0.8,
            "file_path": str(self.track_path),
        }
        record.update(overrides)
        return record

    def test_dashboard_and_catalog_use_music_database(self):
        dashboard_response = self.client.get(reverse("library:dashboard"))
        album_response = self.client.get(reverse("library:album-list"))
        quality_response = self.client.get(reverse("library:quality-report"))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_response.context["stats"]["track_count"], 1)
        self.assertEqual(album_response.status_code, 200)
        self.assertContains(album_response, "Test Album")
        self.assertEqual(quality_response.status_code, 200)
        self.assertContains(quality_response, "Test Track")

    def test_artist_and_album_pages_show_the_loaded_music(self):
        artist = Artist.objects.get()
        album = Album.objects.get()
        self.assertContains(self.client.get(reverse("library:artist-list")), "Test Artist")
        self.assertContains(self.client.get(reverse("library:artist-detail", args=(artist.pk,))), "Test Album")
        detail = self.client.get(reverse("library:album-detail", args=(album.pk,)))
        self.assertContains(detail, "Test Track")
        self.assertContains(detail, "2001")
        self.assertEqual(self.client.get(reverse("library:album-detail", args=(9999,))).status_code, 404)

    def test_browse_cloud_offers_music_genre_and_loaded_date(self):
        response = self.client.get(reverse("library:browse"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Music genre")
        self.assertContains(response, "Loaded date")

        genre_response = self.client.get(reverse("library:genre-index"))
        self.assertEqual(genre_response.status_code, 200)
        self.assertContains(genre_response, "Test Genre")

        filtered_response = self.client.get(reverse("library:genre-index"), {"q": "Test"})
        self.assertEqual(filtered_response.status_code, 200)
        self.assertContains(filtered_response, "Test Genre")

    def test_genre_page_lists_artist_album_tracks_and_queue_actions(self):
        response = self.client.get(reverse("library:genre-detail"), {"name": "Test Genre"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Artist")
        self.assertContains(response, "Test Album")
        self.assertContains(response, "Test Track")
        self.assertContains(response, "data-enqueue-playlist")
        self.assertContains(response, "data-queue-track")

    def test_loaded_date_index_and_detail_show_matching_tracks_and_queue(self):
        index_response = self.client.get(reverse("library:loaded-date-index"))
        self.assertEqual(index_response.status_code, 200)
        loaded_day = Track.objects.get().when_loaded.date().isoformat()
        self.assertContains(index_response, loaded_day)

        detail_response = self.client.get(reverse("library:loaded-date-detail"), {"day": loaded_day})
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Test Artist")
        self.assertContains(detail_response, "Test Album")
        self.assertContains(detail_response, "Test Track")
        self.assertContains(detail_response, "data-enqueue-playlist")
        self.assertContains(detail_response, "data-queue-track")

        missing = self.client.get(reverse("library:loaded-date-detail"), {"day": "1999-01-01"})
        self.assertEqual(missing.status_code, 404)

    def test_library_pages_require_login(self):
        self.client.logout()
        response = self.client.get(reverse("library:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_stream_supports_byte_ranges_and_rejects_outside_files(self):
        track = Track.objects.get()
        response = self.client.get(
            reverse("library:stream-track", args=(track.pk,)),
            HTTP_RANGE="bytes=3-6",
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(
            b"".join(cast(Iterable[bytes], cast(StreamingHttpResponse, response).streaming_content)),
            b"3456",
        )
        self.assertEqual(response["Content-Range"], "bytes 3-6/16")

        Track.objects.update(track_file_path=str(self.root.parent / "outside.mp3"))
        blocked = self.client.get(reverse("library:stream-track", args=(track.pk,)))
        self.assertEqual(blocked.status_code, 404)

    def test_folder_selection_is_confined_to_allowed_root(self):
        child = self.root / "subfolder"
        child.mkdir()
        response = self.client.post(reverse("library:choose-folder"), {"folder": str(child)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(LibraryState.load().source_folder, str(child.resolve()))

        outside = self.root.parent / "outside"
        outside.mkdir()
        self.client.post(reverse("library:choose-folder"), {"folder": str(outside)})
        self.assertEqual(LibraryState.load().source_folder, str(child.resolve()))

    def test_scan_records_status_and_keeps_track_path(self):
        with patch("library.scan_service.read_all_metadata", return_value=[self.record()]):
            count, errors = scan_library(self.root)
        self.assertEqual(count, 1)
        self.assertEqual(errors, [])
        state = LibraryState.load()
        self.assertEqual(state.last_scan_status, "complete")
        self.assertEqual(state.scanned_track_count, 1)
        self.assertEqual(Track.objects.get().track_file_path, str(self.track_path))
        response = self.client.get(reverse("library:dashboard"))
        self.assertContains(response, "Completed")

    def test_rescan_does_not_duplicate_or_change_dates(self):
        before = Track.objects.values_list("when_loaded", "when_updated").get()
        with patch("library.scan_service.read_all_metadata", return_value=[self.record()]):
            scan_library(self.root)
        self.assertEqual((Artist.objects.count(), Album.objects.count(), Track.objects.count()), (1, 1, 1))
        self.assertEqual(Track.objects.values_list("when_loaded", "when_updated").get(), before)

    def test_scan_removes_tracks_whose_files_are_gone_and_prunes_empty_parents(self):
        with patch("library.scan_service.read_all_metadata", return_value=[]):
            scan_library(self.root)
        self.assertEqual((Artist.objects.count(), Album.objects.count(), Track.objects.count()), (0, 0, 0))

    def test_failed_scan_is_recorded(self):
        with patch("library.scan_service.read_all_metadata", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                scan_library(self.root)
        state = LibraryState.load()
        self.assertEqual(state.last_scan_status, "failed")
        self.assertEqual(state.scan_error, "boom")

    def test_refresh_route_starts_scan_for_selected_folder(self):
        with patch("library.views.start_scan", return_value=True) as start_scan_mock:
            response = self.client.post(reverse("library:refresh"))
        self.assertEqual(response.status_code, 302)
        start_scan_mock.assert_called_once_with(str(self.root))

    def tag_edit_setup(self):
        fields = [field for field in repository.get_config()["file_attributes"] if field != "length"]
        current_values = {field: "" for field in fields}
        current_values.update({
            "artist": "Test Artist", "album": "Test Album", "title": "Test Track",
            "tracknumber": "1", "discnumber": "1", "date": "2001", "genre": "Test Genre",
        })
        tag_state = {key: [value] for key, value in current_values.items() if value}

        class FakeEasyID3(dict):
            def __init__(self, file_path):
                super().__init__({key: list(value) for key, value in tag_state.items()})

            def save(self):
                tag_state.clear()
                tag_state.update({key: list(value) for key, value in self.items()})

        return current_values, FakeEasyID3

    def test_tag_preview_requires_confirmation_and_confirmed_write_is_backed_up(self):
        track = Track.objects.get()
        current_values, fake_easyid3 = self.tag_edit_setup()
        proposed_values = {**current_values, "title": "New Track"}
        backup_directory = Path(self.temp_directory.name) / "backups"

        with (
            patch.object(views, "EasyID3", fake_easyid3),
            patch.object(views, "read_file_metadata", return_value=self.record(title="New Track")),
            patch.object(views, "read_artwork_from_id3", return_value=None),
            patch.object(views, "calculate_confidence_for_track", return_value=0.9),
            patch.object(repository, "get_backup_directory", return_value=backup_directory),
        ):
            original_file = self.track_path.read_bytes()
            preview = self.client.post(reverse("library:edit-tags", args=(track.pk,)), proposed_values)
            self.assertEqual(preview.status_code, 200)
            self.assertContains(preview, "Test Track")
            self.assertContains(preview, "New Track")
            self.assertEqual(self.track_path.read_bytes(), original_file)
            self.assertFalse(backup_directory.exists())

            response = self.client.post(
                reverse("library:confirm-tag-edit", args=(track.pk,)),
                {"preview_token": preview.context["preview_token"]},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(list(backup_directory.glob("track-*.mp3")))
        self.assertEqual(Track.objects.get().track_title, "New Track")
        self.assertEqual((Artist.objects.count(), Album.objects.count()), (1, 1))

    def test_confirmed_edit_that_cannot_be_stored_restores_the_file_and_keeps_the_track(self):
        track = Track.objects.get()
        current_values, fake_easyid3 = self.tag_edit_setup()
        backup_directory = Path(self.temp_directory.name) / "backups"

        with (
            patch.object(views, "EasyID3", fake_easyid3),
            patch.object(views, "read_file_metadata", return_value=self.record(title="")),
            patch.object(views, "read_artwork_from_id3", return_value=None),
            patch.object(views, "calculate_confidence_for_track", return_value=0.9),
            patch.object(repository, "get_backup_directory", return_value=backup_directory),
        ):
            original_file = self.track_path.read_bytes()
            preview = self.client.post(
                reverse("library:edit-tags", args=(track.pk,)), {**current_values, "title": ""}
            )
            self.client.post(
                reverse("library:confirm-tag-edit", args=(track.pk,)),
                {"preview_token": preview.context["preview_token"]},
            )

        self.assertEqual(self.track_path.read_bytes(), original_file)
        self.assertEqual(Track.objects.get().track_title, "Test Track")

    def test_counts_are_not_inflated_by_joins_in_a_larger_library(self):
        import_records([
            self.record(title="Second", tracknumber="2", file_path=str(self.root / "b.mp3")),
            self.record(title="Third", tracknumber="3", file_path=str(self.root / "c.mp3")),
            self.record(album="Other Album", title="Solo", file_path=str(self.root / "d.mp3")),
            self.record(artist="Second Artist", album="Third Album", genre="Other Genre",
                        title="Lone", file_path=str(self.root / "e.mp3")),
        ])
        artists = {a.artist_name: (a.album_count, a.track_count)
                   for a in self.client.get(reverse("library:artist-list")).context["artists"]}
        self.assertEqual(artists, {"Test Artist": (2, 4), "Second Artist": (1, 1)})

        albums = {a.album_name: a.track_count
                  for a in self.client.get(reverse("library:album-list")).context["albums"]}
        self.assertEqual(albums, {"Test Album": 3, "Other Album": 1, "Third Album": 1})

        genres = {g["name"]: (g["artist_count"], g["album_count"], g["track_count"])
                  for g in self.client.get(reverse("library:genre-index")).context["genres"]}
        self.assertEqual(genres, {"Test Genre": (1, 2, 4), "Other Genre": (1, 1, 1)})

        day = Track.objects.first().when_loaded.date().isoformat()
        detail = self.client.get(reverse("library:loaded-date-detail"), {"day": day}).context
        self.assertEqual((len(detail["artists"]), len(detail["albums"]), len(detail["tracks"])), (2, 3, 5))
        self.assertEqual(sorted(a.track_count for a in detail["albums"]), [1, 1, 3])

        stats = self.client.get(reverse("library:dashboard")).context["stats"]
        self.assertEqual((stats["artist_count"], stats["album_count"], stats["track_count"]), (2, 3, 5))
        self.assertEqual(stats["missing_artwork_count"], 3)
