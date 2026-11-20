"""Import Data / Read Local MP3 Tags / Lookup MusicBrainz / Enrich Data with AI."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from mutagen.id3 import ID3, TALB, TDRC, TIT2, TPE1, TRCK

from library import repository, scan_service
from library.models import Album, AlbumProfile, Artist, Track
from library.services import enrichment
from library.services.files_scanner import batch_date_from_path, read_all_metadata
from library.services.importer import import_records
from library.test_enrichment import FakeMusicBrainz, record

MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)


class BatchDateTests(TestCase):
    def test_closest_date_folder_names_the_batch_at_any_depth(self):
        self.assertEqual(batch_date_from_path("/m/20261013/Season of Mist/Band_-_Album/01 Song.mp3"), date(2026, 10, 13))
        self.assertEqual(batch_date_from_path("/m/20240315/Label/Band/Album/CD2/01.mp3"), date(2024, 3, 15))
        self.assertEqual(batch_date_from_path("/m/20200101/20240315/Band/01.mp3"), date(2024, 3, 15))

    def test_no_or_invalid_date_folder_means_no_batch(self):
        self.assertIsNone(batch_date_from_path("/m/Band/Album/01.mp3"))
        self.assertIsNone(batch_date_from_path("/m/20241345/Band/01.mp3"))
        self.assertIsNone(batch_date_from_path("/m/Band/20240315.mp3"))  # the file name is not a folder


class LocalTagsTestCase(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        roots = patch("library.repository.get_allowed_roots", return_value=[self.root])
        roots.start()
        self.addCleanup(roots.stop)
        # Band and album share one folder: not the full PUBLISHER/BAND/ALBUM layout.
        self.album_dir = self.root / "20261013" / "Season of Mist" / "Saor_-_Caledonia"
        self.album_dir.mkdir(parents=True)
        self.files = [self.album_dir / f"0{n} Song {n}.mp3" for n in (1, 2)]
        for number, path in enumerate(self.files, start=1):
            path.write_bytes(MP3_FRAME * 10)
            self.tag(path, title=f"Song {number}", number=number)
        import_records(read_all_metadata(self.root, repository.get_config()))
        self.album = Album.objects.get()

    def tag(self, path, *, title, number, album="Caledonia"):
        tags = ID3()
        for frame in (TPE1(encoding=3, text=["Saor"]), TALB(encoding=3, text=[album]), TIT2(encoding=3, text=[title]),
                      TRCK(encoding=3, text=[str(number)]), TDRC(encoding=3, text=["2026"])):
            tags.add(frame)
        tags.save(path, v2_version=3)


class ReadLocalTagsTests(LocalTagsTestCase):
    def test_import_names_the_batch_from_the_date_folder(self):
        self.assertEqual(self.album.album_batch_date, date(2026, 10, 13))

    def test_changed_tag_updates_the_track_without_leaving_a_duplicate(self):
        self.tag(self.files[0], title="Aura", number=1)
        result = scan_service.read_local_tags(album_ids=[self.album.pk])

        self.assertEqual(result.files_read, 2)
        self.assertEqual(result.replaced, 1)  # the title is part of a track's identity
        titles = sorted(Track.objects.values_list("track_title", flat=True))
        self.assertEqual(titles, ["Aura", "Song 2"])

    def test_unchanged_files_change_nothing(self):
        result = scan_service.read_local_tags(artist_ids=[self.album.artist_id])
        self.assertEqual((result.files_read, result.updated, result.replaced, result.created), (2, 0, 0, 0))
        self.assertEqual(result.unchanged, 2)

    def test_missing_files_are_reported_not_deleted(self):
        self.files[1].unlink()
        result = scan_service.read_local_tags(album_ids=[self.album.pk])
        self.assertEqual(len(result.missing), 1)
        self.assertEqual(Track.objects.count(), 2)

    def test_album_renamed_in_tags_moves_tracks_to_the_new_album(self):
        for number, path in enumerate(self.files, start=1):
            self.tag(path, title=f"Song {number}", number=number, album="Caledonia (Remaster)")
        scan_service.read_local_tags(album_ids=[self.album.pk])
        self.assertEqual(list(Album.objects.values_list("album_name", flat=True)), ["Caledonia (Remaster)"])
        self.assertEqual(Track.objects.count(), 2)

    def test_returns_none_while_import_data_runs(self):
        with scan_service._scan_lock:
            self.assertIsNone(scan_service.read_local_tags(album_ids=[self.album.pk]))


class MetadataActionViewTests(LocalTagsTestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user("listener", password="test-password")
        self.web = Client()
        self.web.force_login(user)

    def test_album_and_artist_pages_offer_the_three_actions(self):
        for url in (reverse("library:album-detail", args=[self.album.pk]),
                    reverse("library:artist-detail", args=[self.album.artist_id])):
            page = self.web.get(url).content.decode()
            for label in ("Read Local MP3 Tags", "Lookup MusicBrainz", "Enrich Data with AI"):
                self.assertIn(label, page, url)

    def test_dashboard_uses_import_data_and_view_completeness(self):
        page = self.web.get(reverse("library:dashboard")).content.decode()
        self.assertIn("Import Data", page)
        self.assertIn("View Completeness", page)
        self.assertNotIn("Refresh library", page)

    def test_completeness_names_the_batch(self):
        page = self.web.get(reverse("library:completeness-report")).content.decode()
        self.assertIn("Batch 13 Oct 2026", page)

    def test_read_local_tags_view_returns_to_the_album(self):
        response = self.web.post(reverse("library:read-local-tags"), {"album_id": self.album.pk}, follow=True)
        self.assertRedirects(response, reverse("library:album-detail", args=[self.album.pk]))
        self.assertContains(response, "Read Local MP3 Tags: 2 files read · no changes.")

    def test_lookup_details_online_for_one_album(self):
        with patch("library.views.enrichment_service.start_enrichment", return_value=True) as start:
            response = self.web.post(reverse("library:enrichment-start"), {"album_id": self.album.pk, "refresh": "1"})
        start.assert_called_once_with(refresh=True, artist_ids=None, album_ids=[self.album.pk])
        self.assertRedirects(response, reverse("library:album-detail", args=[self.album.pk]), fetch_redirect_response=False)

    def test_enrich_data_with_ai_for_one_artist(self):
        with patch("library.views.enrichment_service.start_job", return_value=True) as start:
            response = self.web.post(reverse("library:notes-start"), {"artist_id": self.album.artist_id, "refresh": "1"})
        self.assertEqual(start.call_args.kwargs["artist_ids"], [self.album.artist_id])
        self.assertRedirects(response, reverse("library:artist-detail", args=[self.album.artist_id]), fetch_redirect_response=False)


class AlbumLookupScopeTests(TestCase):
    def test_only_the_chosen_album_is_looked_up(self):
        import_records([record(), record(album="Pleasures of the Flesh", file_path="/music/9.mp3")])
        chosen = Album.objects.get(album_name="Bonded By Blood (Remastered)")
        enrichment.enrich_library(FakeMusicBrainz(), album_ids=[chosen.pk])
        self.assertEqual(list(AlbumProfile.objects.values_list("album_id", flat=True)), [chosen.pk])
        self.assertTrue(Artist.objects.get().profile.external_id)


class CarryOverTests(LocalTagsTestCase):
    """Changed tags that create a new entry keep everything the old entry had."""

    def setUp(self):
        super().setUp()
        from datetime import datetime, timezone as tz
        from library.models import (
            AlbumDocument, AlbumFacet, AlbumInsight, ArtistFacet, ArtistProfile, EnrichmentStatus, Facet, TrackLyrics,
        )
        self.models = locals()
        album, artist = self.album, self.album.artist
        AlbumProfile.objects.create(album=album, status=EnrichmentStatus.MATCHED, external_id="album-mbid", rating=4)
        AlbumInsight.objects.create(album=album, status=EnrichmentStatus.MATCHED, summary="A record about the Highlands.")
        AlbumFacet.objects.create(album=album, facet=Facet.objects.create(kind="mood", name="wistful"), source="insights")
        AlbumDocument.objects.create(album=album, file_path=str(self.album_dir / "notes.txt"), file_type="txt", text="Liner notes")
        ArtistProfile.objects.create(artist=artist, status=EnrichmentStatus.MATCHED, external_id="artist-mbid", country="United Kingdom")
        ArtistFacet.objects.create(artist=artist, facet=Facet.objects.create(kind="genre", name="folk metal"))
        first = Track.objects.get(track_title="Song 1")
        TrackLyrics.objects.create(track=first, status=EnrichmentStatus.MATCHED, source="lrclib", text="Over the hills")
        self.loaded = datetime(2026, 1, 2, 3, 4, tzinfo=tz.utc)
        Album.objects.filter(pk=album.pk).update(when_loaded=self.loaded)
        Artist.objects.filter(pk=artist.pk).update(when_loaded=self.loaded)

    def retag(self, *, album="Caledonia", artist="Saor", year="2026", files=None):
        for number, path in enumerate(self.files, start=1):
            if files is not None and number not in files:
                continue
            tags = ID3()
            for frame in (TPE1(encoding=3, text=[artist]), TALB(encoding=3, text=[album]), TIT2(encoding=3, text=[f"Song {number}"]),
                          TRCK(encoding=3, text=[str(number)]), TDRC(encoding=3, text=[year])):
                tags.add(frame)
            tags.save(path, v2_version=3)

    def assert_album_details_on(self, album):
        m = self.models
        self.assertEqual(album.profile.external_id, "album-mbid")
        self.assertEqual(album.insight.summary, "A record about the Highlands.")
        self.assertTrue(m["AlbumFacet"].objects.filter(album=album, facet__name="wistful").exists())
        self.assertEqual(list(album.documents.values_list("text", flat=True)), ["Liner notes"])
        self.assertEqual(album.album_batch_date, date(2026, 10, 13))
        self.assertEqual(album.when_loaded, self.loaded)
        lyrics = m["TrackLyrics"].objects.get(track__album=album)
        self.assertEqual((lyrics.track.track_title, lyrics.text), ("Song 1", "Over the hills"))

    def test_year_change_carries_album_details_to_the_new_entry(self):
        old_pk = self.album.pk
        self.retag(year="2025")
        result = scan_service.read_local_tags(album_ids=[old_pk])

        new = Album.objects.get()
        self.assertNotEqual(new.pk, old_pk)
        self.assertEqual(new.album_release_year, 2025)
        self.assert_album_details_on(new)
        self.assertEqual((result.replaced, result.albums_carried, result.artists_carried), (2, 1, 0))
        self.assertEqual(Track.objects.count(), 2)

    def test_artist_rename_carries_artist_and_album_details(self):
        self.retag(artist="Saor Band")
        result = scan_service.read_local_tags(artist_ids=[self.album.artist_id])

        artist = Artist.objects.get()
        self.assertEqual(artist.artist_name, "Saor Band")
        self.assertEqual(artist.profile.external_id, "artist-mbid")
        self.assertTrue(self.models["ArtistFacet"].objects.filter(artist=artist, facet__name="folk metal").exists())
        self.assertEqual(artist.when_loaded, self.loaded)
        self.assert_album_details_on(Album.objects.get())
        self.assertEqual((result.albums_carried, result.artists_carried), (1, 1))

    def test_import_data_carries_details_and_leaves_no_duplicates(self):
        self.retag(album="Caledonia (Remaster)")
        scan_service.scan_library(self.root)

        self.assertEqual(Track.objects.count(), 2)
        album = Album.objects.get()
        self.assertEqual(album.album_name, "Caledonia (Remaster)")
        self.assert_album_details_on(album)

    def test_one_retagged_track_copies_details_and_the_old_album_keeps_its_own(self):
        self.retag(album="Caledonia Bonus", files=[2])
        scan_service.read_local_tags(album_ids=[self.album.pk])

        old, new = Album.objects.get(pk=self.album.pk), Album.objects.get(album_name="Caledonia Bonus")
        self.assertEqual(old.profile.external_id, "album-mbid")
        self.assertEqual(new.profile.external_id, "album-mbid")
        self.assertEqual((old.track_set.count(), new.track_set.count()), (1, 1))

    def test_the_new_entrys_own_matched_lookup_wins(self):
        from library.models import EnrichmentStatus
        existing = Album.objects.create(artist=self.album.artist, album_name="Caledonia", album_release_year=2025,
                                        album_publisher=self.album.album_publisher)
        AlbumProfile.objects.create(album=existing, status=EnrichmentStatus.MATCHED, external_id="its-own")
        self.retag(year="2025")
        scan_service.read_local_tags(album_ids=[self.album.pk])

        self.assertEqual(Album.objects.get().pk, existing.pk)
        self.assertEqual(AlbumProfile.objects.get().external_id, "its-own")
        self.assertEqual(Album.objects.get().insight.summary, "A record about the Highlands.")  # gaps still filled


class FolderLayoutTests(TestCase):
    def test_artist_dash_album_folder_is_the_common_layout(self):
        from library.services.files_scanner import album_folder, derived_tags
        self.assertEqual(derived_tags("/m/20261013/Season of Mist/SAOR_-_Caledonia/03 - Carved In Stone.mp3"), {
            "publisher": "Season of Mist", "artist": "SAOR", "album": "Caledonia",
            "tracknumber": "3", "title": "Carved In Stone",
        })
        path = "/m/20261013/Label/Band - Album/CD2/01 - Song.mp3"
        self.assertEqual(derived_tags(path)["discnumber"], "2")
        self.assertEqual(derived_tags(path)["album"], "Album")
        self.assertEqual(album_folder(path).name, "Band - Album")

    def test_only_the_first_dash_splits_artist_from_album(self):
        from library.services.files_scanner import split_artist_album
        self.assertEqual(split_artist_album("Band - Album - Deluxe Edition"), ("Band", "Album - Deluxe Edition"))
        self.assertEqual(split_artist_album("Spider-Man_-_Web_Songs"), ("Spider-Man", "Web Songs"))
        self.assertIsNone(split_artist_album("Dead_Space_Enigma"))

    def test_artist_and_album_folders_still_work(self):
        from library.services.files_scanner import derived_tags
        self.assertEqual(derived_tags("/m/20240315/Label/Band/Album/01 - Song.mp3")["album"], "Album")
        self.assertEqual(derived_tags("/m/20240315/Label/Dead_Space_Enigma/01 - Song.mp3"), {})


class EditTagsTests(LocalTagsTestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user("editor", password="test-password")
        self.web = Client()
        self.web.force_login(user)
        self.tracks = list(Track.objects.order_by("track_number"))

    def open_album_editor(self):
        response = self.web.get(reverse("library:album-edit-tags", args=[self.album.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def album_form(self, response, **changes):
        """The editor's form as the browser would send it, with ``changes`` applied."""
        data = {"edit_token": response.context["edit_token"]}
        for field in response.context["header"]:
            data[f"h-{field['key']}"] = field["value"]
            data[f"h-{field['key']}-initial"] = field["initial"]
        for row in response.context["rows"]:
            for cell in row["cells"]:
                data[cell["name"]] = cell["value"]
        data.update(changes)
        return data

    def test_album_page_links_to_the_editor_which_is_its_own_page(self):
        album_page = self.web.get(reverse("library:album-detail", args=[self.album.pk])).content.decode()
        self.assertIn(reverse("library:album-edit-tags", args=[self.album.pk]), album_page)
        editor = self.open_album_editor().content.decode()
        self.assertIn("ALBUM TAGS", editor)
        self.assertIn(f'name="t{self.tracks[0].pk}-title"', editor)
        self.assertIn("Save", editor)

    def test_header_and_cell_edits_are_written_to_the_files(self):
        AlbumProfile.objects.create(album=self.album, status="matched", external_id="album-mbid")
        response = self.open_album_editor()
        first = self.tracks[0]
        data = self.album_form(response, **{"h-album": "Caledonia (Deluxe)", "h-genre": "Folk Metal",
                                            f"t{first.pk}-title": "Aura", f"t{first.pk}-genre": "Folk Metal"})
        # The browser's script copies a header value into its column; without it the server does the same.
        response = self.web.post(reverse("library:album-edit-tags", args=[self.album.pk]), data, follow=True)

        from library.services.tag_editor import read_tags
        self.assertEqual([read_tags(path)["album"] for path in self.files], ["Caledonia (Deluxe)"] * 2)
        self.assertEqual([read_tags(path)["genre"] for path in self.files], ["Folk Metal"] * 2)
        self.assertEqual(read_tags(self.files[0])["title"], "Aura")
        album = Album.objects.get()
        self.assertEqual(album.album_name, "Caledonia (Deluxe)")
        self.assertEqual(album.profile.external_id, "album-mbid")  # carried over to the new entry
        self.assertRedirects(response, reverse("library:album-detail", args=[album.pk]))
        self.assertContains(response, "Edit Tags: saved to 2 files · details carried over to the new album.")

    def test_bad_values_are_refused_and_nothing_is_written(self):
        response = self.open_album_editor()
        before = self.files[0].read_bytes()
        data = self.album_form(response, **{f"t{self.tracks[0].pk}-tracknumber": "first"})
        response = self.web.post(reverse("library:album-edit-tags", args=[self.album.pk]), data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Use a number, like 3 or 3/12.", status_code=400)
        self.assertEqual(self.files[0].read_bytes(), before)

    def test_a_file_changed_since_opening_the_editor_is_not_overwritten(self):
        response = self.open_album_editor()
        self.tag(self.files[0], title="Changed elsewhere", number=1)
        data = self.album_form(response, **{f"t{self.tracks[0].pk}-title": "Mine"})
        response = self.web.post(reverse("library:album-edit-tags", args=[self.album.pk]), data, follow=True)
        self.assertContains(response, "changed since the editor was opened")
        from library.services.tag_editor import read_tags
        self.assertEqual(read_tags(self.files[0])["title"], "Changed elsewhere")

    def test_a_failure_puts_every_file_back(self):
        from library.services import tag_editor
        originals = [path.read_bytes() for path in self.files]
        real_write = tag_editor._write
        calls = []

        def flaky(path, changes):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            real_write(path, changes)

        with patch.object(tag_editor, "_write", flaky):
            with self.assertRaises(tag_editor.TagEditError):
                tag_editor.save({str(path): {"genre": "Folk"} for path in self.files})
        self.assertEqual([path.read_bytes() for path in self.files], originals)

    def test_artist_editor_writes_name_country_and_formed_year(self):
        from library.models import ArtistProfile
        ArtistProfile.objects.create(artist=self.album.artist, status="matched", external_id="artist-mbid")
        url = reverse("library:artist-edit-tags", args=[self.album.artist_id])
        response = self.web.get(url)
        self.assertEqual(response.status_code, 200)
        data = {"edit_token": response.context["edit_token"]}
        for field in response.context["fields"]:
            data[f"h-{field['key']}-initial"] = field["initial"]
        data.update({"h-artist": "Saor Band", "h-artist_country": "Scotland", "h-artist_formed_year": "2013"})
        self.web.post(url, data)

        artist = Artist.objects.get()
        self.assertEqual((artist.artist_name, artist.artist_country, artist.artist_formed_year), ("Saor Band", "Scotland", 2013))
        self.assertEqual(artist.profile.external_id, "artist-mbid")
        from library.services.tag_editor import read_tags
        self.assertEqual(read_tags(self.files[1])["artist_country"], "Scotland")

    def test_artist_editor_refuses_a_bad_year(self):
        url = reverse("library:artist-edit-tags", args=[self.album.artist_id])
        response = self.web.get(url)
        data = {"edit_token": response.context["edit_token"], "h-artist_formed_year": "around 2010", "h-artist_formed_year-initial": ""}
        response = self.web.post(url, data)
        self.assertContains(response, "Use a year, like 1994.", status_code=400)


class WriteBackFoundDataTests(LocalTagsTestCase):
    """Found data (lookups, liner notes, file names) is offered by Edit Tags and written to the files on Save."""

    open_album_editor = EditTagsTests.open_album_editor
    album_form = EditTagsTests.album_form

    def setUp(self):
        super().setUp()
        self.web = Client()
        self.web.force_login(get_user_model().objects.create_user("editor", password="test-password"))
        from library.models import AlbumCredit, AlbumFacet, ArtistFacet, ArtistProfile, EnrichmentStatus, Facet
        from library.services import resolver
        genre = Facet.objects.create(kind="genre", name="folk metal")
        AlbumFacet.objects.create(album=self.album, facet=genre, source="musicbrainz", weight=3)
        ArtistProfile.objects.create(artist=self.album.artist, status=EnrichmentStatus.MATCHED,
                                     country="Scotland", lineup=[{"name": "Andy Marshall", "instruments": []}])
        AlbumCredit.objects.create(album=self.album, kind="composer", name="Andy Marshall", source="notes",
                                   evidence="Music by Andy Marshall")
        resolver.resolve_for_albums([self.album.pk])
        self.album.refresh_from_db()

    def test_found_values_are_offered_with_where_they_came_from(self):
        header = {field["key"]: field for field in self.open_album_editor().context["header"]}
        self.assertEqual((header["genre"]["suggested"], header["genre"]["origin"]), ("folk metal", "MusicBrainz"))
        self.assertEqual((header["composer"]["suggested"], header["composer"]["origin"]), ("Andy Marshall", "Liner notes"))
        self.assertEqual(header["date"]["suggested"], "")  # the year is already a tag in the files

    def test_saving_the_found_values_writes_them_and_they_become_tags(self):
        response = self.open_album_editor()
        data = self.album_form(response, **{"h-genre": "folk metal", "h-composer": "Andy Marshall"})
        self.web.post(reverse("library:album-edit-tags", args=[self.album.pk]), data, follow=True)

        from library.services.tag_editor import read_tags
        self.assertEqual([read_tags(path)["genre"] for path in self.files], ["folk metal"] * 2)
        self.assertEqual([read_tags(path)["composer"] for path in self.files], ["Andy Marshall"] * 2)
        album = Album.objects.get()
        self.assertEqual(album.album_genre, "folk metal")
        self.assertEqual(album.resolved_sources["genre"], "tags")
        self.assertEqual(album.resolved_credits["composer"]["source"], "tags")
        # Nothing left to offer for what is now in the files.
        header = {field["key"]: field for field in self.open_album_editor().context["header"]}
        self.assertEqual((header["genre"]["suggested"], header["composer"]["suggested"]), ("", ""))
