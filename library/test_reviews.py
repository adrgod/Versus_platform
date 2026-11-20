import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Album, Review, UserPreference
from .reviews import clean_html, default_body
from .services.importer import import_records


def post(client, url, body):
    return client.post(url, data=json.dumps(body), content_type="application/json")


class CleaningTests(TestCase):
    def test_scripts_attributes_and_bad_links_are_removed(self):
        html, text = clean_html(
            '<p onclick="x()">Hi <b>there</b></p><script>alert(1)</script>'
            '<a href="javascript:alert(1)">bad</a> <a href="https://e.com/a?b=1&c=2" style="x">ok</a><img src=x onerror=y>')
        self.assertEqual(html, '<p>Hi <b>there</b></p>bad <a href="https://e.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">ok</a>')
        self.assertEqual(text, "Hi there\nbad ok")

    def test_unclosed_tags_are_closed_and_text_is_escaped(self):
        html, _ = clean_html("<ul><li>a < b</li><li><strong>x")
        self.assertEqual(html, "<ul><li>a &lt; b</li><li><strong>x</strong></li></ul>")


class ReviewTests(TestCase):
    def setUp(self):
        import_records([{"artist": "Exodus", "album": "Bonded by Blood", "title": "Piranha", "tracknumber": "1",
                         "file_path": "/music/1.mp3", "organization": "Torrid"}])
        self.album = Album.objects.get()
        self.user = get_user_model().objects.create_user("adriano", password="x")
        self.client.force_login(self.user)
        self.url = reverse("library:review-save", args=(self.album.pk,))

    def test_new_album_page_offers_the_default_text(self):
        page = self.client.get(reverse("library:album-detail", args=(self.album.pk,)))
        self.assertContains(page, "<p><strong>EXODUS</strong></p><p>«Bonded by Blood»</p>", html=False)
        self.assertContains(page, "data-review-save")

    def test_default_body_skips_missing_label(self):
        self.assertNotIn("(", default_body(self.album, ""))
        self.assertIn("<p>(Torrid)</p>", default_body(self.album, "Torrid"))

    def test_album_rating_and_review_score_are_one_value_out_of_ten(self):
        from .models import AlbumRating
        rate = reverse("library:rate", args=("album", self.album.pk))
        post(self.client, rate, {"stars": 7})
        self.assertEqual(AlbumRating.objects.get().stars, 7)
        self.assertEqual(post(self.client, rate, {"stars": 11}).status_code, 400)
        page = self.client.get(reverse("library:album-detail", args=(self.album.pk,)))
        self.assertContains(page, 'data-review-score data-value="7"')  # the review box shows the same score
        post(self.client, self.url, {"score": 9, "html": "<p>x</p>", "author": ""})
        self.assertEqual(AlbumRating.objects.get().stars, 9)  # saving the review sets the rating
        post(self.client, rate, {"stars": 4})
        self.assertEqual(Review.objects.get().score, 4)  # rating the album sets the review's score
        self.assertEqual(post(self.client, rate, {"stars": 0}).json()["stars"], 0)  # the cross removes the rating
        self.assertFalse(AlbumRating.objects.exists())
        self.assertEqual(Review.objects.get().score, 0)  # and the review's score, but the review stays
        page = self.client.get(reverse("library:album-detail", args=(self.album.pk,)))
        self.assertContains(page, "data-rate-clear")
        self.assertContains(page, "data-review-clear")
        self.assertContains(page, f'data-rate-url="{rate}"')
        self.assertContains(self.client.get(reverse("library:review-list")), "no score")

    def test_clearing_a_rating_without_a_review(self):
        from .models import AlbumRating
        rate = reverse("library:rate", args=("album", self.album.pk))
        post(self.client, rate, {"stars": 5})
        self.assertEqual(post(self.client, rate, {"stars": 0}).json()["stars"], 0)
        self.assertFalse(AlbumRating.objects.exists())
        self.assertFalse(Review.objects.exists())

    def test_save_update_and_name_fallback(self):
        result = post(self.client, self.url, {"score": 8, "html": "<p>Great <b>riffs</b></p>", "author": ""})
        self.assertEqual(result.status_code, 200)
        review = Review.objects.get()
        self.assertEqual((review.score, review.author_name, review.user), (8, "adriano", self.user))  # login name
        self.assertEqual(review.body_text, "Great riffs")
        post(self.client, self.url, {"score": 9, "html": "<p>Better</p>", "author": "Adriano Godinho"})
        self.assertEqual(Review.objects.count(), 1)
        review = Review.objects.get()
        self.assertEqual((review.score, review.author_name), (9, "Adriano Godinho"))
        self.assertEqual(UserPreference.objects.get().display_name, "Adriano Godinho")  # remembered on the account
        page = self.client.get(reverse("library:album-detail", args=(self.album.pk,)))
        self.assertContains(page, 'value="Adriano Godinho"')
        self.assertContains(page, "Better")

    def test_score_is_required_and_limited(self):
        for score in (0, 11, "x"):
            self.assertIn(post(self.client, self.url, {"score": score, "html": "x"}).status_code, (400,))
        self.assertFalse(Review.objects.exists())

    def test_reviews_are_searchable_and_deletable(self):
        post(self.client, self.url, {"score": 7, "html": "<p>Thrash perfection</p>", "author": "A"})
        Album.objects.filter(pk=self.album.pk).update(resolved_label="Torrid Records")
        listing = reverse("library:review-list")
        self.assertContains(self.client.get(listing, {"q": "perfection"}), "Bonded by Blood")
        self.assertContains(self.client.get(listing, {"q": "torrid"}), "Bonded by Blood")
        self.assertNotContains(self.client.get(listing, {"q": "zzz"}), "Bonded by Blood")
        post(self.client, reverse("library:review-delete", args=(self.album.pk,)), {})
        self.assertFalse(Review.objects.exists())

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(post(self.client, self.url, {"score": 5}).status_code, 302)
        self.assertEqual(self.client.get(reverse("library:review-list")).status_code, 302)
