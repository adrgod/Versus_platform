from django.test import TestCase

from .models import Album, DiscogsAlbum, EnrichmentStatus
from .services import discogs
from .services.importer import import_records

RELEASE = {
    "id": 555, "title": "Bonded by Blood", "year": 1985, "uri": "https://www.discogs.com/release/555-Exodus-Bonded-By-Blood",
    "labels": [{"name": "Torrid Records (2)", "catno": "TOR 1"}],
    "extraartists": [
        {"name": "Mark Whitaker (3)", "role": "Producer"},
        {"name": "Mark Whitaker (3)", "role": "Engineer [Recording]"},
        {"name": "Rob Sandoval", "role": "Artwork [Cover]"},
        {"name": "Gary Holt", "role": "Written-By"},
        {"name": "Somebody", "role": "Photography By"},
    ],
    "companies": [{"name": "Prairie Sun Studios", "entity_type_name": "Recorded At"},
                  {"name": "Some Pressing Plant", "entity_type_name": "Pressed By"}],
}


class FakeDiscogs:
    token = "test"

    def __init__(self):
        self.calls = []

    def search_releases(self, artist, title):
        self.calls.append(("search", artist, title))
        if artist != "Exodus":
            return []
        return [{"id": 1, "title": "Exodus - Bonded By Blood", "year": "1990", "master_id": 0},
                {"id": 2, "title": "Exodus - Bonded By Blood", "year": "1985", "master_id": 99},
                {"id": 3, "title": "Someone Else - Bonded By Blood", "year": "1985", "master_id": 5}]

    def master(self, master_id):
        self.calls.append(("master", master_id))
        return {"main_release": 555}

    def release(self, release_id):
        self.calls.append(("release", release_id))
        return RELEASE


def record(**overrides):
    base = {"artist": "Exodus", "album": "Bonded By Blood (Remastered)", "title": "Bonded by Blood",
            "tracknumber": "1", "file_path": "/music/1.mp3"}
    base.update(overrides)
    return base


class ParsingTests(TestCase):
    def test_credits_are_sorted_into_kinds_and_names_cleaned(self):
        credits = {(c["kind"], c["name"]): c for c in discogs.parse_credits(RELEASE)}
        self.assertEqual(set(credits), {("producer", "Mark Whitaker"), ("engineer", "Mark Whitaker"),
                                        ("artwork", "Rob Sandoval"), ("composer", "Gary Holt"),
                                        ("studio", "Prairie Sun Studios")})
        self.assertEqual(credits[("engineer", "Mark Whitaker")]["role"], "Engineer [Recording]")
        self.assertIn("recorded at", credits[("studio", "Prairie Sun Studios")]["role"])

    def test_clean_name(self):
        self.assertEqual(discogs.clean_name("Torrid Records (2)"), "Torrid Records")
        self.assertEqual(discogs.clean_name("Exodus*"), "Exodus")


class LookupTests(TestCase):
    def setUp(self):
        import_records([record(), record(artist="Nobody Known", album="Lost", file_path="/music/2.mp3")])
        self.fake = FakeDiscogs()

    def test_master_release_is_read_and_fills_credit_cells(self):
        stats = discogs.run(client=self.fake)
        self.assertEqual((stats.albums_matched, stats.albums_not_found), (1, 1))
        self.assertIn(("master", 99), self.fake.calls)  # the master's main release, not the 1990 reissue
        self.assertEqual(self.fake.calls.count(("release", 555)), 1)

        album = Album.objects.get(album_name__startswith="Bonded")
        self.assertEqual(album.discogs.status, EnrichmentStatus.MATCHED)
        self.assertEqual((album.resolved_year, album.resolved_label), (1985, "Torrid Records"))
        cells = album.resolved_credits
        self.assertEqual((cells["producer"]["source"], cells["producer"]["entries"][0]["name"]), ("discogs", "Mark Whitaker"))
        self.assertEqual(cells["studio"]["entries"][0]["name"], "Prairie Sun Studios")
        self.assertEqual(cells["artwork"]["entries"][0]["name"], "Rob Sandoval")
        self.assertEqual(album.resolved_sources["year"], "discogs")

    def test_unknown_album_is_recorded_as_not_found(self):
        discogs.run(client=self.fake)
        self.assertEqual(DiscogsAlbum.objects.get(album__album_name="Lost").status, EnrichmentStatus.NOT_FOUND)

    def test_matched_albums_are_skipped_unless_refreshing(self):
        discogs.run(client=self.fake)
        before = len(self.fake.calls)
        discogs.run(client=self.fake)
        self.assertEqual(len(self.fake.calls), before)
        discogs.run(client=self.fake, refresh=True)
        self.assertGreater(len(self.fake.calls), before)

    def test_musicbrainz_credits_win_over_discogs(self):
        from .models import AlbumProfile
        album = Album.objects.get(album_name__startswith="Bonded")
        AlbumProfile.objects.create(album=album, status=EnrichmentStatus.MATCHED, credits=[
            {"kind": "producer", "name": "MB Producer", "role": "producer", "area": "", "count": 1}])
        discogs.run(client=self.fake)
        album.refresh_from_db()
        self.assertEqual(album.resolved_credits["producer"]["entries"][0]["name"], "MB Producer")
        self.assertEqual(album.resolved_credits["engineer"]["source"], "discogs")  # MusicBrainz had none

    def test_missing_token_stops_with_instructions(self):
        client = FakeDiscogs()
        client.token = ""
        with self.assertRaisesMessage(discogs.DiscogsError, "access token"):
            discogs.run(client=client)


class PageTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        import_records([record()])
        self.client.force_login(get_user_model().objects.create_user("tester", password="x"))

    def test_pages_show_the_discogs_button_and_start_needs_a_token(self):
        from django.urls import reverse
        album = Album.objects.get()
        page = self.client.get(reverse("library:album-detail", args=(album.pk,)))
        self.assertContains(page, "Lookup on Discogs")
        self.assertContains(self.client.get(reverse("library:enrichment")), "Lookup on Discogs")
        from unittest import mock
        with mock.patch("library.services.discogs.configured_token", return_value=""):
            reply = self.client.post(reverse("library:discogs-start"), {"album_id": album.pk}, follow=True)
        self.assertContains(reply, "access token")
