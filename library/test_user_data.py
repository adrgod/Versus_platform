import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import AlbumRating, PlayEvent, Track, TrackRating, UserPreference
from .services.importer import import_records


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


class UserDataTests(TestCase):
    def setUp(self):
        import_records([{"artist": "Exodus", "album": "Bonded by Blood", "title": "Piranha",
                         "tracknumber": "1", "file_path": "/music/1.mp3"}])
        self.track = Track.objects.get()
        User = get_user_model()
        self.user = User.objects.create_user("ana", password="x")
        self.other = User.objects.create_user("bob", password="x")
        self.client.force_login(self.user)

    def test_preferences_are_saved_per_user_and_come_back_on_the_next_login(self):
        post(self.client, reverse("library:preferences"),
             {"display": {"theme": "dark", "accent": "teal"}, "eq": {"60": 4, "250": -2}, "volume": 0.3})
        post(self.client, reverse("library:preferences"), {"eq": {"1000": 5}})  # merges, keeps the rest
        self.client.logout()
        self.client.force_login(self.user)
        data = self.client.get(reverse("library:preferences")).json()
        self.assertEqual(data["display"], {"theme": "dark", "accent": "teal"})
        self.assertEqual(data["eq"], {"60": 4, "250": -2, "1000": 5})
        self.assertEqual(data["volume"], 0.3)
        # embedded in the page for the first paint, and not shared with other users
        page = self.client.get(reverse("library:dashboard"))
        self.assertContains(page, 'id="user-prefs"')
        self.assertContains(page, '"theme": "dark"')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("library:preferences")).json()["display"], {})

    def test_bad_values_are_dropped_or_clamped(self):
        post(self.client, reverse("library:preferences"),
             {"display": {"theme": "<script>", "bogus": "x", "grid": "compact"},
              "eq": {"60": 99, "7": 1}, "volume": 5})
        row = UserPreference.objects.get(user=self.user)
        self.assertEqual(row.display, {"grid": "compact"})
        self.assertEqual(row.eq, {"60": 12})
        self.assertEqual(row.volume, 1.0)
        self.assertEqual(self.client.post(reverse("library:preferences"), "nope",
                                          content_type="application/json").status_code, 400)

    def test_plays_are_recorded_for_the_user_and_listed_in_history(self):
        post(self.client, reverse("library:record-play", args=(self.track.pk,)), {})
        self.assertEqual(PlayEvent.objects.filter(user=self.user).count(), 1)
        self.assertContains(self.client.get(reverse("library:history")), "Piranha")
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("library:history")), "Piranha")

    def test_ratings_are_set_changed_and_cleared(self):
        url = reverse("library:rate", args=("track", self.track.pk))
        post(self.client, url, {"stars": 4})
        post(self.client, url, {"stars": 5})
        self.assertEqual(TrackRating.objects.get(user=self.user).stars, 5)
        album_url = reverse("library:rate", args=("album", self.track.album_id))
        post(self.client, album_url, {"stars": 3})
        self.assertEqual(AlbumRating.objects.get().stars, 3)
        self.assertEqual(post(self.client, url, {"stars": 9}).status_code, 400)
        page = self.client.get(reverse("library:album-detail", args=(self.track.album_id,)))
        self.assertContains(page, 'data-rate-value="5"')
        post(self.client, url, {"stars": 0})
        self.assertFalse(TrackRating.objects.exists())

    def test_everything_needs_a_login(self):
        self.client.logout()
        for url in (reverse("library:preferences"), reverse("library:history")):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(post(self.client, reverse("library:record-play", args=(self.track.pk,)), {}).status_code, 302)
