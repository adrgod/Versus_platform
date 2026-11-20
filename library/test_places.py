from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from library.models import Album, Artist, MetalArchivesArtist, PlaceCountry
from library.services import places, resolver


class LocateTests(TestCase):
    def test_a_country_stays_a_country(self):
        self.assertEqual(places.locate("Hungary"), ("Hungary", "", ""))

    def test_city_and_country_in_one_tag_are_split(self):
        self.assertEqual(places.locate("Oslo, Norway"), ("Norway", "Oslo", ""))
        self.assertEqual(places.locate("Tampere (Finland)"), ("Finland", "Tampere", ""))
        self.assertEqual(places.locate("Los Angeles, California, USA"), ("USA", "Los Angeles, California", ""))

    def test_a_known_city_gives_its_country_offline(self):
        with patch.object(places.NominatimClient, "country_of", side_effect=AssertionError("went online")):
            self.assertEqual(places.locate("Budapest"), ("Hungary", "Budapest", "city"))
            self.assertEqual(places.locate("Göteborg"), ("Sweden", "Göteborg", "city"))
            self.assertEqual(places.locate("Gothenburg")[0], "Sweden")  # English spelling of a big city
            self.assertEqual(places.locate("Birmingham")[0], "United Kingdom")  # the biggest one

    def test_another_source_beats_a_city_search(self):
        with patch.object(places.NominatimClient, "country_of", side_effect=AssertionError("went online")):
            self.assertEqual(places.locate("Smalltown", hint_country="Norway"), ("Norway", "Smalltown", "hint"))

    def test_unknown_place_is_searched_online_once_and_kept(self):
        with patch.object(places.NominatimClient, "country_of", return_value="Norway") as search:
            self.assertEqual(places.locate("Tinyvik"), ("Norway", "Tinyvik", "city"))
            self.assertEqual(places.locate("tinyvik")[0], "Norway")
        self.assertEqual(search.call_count, 1)
        self.assertEqual(PlaceCountry.objects.get().country, "Norway")

    def test_a_place_nobody_knows_is_not_asked_again(self):
        with patch.object(places.NominatimClient, "country_of", return_value="") as search:
            self.assertEqual(places.locate("Nowhereville"), ("", "Nowhereville", ""))
            places.locate("Nowhereville")
        self.assertEqual(search.call_count, 1)

    def test_a_failed_lookup_is_not_kept(self):
        with patch.object(places, "_unreachable", []), \
                patch.object(places.NominatimClient, "country_of", side_effect=places.ServiceError("offline")):
            self.assertEqual(places.locate("Tinyvik")[0], "")
        self.assertFalse(PlaceCountry.objects.exists())


class ResolverPlaceTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_user("tester", password="x"))

    def make(self, country):
        artist = Artist.objects.create(artist_name="Band", artist_country=country)
        Album.objects.create(artist=artist, album_name="Record")
        resolver.resolve_artists([artist.pk])
        artist.refresh_from_db()
        return artist

    def test_city_tag_becomes_country_with_the_city_kept(self):
        artist = self.make("Budapest")
        self.assertEqual((artist.resolved_country, artist.resolved_place), ("Hungary", "Budapest"))
        self.assertEqual(artist.artist_country, "Budapest")  # the tag itself is untouched
        page = self.client.get(reverse("library:artist-detail", args=(artist.pk,)))
        self.assertContains(page, "Hungary")
        self.assertContains(page, "(Budapest)")
        self.assertContains(self.client.get(reverse("library:artist-list")), "(Budapest)")

    def test_plain_country_has_no_place(self):
        artist = self.make("Hungary")
        self.assertEqual((artist.resolved_country, artist.resolved_place), ("Hungary", ""))

    def test_metal_archives_country_is_used_for_a_city_tag(self):
        artist = Artist.objects.create(artist_name="Band", artist_country="Smalltown")
        MetalArchivesArtist.objects.create(artist=artist, status="matched", country="Norway")
        resolver.resolve_artists([artist.pk])
        artist.refresh_from_db()
        self.assertEqual((artist.resolved_country, artist.resolved_place), ("Norway", "Smalltown"))
        self.assertEqual(artist.resolved_sources["country"], "metal-archives")

    def test_unknown_text_is_shown_as_before(self):
        with patch.object(places.NominatimClient, "country_of", return_value=""):
            artist = self.make("Zzyzxqw Land")
        self.assertEqual((artist.resolved_country, artist.resolved_place), ("Zzyzxqw Land", ""))
