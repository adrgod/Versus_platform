from django.test import TestCase

from .models import Album, Artist, EnrichmentStatus, MetalArchivesAlbum, MetalArchivesArtist
from .services import enrichment, metal_archives as ma
from .services.importer import import_records

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

BAND_HTML = """
<div id="band_stats">
  <dl class="float_left">
    <dt>Country of origin:</dt><dd><a href="https://www.metal-archives.com/lists/US">United States</a></dd>
    <dt>Location:</dt><dd>Richmond, California</dd>
    <dt>Status:</dt><dd class="active">Active</dd>
    <dt>Formed in:</dt><dd>1979</dd>
  </dl>
  <dl class="float_right">
    <dt>Genre:</dt><dd>Thrash Metal (early); Groove Metal (later); Thrash Metal</dd>
    <dt>Current label:</dt><dd><a href="https://www.metal-archives.com/labels/Nuclear_Blast/2">Nuclear Blast</a></dd>
  </dl>
</div>"""

ALBUM_HTML = """
<a class="image" id="cover" title="Exodus - Bonded by Blood" href="https://www.metal-archives.com/images/1/2/3/123.jpg?1"><img src="x"></a>
<dl class="float_left"><dt>Type:</dt><dd>Full-length</dd><dt>Release date:</dt><dd>April 25th, 1985</dd></dl>
<dl class="float_right"><dt>Label:</dt><dd><a href="https://www.metal-archives.com/labels/Torrid/9">Torrid Records</a></dd></dl>"""

EXODUS_PL = "https://www.metal-archives.com/bands/Exodus/777"
EXODUS_US = "https://www.metal-archives.com/bands/Exodus/411"
BONDED = "https://www.metal-archives.com/albums/Exodus/Bonded_by_Blood/1000"


class FakeMetalArchives:
    """Answers like the site for two bands called Exodus; only the US one made our album."""

    def __init__(self):
        self.calls = []

    def search_albums(self, band_name, title):
        self.calls.append(("albums", band_name, title))
        if band_name == "Nobody Known":
            return []
        return [
            {"band_id": 411, "band_name": "Exodus", "band_url": EXODUS_US, "album_id": 1000,
             "album_url": BONDED, "title": "Bonded by Blood", "type": "Full-length"},
            {"band_id": 411, "band_name": "Exodus", "band_url": EXODUS_US, "album_id": 1001,
             "album_url": BONDED.replace("1000", "1001"), "title": "Bonded by Blood", "type": "Live album"},
        ]

    def search_bands(self, name):
        self.calls.append(("bands", name))
        if name == "Nobody Known":
            return []
        return [{"id": 777, "name": "Exodus", "url": EXODUS_PL, "genre": "Death Metal", "country": "Poland"},
                {"id": 411, "name": "Exodus", "url": EXODUS_US, "genre": "Thrash Metal", "country": "United States"}]

    def page(self, url):
        self.calls.append(("page", url))
        return BAND_HTML if "/bands/" in url else ALBUM_HTML

    def cover(self, image_url, referer):
        self.calls.append(("cover", image_url))
        return PNG


def record(**overrides):
    base = {"artist": "Exodus", "album": "Bonded By Blood (Remastered)", "title": "Bonded by Blood",
            "tracknumber": "1", "file_path": "/music/1.mp3"}
    base.update(overrides)
    return base


class ParsingTests(TestCase):
    def test_band_page_fields(self):
        info = ma.parse_band_page(BAND_HTML)
        self.assertEqual(info["country"], "United States")
        self.assertEqual((info["formed_year"], info["band_status"], info["label"]), (1979, "Active", "Nuclear Blast"))

    def test_album_page_fields(self):
        info = ma.parse_album_page(ALBUM_HTML)
        self.assertEqual((info["release_type"], info["release_year"], info["label"]), ("Full-length", 1985, "Torrid Records"))
        self.assertTrue(info["cover_url"].endswith("/123.jpg?1"))

    def test_unsigned_label_is_not_a_label(self):
        self.assertEqual(ma.parse_band_page("<dt>Current label:</dt><dd>Unsigned/independent</dd>")["label"], "")

    def test_genres_lose_period_notes_and_repeats(self):
        self.assertEqual(ma.split_genres("Thrash Metal (early); Hard Rock (mid); Thrash Metal"), ["Thrash Metal", "Hard Rock"])

    def test_search_rows_become_dicts(self):
        rows = [['<a href="https://www.metal-archives.com/bands/Metallica/125" title="Metallica (US)">Metallica</a>',
                 '<a href="https://www.metal-archives.com/albums/Metallica/Master_of_Puppets/547">Master of Puppets</a> <!-- 10.6 -->',
                 "Full-length"]]
        row = ma.parse_album_rows(rows)[0]
        self.assertEqual((row["band_id"], row["album_id"], row["title"], row["type"]), (125, 547, "Master of Puppets", "Full-length"))
        self.assertEqual(ma.parse_album_rows([["no link", "no link", "x"]]), [])


class LookupTests(TestCase):
    def setUp(self):
        import_records([record(), record(artist="Nobody Known", album="Lost", file_path="/music/2.mp3")])
        self.fake = FakeMetalArchives()

    def test_lookup_stores_details_and_fills_gaps(self):
        stats = ma.run(client=self.fake)
        self.assertEqual((stats.artists_matched, stats.artists_not_found, stats.albums_matched, stats.covers_added), (1, 1, 1, 1))

        artist = Artist.objects.get(artist_name="Exodus")
        entry = artist.metal_archives
        self.assertEqual((entry.ma_id, entry.country, entry.formed_year), (411, "United States", 1979))
        # tags > MusicBrainz > Metal Archives: nothing else knew, so Metal Archives fills the gaps.
        self.assertEqual((artist.resolved_country, artist.resolved_formed_year), ("United States", 1979))
        self.assertEqual(artist.resolved_sources["country"], "metal-archives")

        album = Album.objects.get(album_name__startswith="Bonded")
        self.assertEqual((album.resolved_year, album.resolved_label), (1985, "Torrid Records"))
        self.assertEqual(album.resolved_genre, "Thrash Metal, Groove Metal")
        self.assertEqual(album.resolved_sources["genre"], "artist:metal-archives")
        self.assertEqual(album.album_cover, PNG)
        self.assertEqual(album.metal_archives.release_type, "Full-length")  # the live album is not preferred

    def test_unknown_band_is_recorded_as_not_found(self):
        ma.run(client=self.fake)
        entry = MetalArchivesArtist.objects.get(artist__artist_name="Nobody Known")
        self.assertEqual(entry.status, EnrichmentStatus.NOT_FOUND)
        self.assertIn("No Metal Archives band", entry.note)

    def test_the_band_with_the_artists_album_wins_over_a_namesake(self):
        artist = Artist.objects.get(artist_name="Exodus")
        band, _ = ma.match_band(self.fake, artist, list(artist.album_set.all()))
        self.assertEqual(band["id"], 411)

    def test_namesakes_without_album_evidence_are_not_guessed(self):
        class NoAlbums(FakeMetalArchives):
            def search_albums(self, band_name, title):
                return []
        artist = Artist.objects.get(artist_name="Exodus")
        band, note = ma.match_band(NoAlbums(), artist, list(artist.album_set.all()))
        self.assertIsNone(band)
        self.assertIn("2 Metal Archives bands", note)

    def test_existing_cover_and_tags_are_never_replaced(self):
        Album.objects.filter(album_name__startswith="Bonded").update(album_cover=b"mine", album_release_year=1999)
        ma.run(client=self.fake)
        album = Album.objects.get(album_name__startswith="Bonded")
        self.assertEqual(bytes(album.album_cover), b"mine")
        self.assertEqual((album.resolved_year, album.resolved_sources["year"]), (1999, "tags"))
        self.assertFalse(any(call[0] == "cover" for call in self.fake.calls))

    def test_second_run_skips_finished_artists_unless_refreshed(self):
        ma.run(client=self.fake)
        self.fake.calls.clear()
        ma.run(client=self.fake)
        self.assertEqual(self.fake.calls, [])
        ma.run(client=self.fake, refresh=True)
        self.assertTrue(self.fake.calls)

    def test_one_album_scope_only_looks_that_album_up(self):
        import_records([record(album="Pleasures of the Flesh", file_path="/music/3.mp3")])
        target = Album.objects.get(album_name__startswith="Bonded")
        ma.run(client=self.fake, album_ids=[target.pk])
        self.assertEqual(MetalArchivesAlbum.objects.count(), 1)

    def test_a_failing_request_marks_the_artist_and_goes_on(self):
        class Blocked(FakeMetalArchives):
            def search_albums(self, band_name, title):
                raise ma.MetalArchivesError("Metal Archives answered 403")
        stats = ma.run(client=Blocked())
        self.assertEqual(len(stats.errors), 2)
        self.assertEqual(MetalArchivesArtist.objects.filter(status=EnrichmentStatus.ERROR).count(), 2)


class CoverArtTests(TestCase):
    def setUp(self):
        import_records([record()])
        self.album = Album.objects.get()

    def test_cover_is_added_when_missing(self):
        class Client:
            def front_cover(self, mbid):
                return PNG
        self.assertTrue(enrichment.fill_cover(Client(), self.album, "mbid"))
        self.assertEqual(bytes(Album.objects.get().album_cover), PNG)

    def test_existing_cover_is_kept_and_not_even_requested(self):
        Album.objects.update(album_cover=b"mine")
        class Client:
            def front_cover(self, mbid):
                raise AssertionError("must not be asked")
        self.assertFalse(enrichment.fill_cover(Client(), self.album, "mbid"))
        self.assertEqual(bytes(Album.objects.get().album_cover), b"mine")

    def test_archive_failure_or_no_cover_does_not_raise(self):
        class Down:
            def front_cover(self, mbid):
                raise OSError("offline")
        class Empty:
            def front_cover(self, mbid):
                return None
        self.assertFalse(enrichment.fill_cover(Down(), self.album, "mbid"))
        self.assertFalse(enrichment.fill_cover(Empty(), self.album, "mbid"))
        self.assertFalse(enrichment.fill_cover(object(), self.album, "mbid"))  # client without covers
