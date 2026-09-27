from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from db import db_operations
from . import repository
from .scan_service import scan_library


class LibraryWorkflowTests(TestCase):
	def setUp(self):
		self.temp_directory = TemporaryDirectory()
		self.addCleanup(self.temp_directory.cleanup)
		self.root = Path(self.temp_directory.name) / "music"
		self.root.mkdir()
		self.track_path = self.root / "track.mp3"
		self.track_path.write_bytes(b"0123456789abcdef")
		self.database_path = Path(self.temp_directory.name) / "music.db"

		database_patch = patch("library.repository.get_database_path", return_value=self.database_path)
		roots_patch = patch("library.repository.get_allowed_roots", return_value=[self.root])
		database_patch.start()
		roots_patch.start()
		self.addCleanup(database_patch.stop)
		self.addCleanup(roots_patch.stop)

		connection = repository.connect()
		db_operations.db_create_schema(connection)
		db_operations.db_load_data(connection, [self.record()])
		repository.get_library_state(connection)
		connection.execute(
			"UPDATE tbl_library_state SET source_folder = ? WHERE singleton_id = 1",
			(str(self.root),),
		)
		connection.commit()
		connection.close()

		self.user = get_user_model().objects.create_user(
			username="library-test-user",
			password="not-a-real-password",
		)
		self.addCleanup(self.user.delete)
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

	def track_id(self):
		connection = repository.connect()
		try:
			return connection.execute("SELECT track_id FROM tbl_track").fetchone()[0]
		finally:
			connection.close()

	def test_dashboard_and_catalog_use_music_database(self):
		dashboard_response = self.client.get(reverse("library:dashboard"))
		album_response = self.client.get(reverse("library:album-list"))
		quality_response = self.client.get(reverse("library:quality-report"))
		self.assertEqual(dashboard_response.status_code, 200)
		self.assertEqual(dashboard_response.context["stats"]["track_count"], 1)
		self.assertEqual(album_response.status_code, 200)
		self.assertContains(album_response, "Test Album")
		self.assertEqual(quality_response.status_code, 200)

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
		connection = repository.connect()
		loaded_day = connection.execute("SELECT DATE(when_loaded) FROM tbl_track LIMIT 1").fetchone()[0]
		connection.close()
		self.assertContains(index_response, loaded_day)

		detail_response = self.client.get(reverse("library:loaded-date-detail"), {"day": loaded_day})
		self.assertEqual(detail_response.status_code, 200)
		self.assertContains(detail_response, "Test Artist")
		self.assertContains(detail_response, "Test Album")
		self.assertContains(detail_response, "Test Track")
		self.assertContains(detail_response, "data-enqueue-playlist")
		self.assertContains(detail_response, "data-queue-track")

	def test_library_pages_require_login(self):
		self.client.logout()
		response = self.client.get(reverse("library:dashboard"))
		self.assertEqual(response.status_code, 302)
		self.assertIn("/accounts/login/", response["Location"])

	def test_stream_supports_byte_ranges_and_rejects_outside_files(self):
		track_id = self.track_id()
		response = self.client.get(
			reverse("library:stream-track", args=(track_id,)),
			HTTP_RANGE="bytes=3-6",
		)
		self.assertEqual(response.status_code, 206)
		self.assertEqual(b"".join(response.streaming_content), b"3456")
		self.assertEqual(response["Content-Range"], "bytes 3-6/16")

		connection = repository.connect()
		connection.execute("UPDATE tbl_track SET file_path = ?", (str(self.root.parent / "outside.mp3"),))
		connection.commit()
		connection.close()
		blocked = self.client.get(reverse("library:stream-track", args=(track_id,)))
		self.assertEqual(blocked.status_code, 404)

	def test_folder_selection_is_confined_to_allowed_root(self):
		child = self.root / "subfolder"
		child.mkdir()
		response = self.client.post(reverse("library:choose-folder"), {"folder": str(child)})
		self.assertEqual(response.status_code, 302)
		connection = repository.connect()
		self.assertEqual(repository.get_library_state(connection)["source_folder"], str(child.resolve()))
		connection.close()

		outside = self.root.parent / "outside"
		outside.mkdir()
		self.client.post(reverse("library:choose-folder"), {"folder": str(outside)})
		connection = repository.connect()
		self.assertEqual(repository.get_library_state(connection)["source_folder"], str(child.resolve()))
		connection.close()

	def test_scan_records_status_and_keeps_track_path(self):
		with patch("library.scan_service.local_files.read_all_metadata", return_value=[self.record()]):
			count, errors = scan_library(self.root)
		self.assertEqual(count, 1)
		self.assertEqual(errors, [])
		connection = repository.connect()
		state = repository.get_library_state(connection)
		stored_path = connection.execute("SELECT file_path FROM tbl_track").fetchone()[0]
		connection.close()
		self.assertEqual(state["last_scan_status"], "complete")
		self.assertEqual(state["scanned_track_count"], 1)
		self.assertEqual(stored_path, str(self.track_path))
		response = self.client.get(reverse("library:dashboard"))
		self.assertContains(response, "Completed")

	def test_refresh_route_starts_scan_for_selected_folder(self):
		with patch("library.views.start_scan", return_value=True) as start_scan_mock:
			response = self.client.post(reverse("library:refresh"))
		self.assertEqual(response.status_code, 302)
		start_scan_mock.assert_called_once_with(str(self.root))

	def test_tag_preview_requires_confirmation_and_confirmed_write_is_backed_up(self):
		from . import views

		track_id = self.track_id()
		fields = [field for field in repository.get_config()["file_attributes"] if field != "length"]
		current_values = {field: "" for field in fields}
		current_values.update({"artist": "Test Artist", "album": "Test Album", "title": "Test Track", "tracknumber": "1", "discnumber": "1", "date": "2001", "genre": "Test Genre"})
		proposed_values = {**current_values, "title": "New Track"}
		tag_state = {key: [value] for key, value in current_values.items() if value}
		backup_directory = Path(self.temp_directory.name) / "backups"

		class FakeEasyID3(dict):
			def __init__(self, file_path):
				super().__init__({key: list(value) for key, value in tag_state.items()})

			def save(self):
				tag_state.clear()
				tag_state.update({key: list(value) for key, value in self.items()})

		updated_record = self.record(title="New Track")
		backup_directory = Path(self.temp_directory.name) / "backups"
		with (
			patch.object(views, "EasyID3", FakeEasyID3),
			patch.object(views.local_files, "read_metadata", return_value=updated_record),
			patch.object(views.local_files, "read_artwork", return_value=None),
			patch.object(views.local_files, "calculate_confidence_for_track", return_value=0.9),
			patch.object(repository, "get_backup_directory", return_value=backup_directory),
		):
			original_file = self.track_path.read_bytes()
			preview = self.client.post(
				reverse("library:edit-tags", args=(track_id,)),
				proposed_values,
			)
			self.assertEqual(preview.status_code, 200)
			self.assertContains(preview, "Test Track")
			self.assertContains(preview, "New Track")
			self.assertEqual(self.track_path.read_bytes(), original_file)
			self.assertFalse(backup_directory.exists())

			response = self.client.post(
				reverse("library:confirm-tag-edit", args=(track_id,)),
				{"preview_token": preview.context["preview_token"]},
			)

		self.assertEqual(response.status_code, 302)
		self.assertTrue(list(backup_directory.glob("track-*.mp3")))
		connection = repository.connect()
		self.assertEqual(connection.execute("SELECT track_name FROM tbl_track").fetchone()[0], "New Track")
		connection.close()
