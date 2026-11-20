import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from library import repository


class DriveRootsTests(TestCase):
    def setUp(self):
        self.d, self.e = (Path(tempfile.mkdtemp()).resolve() for _ in range(2))
        self.client.force_login(get_user_model().objects.create_user("tester", password="x"))

    def test_filesystem_root_means_every_drive_on_windows(self):
        config = {"source_data_folder": str(self.d), "allowed_library_roots": ["/"]}
        with patch.object(repository, "IS_WINDOWS", True), \
                patch.object(repository, "windows_drives", return_value=[self.d, self.e]), \
                patch.object(repository, "get_config", return_value=config):
            self.assertEqual(repository.get_allowed_roots(), [self.d, self.e])
            self.assertEqual(repository.resolve_allowed_folder(self.e), self.e)  # the second drive is allowed

    def test_other_systems_keep_the_configured_roots(self):
        config = {"source_data_folder": str(self.d), "allowed_library_roots": [str(self.d)]}
        with patch.object(repository, "get_config", return_value=config):
            self.assertEqual(repository.get_allowed_roots(), [self.d])

    def test_browser_offers_the_drives_and_a_path_box(self):
        with patch("library.repository.get_allowed_roots", return_value=[self.d, self.e]):
            page = self.client.get(reverse("library:folder-browser"), {"path": str(self.e)})
        self.assertContains(page, "Drives and locations")
        self.assertContains(page, 'name="path"')
