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


class AlbumListSortFilterTests(TestCase):
    def setUp(self):
        from datetime import date
        from decimal import Decimal
        from library.models import Album, Artist, Track
        self.client.force_login(get_user_model().objects.create_user("tester2", password="x"))
        zed, amy = Artist.objects.create(artist_name="Zed"), Artist.objects.create(artist_name="Amy")
        self.long = Album.objects.create(artist=zed, album_name="Alpha", album_release_year=1990, resolved_year=1990,
                                         resolved_country="Norway", album_batch_date=date(2026, 1, 5))
        self.short = Album.objects.create(artist=amy, album_name="Beta", album_release_year=2000, resolved_year=2000,
                                          resolved_country="Hungary", album_batch_date=date(2026, 2, 9))
        for n in range(3):
            Track.objects.create(album=self.long, track_title=f"L{n}", track_number=n + 1, track_duration=Decimal("1200"))
        Track.objects.create(album=self.short, track_title="S", track_number=1, track_duration=Decimal("180"))

    def names(self, **params):
        return [a.album_name for a in self.client.get(reverse("library:album-list"), params).context["albums"]]

    def test_sorting(self):
        self.assertEqual(self.names(sort="artist"), ["Beta", "Alpha"])  # Amy, Zed
        self.assertEqual(self.names(sort="album", dir="desc"), ["Beta", "Alpha"])
        self.assertEqual(self.names(sort="tracks", dir="desc"), ["Alpha", "Beta"])
        self.assertEqual(self.names(sort="length"), ["Beta", "Alpha"])
        self.assertEqual(self.names(sort="nonsense"), ["Beta", "Alpha"])  # falls back to artist

    def test_filters(self):
        self.assertEqual(self.names(country="Norway"), ["Alpha"])
        self.assertEqual(self.names(year="2000"), ["Beta"])
        self.assertEqual(self.names(batch="2026-02-09"), ["Beta"])
        self.assertEqual(self.names(batch="garbage"), [])
        self.assertEqual(self.names(tracks_min="2"), ["Alpha"])
        self.assertEqual(self.names(tracks_max="1"), ["Beta"])
        self.assertEqual(self.names(minutes_min="30"), ["Alpha"])  # 60 minutes
        self.assertEqual(self.names(minutes_max="5"), ["Beta"])  # 3 minutes
        self.assertEqual(self.names(tracks_min="abc"), ["Beta", "Alpha"])  # ignored

    def test_tile_shows_the_length_and_header_has_the_controls(self):
        page = self.client.get(reverse("library:album-list"))
        self.assertContains(page, "3 TRACKS · 1:00:00")
        self.assertContains(page, "1 TRACKS · 3:00")
        for name in ("sort", "batch", "country", "year", "tracks_min", "minutes_max"):
            self.assertContains(page, f'name="{name}"')
