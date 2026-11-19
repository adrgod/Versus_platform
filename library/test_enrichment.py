from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Track, Album, AlbumFacet, AlbumProfile, Artist, ArtistFacet, ArtistProfile, EnrichmentStatus
from .services import enrichment, explorer
from .services.importer import import_records

BAND_ID = "11111111-1111-1111-1111-111111111111"
OTHER_BAND_ID = "22222222-2222-2222-2222-222222222222"
GROUP_ID = "33333333-3333-3333-3333-333333333333"
RELEASE_ID = "44444444-4444-4444-4444-444444444444"


class FakeMusicBrainz:
    """Answers like the MusicBrainz web service, for two bands that share a name."""

    def __init__(self):
        self.calls = []

    def search_artists(self, name, limit=10):
        self.calls.append(("search", name))
        if "nobody" in name.lower():
            return []
        return [
            {"id": OTHER_BAND_ID, "name": "Exodus", "score": 100, "country": "PL"},
            {"id": BAND_ID, "name": "Exodus", "score": 99, "country": "US"},
        ]

    def release_groups(self, mbid, max_items=500):
        self.calls.append(("release-groups", mbid))
        if mbid == OTHER_BAND_ID:
            return [{"id": "x", "title": "Supernova", "primary-type": "Album", "first-release-date": "1980"}]
        return [
            {"id": GROUP_ID, "title": "Bonded by Blood", "primary-type": "Album", "secondary-types": [],
             "first-release-date": "1985-04-25", "rating": {"value": 4.4, "votes-count": 5},
             "genres": [{"name": "thrash metal", "count": 9}]},
            {"id": "live", "title": "Bonded by Blood", "primary-type": "Album", "secondary-types": ["Live"],
             "first-release-date": "2009"},
            {"id": "pleasures", "title": "Pleasures of the Flesh", "primary-type": "Album",
             "secondary-types": [], "first-release-date": "1987"},
        ]

    def artist(self, mbid):
        return {
            "id": mbid, "name": "Exodus", "type": "Group", "country": "US",
            "area": {"name": "United States"}, "begin-area": {"name": "Richmond"},
            "life-span": {"begin": "1979", "ended": False},
            "rating": {"value": 4.25, "votes-count": 13},
            "genres": [{"name": "thrash metal", "count": 67}, {"name": "heavy metal", "count": 20},
                       {"name": "polka", "count": 1}],
            "relations": [
                {"type": "member of band", "direction": "backward", "attributes": ["guitar", "original"],
                 "begin": "1979", "end": "1983", "ended": True, "artist": {"id": "p1", "name": "Kirk Hammett"}},
                {"type": "member of band", "direction": "backward", "attributes": ["drums (drum set)"],
                 "begin": "1979", "ended": False, "artist": {"id": "p2", "name": "Tom Hunting"}},
            ],
        }

    def release_group(self, mbid):
        return {"id": mbid, "title": "Set by hand", "primary-type": "Album", "first-release-date": "1990"}

    def releases_of_group(self, mbid):
        return [{"id": "later", "date": "1999"}, {"id": RELEASE_ID, "date": "1985-04-25"}]

    def release(self, mbid):
        return {
            "id": mbid,
            "relations": [
                {"type": "design/illustration", "target-type": "artist", "artist": {"id": "a1", "name": "Rob Sandoval"}},
            ],
            "label-info": [{"catalog-number": "TT 001", "label": {"id": "l1", "name": "Torrid Records"}}],
            "media": [{"tracks": [{"recording": {"relations": [
                {"type": "recorded at", "target-type": "place", "place": {"id": "s1", "name": "Prairie Sun", "area": {"name": "Cotati"}}},
                {"type": "performance", "target-type": "work", "work": {"id": "w1", "relations": [
                    {"type": "composer", "target-type": "artist", "artist": {"id": "c1", "name": "Kirk Hammett"}},
                    {"type": "lyricist", "target-type": "artist", "artist": {"id": "c2", "name": "Outside Writer"}},
                ]}},
                {"type": "producer", "target-type": "artist", "artist": {"id": "pr", "name": "Mark Whitaker"}},
                {"type": "mix", "target-type": "artist", "artist": {"id": "pr", "name": "Mark Whitaker"}},
                {"type": "instrument", "target-type": "artist", "attributes": ["electric guitar"],
                 "artist": {"id": "g", "name": "Gary Holt"}},
            ]}}]}],
        }


def record(**overrides):
    base = {"artist": "Exodus", "album": "Bonded By Blood (Remastered)", "date": "1985",
            "title": "Bonded by Blood", "tracknumber": "1", "file_path": "/music/1.mp3", "genre": "Thrash Metal"}
    base.update(overrides)
    return base


class MatchingTests(TestCase):
    def test_normalize_ignores_spelling_differences(self):
        self.assertEqual(enrichment.normalize("The Number Of The Beast (2015 Remaster)"), "number of the beast")
        self.assertEqual(enrichment.similarity("Motörhead", "Motorhead"), 1.0)
        self.assertEqual(enrichment.similarity("Simon & Garfunkel", "Simon and Garfunkel"), 1.0)
        self.assertLess(enrichment.similarity("Metallica", "Megadeth"), enrichment.NAME_THRESHOLD)

    def test_parse_mbid_from_link(self):
        self.assertEqual(enrichment.parse_mbid(f"https://musicbrainz.org/artist/{BAND_ID}/releases"), BAND_ID)
        self.assertIsNone(enrichment.parse_mbid("not an id"))

    def test_studio_album_preferred_over_live_namesake(self):
        groups = FakeMusicBrainz().release_groups(BAND_ID)
        match, score = enrichment.best_release_group("Bonded by Blood", groups, 1985)
        self.assertEqual(match["id"], GROUP_ID)


class EnrichmentTests(TestCase):
    def setUp(self):
        import_records([record(), record(artist="Nobody Known", album="Lost", file_path="/music/2.mp3")])
        self.client_api = FakeMusicBrainz()

    def test_enrich_library_stores_profiles_and_facets(self):
        stats = enrichment.enrich_library(self.client_api)
        self.assertEqual((stats.artists_matched, stats.artists_not_found, stats.albums_matched), (1, 1, 1))

        profile = ArtistProfile.objects.get(artist__artist_name="Exodus")
        # Two bands are called Exodus; the one whose discography has our album wins.
        self.assertEqual(profile.external_id, BAND_ID)
        self.assertEqual((profile.country, profile.begin_year, profile.studio_album_count), ("United States", 1979, 2))
        self.assertEqual([m["instruments"] for m in profile.lineup], [["guitar"], ["drums"]])
        self.assertTrue(profile.lineup[0]["original"])
        self.assertTrue(profile.lineup[1]["current"])

        artist = Artist.objects.get(artist_name="Exodus")
        # File tags are untouched: country/year stay part of the importer's identity.
        self.assertEqual((artist.artist_country, artist.artist_formed_year), ("", None))
        genres = set(ArtistFacet.objects.filter(artist=artist, facet__kind="genre").values_list("facet__name", flat=True))
        self.assertEqual(genres, {"thrash metal", "heavy metal"})  # "polka" has too few votes

        album = AlbumProfile.objects.get(album__artist=artist)
        self.assertEqual((album.external_id, album.release_id, album.first_release_date), (GROUP_ID, RELEASE_ID, "1985-04-25"))
        links = {(link.facet.kind, link.facet.name, link.detail) for link in AlbumFacet.objects.select_related("facet")}
        self.assertIn(("studio", "Prairie Sun", "recorded"), links)
        self.assertIn(("engineer", "Mark Whitaker", "mixing"), links)
        self.assertIn(("producer", "Mark Whitaker", "producer"), links)
        self.assertIn(("artwork", "Rob Sandoval", "design / illustration"), links)
        self.assertIn(("musician", "Gary Holt", "electric guitar"), links)
        self.assertIn(("label", "Torrid Records", "TT 001"), links)

        self.assertEqual(ArtistProfile.objects.get(artist__artist_name="Nobody Known").status, EnrichmentStatus.NOT_FOUND)

    def test_album_not_found_online_still_gets_artist_genres_and_country(self):
        import_records([record(album="Unlisted Demo", date="1983", file_path="/music/3.mp3", genre="")])
        enrichment.enrich_library(self.client_api)
        demo = AlbumProfile.objects.get(album__album_name="Unlisted Demo")
        self.assertEqual(demo.status, EnrichmentStatus.NOT_FOUND)
        self.assertEqual(demo.country, "United States")
        links = {(link.facet.kind, link.facet.name) for link in AlbumFacet.objects.filter(album=demo.album).select_related("facet")}
        self.assertIn(("genre", "thrash metal"), links)
        self.assertNotIn(("member", "Tom Hunting"), links)  # members belong to the artist page only
        self.assertEqual(demo.album.album_genre, "thrash metal, heavy metal")

    def test_album_lookup_reuses_complete_artist_without_artist_requests(self):
        enrichment.enrich_library(self.client_api)
        album = Album.objects.get(album_name__startswith="Bonded")
        again = FakeMusicBrainz()
        enrichment.enrich_library(again, album_ids=[album.pk])
        kinds = {call[0] for call in again.calls}
        self.assertNotIn("search", kinds)
        self.assertEqual(AlbumProfile.objects.get(album=album).status, EnrichmentStatus.MATCHED)

    def test_album_lookup_fetches_artist_when_its_tags_are_missing(self):
        enrichment.enrich_library(self.client_api)
        album = Album.objects.get(album_name__startswith="Bonded")
        ArtistFacet.objects.filter(artist=album.artist, facet__kind="genre").delete()
        again = FakeMusicBrainz()
        enrichment.enrich_library(again, album_ids=[album.pk])
        self.assertIn("search", {call[0] for call in again.calls})
        self.assertTrue(ArtistFacet.objects.filter(artist=album.artist, facet__kind="genre").exists())

    def test_second_run_skips_what_was_looked_up(self):
        enrichment.enrich_library(self.client_api)
        self.client_api.calls.clear()
        stats = enrichment.enrich_library(self.client_api)
        self.assertEqual(stats.artists_total, 0)
        self.assertEqual(self.client_api.calls, [])

    def test_refresh_replaces_links_without_duplicates(self):
        enrichment.enrich_library(self.client_api)
        count = AlbumFacet.objects.count()
        enrichment.enrich_library(self.client_api, refresh=True)
        self.assertEqual(AlbumFacet.objects.count(), count)

    def test_manual_id_skips_search(self):
        artist = Artist.objects.get(artist_name="Nobody Known")
        enrichment.set_artist_id(artist, f"https://musicbrainz.org/artist/{BAND_ID}")
        enrichment.enrich_library(self.client_api, artist_ids=[artist.pk])
        self.assertNotIn(("search", "Nobody Known"), self.client_api.calls)
        self.assertEqual(ArtistProfile.objects.get(artist=artist).status, EnrichmentStatus.MATCHED)


class ExplorerTests(TestCase):
    def setUp(self):
        import_records([
            record(),
            record(artist="Kreator", album="Pleasure to Kill", date="1986", genre="Thrash Metal", file_path="/m/3.mp3"),
            record(artist="Enya", album="Watermark", date="1988", genre="New Age", file_path="/m/4.mp3"),
            record(artist="Enya", album="Shepherd Moons", date="1991", genre="New Age", file_path="/m/5.mp3"),
        ])
        enrichment.enrich_library(FakeMusicBrainz(), artist_ids=[Artist.objects.get(artist_name="Exodus").pk])

    def albums_for(self, *params):
        result = explorer.explore(explorer.parse_selection(params))
        return set(Album.objects.filter(id__in=result.album_ids).values_list("album_name", flat=True)), result

    def test_or_within_group_and_across_groups(self):
        names, _ = self.albums_for("genre:thrash metal")
        self.assertEqual(names, {"Bonded By Blood (Remastered)", "Pleasure to Kill"})
        names, _ = self.albums_for("genre:thrash metal", "genre:new age")
        self.assertEqual(len(names), 4)
        names, _ = self.albums_for("genre:new age", "decade:1980s")
        self.assertEqual(names, {"Watermark"})
        names, _ = self.albums_for("genre:thrash metal", "member:kirk hammett")
        self.assertEqual(names, {"Bonded By Blood (Remastered)"})

    def test_counts_reflect_other_groups(self):
        _, result = self.albums_for("decade:1980s")
        genre = next(group for group in result.groups if group.key == "genre")
        counts = {row["name"].casefold(): row["count"] for row in genre.rows}
        self.assertEqual(counts["new age"], 1)  # Shepherd Moons is from the 1990s
        decade = next(group for group in result.groups if group.key == "decade")
        self.assertEqual({row["name"]: row["count"] for row in decade.rows}, {"1980s": 3, "1990s": 1})

    def test_pages_render(self):
        user = get_user_model().objects.create_user(username="explorer", password="not-a-real-password")
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)
        response = client.get(reverse("library:explore"), {"f": ["genre:thrash metal", "studio:prairie sun"]})
        self.assertContains(response, "Bonded By Blood")
        self.assertNotContains(response, "Pleasure to Kill")
        self.assertContains(client.get(reverse("library:decade-index")), "1980 – 1989")
        with patch("library.views.enrichment_service.start_enrichment") as start_lookup:
            lookup_page = client.get(reverse("library:enrichment"))
        self.assertEqual(lookup_page.status_code, 200)
        self.assertContains(lookup_page, "Lookup Details Online")
        start_lookup.assert_not_called()
        exodus = Artist.objects.get(artist_name="Exodus")
        artist_page = client.get(reverse("library:artist-detail", args=(exodus.pk,)))
        self.assertContains(artist_page, "Kirk Hammett")
        self.assertContains(artist_page, "Pleasures of the Flesh")
        album_page = client.get(reverse("library:album-detail", args=(exodus.album_set.get().pk,)))
        self.assertContains(album_page, "Prairie Sun")
        self.assertContains(album_page, "Rob Sandoval")


class FlakyMusicBrainz(FakeMusicBrainz):
    """Fails every release lookup, like a MusicBrainz that keeps answering 503."""

    def release(self, mbid):
        raise enrichment.MusicBrainzError("MusicBrainz stayed busy")


class ReviewFixTests(TestCase):
    def setUp(self):
        import_records([record(), record(album="Pleasures of the Flesh", date="1987", file_path="/music/9.mp3")])

    def test_album_failure_keeps_artist_matched_and_other_albums_going(self):
        stats = enrichment.enrich_library(FlakyMusicBrainz())
        profile = ArtistProfile.objects.get()
        self.assertEqual(profile.status, EnrichmentStatus.MATCHED)
        self.assertEqual(AlbumProfile.objects.filter(status=EnrichmentStatus.ERROR).count(), 2)
        self.assertEqual(len(stats.errors), 2)
        # The failed albums are retried by the next normal run.
        self.assertEqual(enrichment.artists_to_enrich(), [profile.artist])

    def test_unexpected_error_does_not_stop_the_run(self):
        import_records([record(artist="Second Band", album="Other", file_path="/music/8.mp3")])

        class Broken(FakeMusicBrainz):
            def search_artists(self, name, limit=10):
                if name == "Exodus":
                    raise KeyError("id")
                return super().search_artists(name, limit)

        stats = enrichment.enrich_library(Broken())
        self.assertEqual(stats.artists_done, 2)
        self.assertEqual(ArtistProfile.objects.get(artist__artist_name="Exodus").status, EnrichmentStatus.ERROR)
        self.assertIn("KeyError", stats.errors[0])

    def test_pending_album_is_picked_up_although_another_album_is_done(self):
        enrichment.enrich_library(FakeMusicBrainz())
        album = Album.objects.get(album_name="Pleasures of the Flesh")
        enrichment.set_album_id(album, f"https://musicbrainz.org/release-group/{GROUP_ID}")
        self.assertEqual(enrichment.artists_to_enrich(), [album.artist])

    def test_instrument_credit_without_instrument_has_no_role(self):
        credits = enrichment._credits({"relations": [
            {"type": "instrument", "target-type": "artist", "attributes": [], "artist": {"id": "x", "name": "Session Player"}},
        ]})
        self.assertEqual(credits[0]["role"], "")

    def test_rating_band_matches_displayed_rounding(self):
        self.assertEqual(explorer.rating_band(4.45), "4.5 – 5")
        self.assertEqual(explorer.rating_band(4.44), "4.0 – 4.4")
        self.assertEqual(explorer.rating_band(2.96), "3.0 – 3.4")

    def test_lookup_errors_are_listed_on_the_page(self):
        from unittest.mock import patch
        from . import enrichment_service

        job = enrichment_service.BackgroundJob(kind="musicbrainz", running=False, stats=enrichment.EnrichmentStats())
        job.stats.errors = ["Exodus: MusicBrainz stayed busy"]
        user = get_user_model().objects.create_user(username="lookup", password="not-a-real-password")
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)
        with patch.object(enrichment_service, "get_job", return_value=job):
            response = client.get(reverse("library:enrichment"))
        self.assertContains(response, "Exodus: MusicBrainz stayed busy")
        self.assertNotContains(response, "{{")


class AlbumInheritsArtistTagsTests(TestCase):
    def test_genre_and_country_are_copied_onto_the_album(self):
        import_records([record(genre="")])
        enrichment.enrich_library(FakeMusicBrainz())
        album = Album.objects.get(album_name__startswith="Bonded")
        AlbumFacet.objects.filter(album=album, facet__kind="genre").delete()
        Album.objects.filter(pk=album.pk).update(album_genre="")
        enrichment.inherit_artist_tags(album)  # no network
        album.refresh_from_db()
        self.assertEqual(album.album_genre, "thrash metal, heavy metal")
        self.assertEqual(album.profile.country, "United States")
        names = set(AlbumFacet.objects.filter(album=album, facet__kind="genre").values_list("facet__name", flat=True))
        self.assertEqual(names, {"thrash metal", "heavy metal"})

    def test_pages_show_only_the_albums_own_data(self):
        import_records([record(genre="")])
        enrichment.enrich_library(FakeMusicBrainz())
        album = Album.objects.get(album_name__startswith="Bonded")
        AlbumFacet.objects.filter(album=album, facet__kind="member").delete()
        get_user_model().objects.create_user("u", password="p")
        client = Client()
        client.login(username="u", password="p")
        page = client.get(reverse("library:album-detail", args=(album.pk,))).content.decode()
        self.assertIn("United States", page)
        self.assertNotIn("Tom Hunting", page)


class ArtistFilterTests(TestCase):
    def setUp(self):
        import_records([
            record(),
            record(artist="Second Band", album="Quiet", date="2001", file_path="/music/5.mp3", genre="Ambient; Drone"),
        ])
        enrichment.enrich_library(FakeMusicBrainz())
        get_user_model().objects.create_user("u", password="p")
        self.web = Client()
        self.web.login(username="u", password="p")

    def names(self, **params):
        response = self.web.get(reverse("library:artist-list"), params)
        return [a.artist_name for a in response.context["artists"]]

    def test_each_filter_narrows_the_list(self):
        self.assertEqual(self.names(), ["Exodus", "Second Band"])
        self.assertEqual(self.names(country="United States"), ["Exodus"])
        self.assertEqual(self.names(genre="Ambient"), ["Second Band"])
        self.assertEqual(self.names(genre="thrash metal"), ["Exodus"])
        self.assertEqual(self.names(year="2001"), ["Second Band"])
        self.assertEqual(self.names(albums="1"), ["Exodus", "Second Band"])
        self.assertEqual(self.names(albums="7+"), [])
        self.assertEqual(self.names(voice="instrumental"), [])


class CompletenessGroupingTests(TestCase):
    def setUp(self):
        import_records([
            record(),
            record(artist="Second Band", album="Quiet", date="2001", file_path="/music/5.mp3", genre="Ambient; Drone", title="Quiet Track"),
        ])
        Album.objects.filter(album_name="Quiet").update(album_publisher="Calm Records")
        Track.objects.filter(track_title="Quiet Track").update(track_title="")
        enrichment.enrich_library(FakeMusicBrainz())
        get_user_model().objects.create_user("u", password="p")
        self.web = Client()
        self.web.login(username="u", password="p")

    def groups(self, by):
        with patch("library.views.repository.get_config", return_value={"quality_attributes": ["title", "genre"]}):
            response = self.web.get(reverse("library:completeness-report"), {"by": by})
        self.assertEqual(response.status_code, 200)
        return {g["label"]: g for g in response.context["groups"]}, response

    def test_every_grouping_renders_with_percentages(self):
        for by in ("batch", "label", "country", "decade", "year", "genre", "nonsense"):
            groups, response = self.groups(by)
            self.assertTrue(groups, by)
            self.assertContains(response, "% complete overall")

    def test_groups_and_their_percentages(self):
        groups, _ = self.groups("genre")
        self.assertEqual(set(groups), {"Thrash Metal", "Ambient"})
        self.assertEqual(groups["Thrash Metal"]["percent"], 100)
        self.assertEqual(groups["Ambient"]["percent"], 50)  # title missing, genre present
        self.assertEqual(groups["Ambient"]["artists"][0]["percent"], 50)
        self.assertEqual(set(self.groups("decade")[0]), {"1980s", "2000s"})
        self.assertIn("Calm Records", self.groups("label")[0])
        self.assertIn("Torrid Records", self.groups("label")[0])  # label found online when the tag is empty


class AlbumDetailsTableTests(TestCase):
    def page(self, **overrides):
        import_records([record(**overrides)])
        album = Album.objects.get(album_name__startswith="Bonded")
        get_user_model().objects.create_user("u", password="p")
        client = Client()
        client.login(username="u", password="p")
        return client.get(reverse("library:album-detail", args=(album.pk,))), album

    def test_details_table_replaces_the_header_tags(self):
        response, album = self.page(genre="Thrash Metal; Speed Metal")
        Album.objects.filter(pk=album.pk).update(album_publisher="Torrid Records")
        page = response.content.decode()
        self.assertIn('aria-label="Album details"', page)
        self.assertNotIn("YEAR UNKNOWN", page)
        self.assertNotIn("GENRE UNKNOWN", page)
        self.assertEqual(response.context["main_genre"], "Thrash Metal")
        self.assertEqual(response.context["other_genres"], ["Speed Metal"])

    def test_missing_values_leave_the_cell_empty(self):
        response, _ = self.page(genre="", date="")
        page = response.content.decode()
        self.assertEqual(response.context["main_genre"], "")
        self.assertGreaterEqual(page.count("credit-group is-empty"), 3)  # label, country, year, genre


class AlbumCreditsTableTests(TestCase):
    def test_fixed_credit_cells_with_composer_and_lyricist_rules(self):
        import_records([record()])
        enrichment.enrich_library(FakeMusicBrainz())
        album = Album.objects.get(album_name__startswith="Bonded")
        get_user_model().objects.create_user("u", password="p")
        client = Client()
        client.login(username="u", password="p")
        response = client.get(reverse("library:album-detail", args=(album.pk,)))
        cells = {cell["label"]: cell["entries"] for cell in response.context["credit_cells"]}
        self.assertEqual([e["name"] for e in cells["Main composer"]], ["Kirk Hammett"])  # a band member
        self.assertEqual([e["name"] for e in cells["Main lyricist"]], ["Exodus"])  # not in the band: the band
        self.assertEqual((cells["Recording studio"][0]["name"], cells["Recording studio"][0]["detail"]), ("Prairie Sun", "Cotati"))
        self.assertEqual([e["name"] for e in cells["Producer"]], ["Mark Whitaker"])
        self.assertEqual([e["name"] for e in cells["Sound engineer"]], ["Mark Whitaker"])
        self.assertEqual([e["name"] for e in cells["Artwork artist"]], ["Rob Sandoval"])
        page = response.content.decode()
        credits = page[page.index('aria-label="Album credits"'):page.index('class="track-section"')]
        self.assertNotIn("Torrid Records", credits)  # the label lives in Details, not in Credits
        self.assertIn("Torrid Records", page)

    def test_empty_cells_stay_visible_when_nothing_is_known(self):
        import_records([record()])
        album = Album.objects.get(album_name__startswith="Bonded")
        get_user_model().objects.create_user("u", password="p")
        client = Client()
        client.login(username="u", password="p")
        page = client.get(reverse("library:album-detail", args=(album.pk,))).content.decode()
        self.assertEqual(page.count("credit-group is-empty"), 6 + 2)  # six credits plus label and country (the year is known)


class LocalCreditSourceTests(TestCase):
    def test_tag_credits_count_names_across_tracks(self):
        from .models import AlbumCredit
        from .services import local_credits
        import_records([record(), record(title="Second", tracknumber="2", file_path="/music/2.mp3")])
        album = Album.objects.get()
        def tag_values(path, key):
            if key != "composer":
                return []
            return ["Gary Holt", "Rob Dukes"] if path.endswith("1.mp3") else ["Gary Holt"]

        with patch("library.services.local_credits.Path.exists", return_value=True), \
                patch("library.services.local_credits._tag_values", side_effect=tag_values):
            local_credits.refresh_tag_credits(album)
        found = {c.name: c.count for c in AlbumCredit.objects.filter(album=album, kind="composer", source="tags")}
        self.assertEqual(found, {"Gary Holt": 2, "Rob Dukes": 1})

    def test_notes_credits_need_a_quote_that_contains_the_name(self):
        from .services import insights
        text = "Produced by Mark Whitaker at Prairie Sun Studios. Cover art by Rob Sandoval."
        cleaned = insights.clean_credits([
            {"role": "producer", "name": "Mark Whitaker", "evidence": "Produced by Mark Whitaker at Prairie Sun Studios"},
            {"role": "artwork", "name": "Someone Else", "evidence": "Cover art by Rob Sandoval."},   # name not in quote
            {"role": "engineer", "name": "Invented Person", "evidence": "Engineered by Invented Person at the moon"},  # not in text
            {"role": "producer", "name": "Mark Whitaker", "evidence": "Produced by Mark Whitaker at Prairie Sun Studios"},  # duplicate
        ], text)
        self.assertEqual([(c["role"], c["name"]) for c in cleaned], [("producer", "Mark Whitaker")])

    def test_cells_fall_through_sources_and_say_which_one_was_used(self):
        from .views import _credit_cells
        credits = {
            "musicbrainz": [{"kind": "producer", "name": "MB Producer", "role": "producer", "area": "", "count": 0}],
            "tags": [{"kind": "composer", "name": "Gary Holt", "role": "", "area": "", "count": 4, "evidence": "Composer tag on 4 tracks"}],
            "notes": [
                {"kind": "producer", "name": "Notes Producer", "role": "", "area": "", "count": 0},
                {"kind": "artwork", "name": "Rob Sandoval", "role": "", "area": "", "count": 0, "evidence": "Cover art by Rob Sandoval"},
            ],
        }
        cells = {c["label"]: c for c in _credit_cells(credits, "Exodus", [{"name": "Gary Holt"}])}
        self.assertEqual((cells["Main composer"]["entries"][0]["name"], cells["Main composer"]["source"]), ("Gary Holt", "MP3 tags"))
        self.assertEqual((cells["Producer"]["entries"][0]["name"], cells["Producer"]["source"]), ("MB Producer", "MusicBrainz"))
        self.assertEqual((cells["Artwork artist"]["entries"][0]["name"], cells["Artwork artist"]["source"]), ("Rob Sandoval", "Liner notes"))
        self.assertEqual((cells["Sound engineer"]["entries"], cells["Sound engineer"]["source"]), ([], ""))
