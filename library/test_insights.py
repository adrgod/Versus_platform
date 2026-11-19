from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from mutagen.id3 import ID3, USLT

from .models import (
    Album, AlbumDocument, AlbumFacet, AlbumInsight, Artist, ArtistFacet, EnrichmentStatus, Track, TrackLyrics,
)
from .services import album_notes, explorer, insights, lyrics
from .services.importer import import_records

NOTES = """Iron Roots - Rust and Bone
Release date: 29 March 1982

Rust and Bone is a crushing, melancholic record with a raw production.
Guitarist Tom Hale left the band in 1979 to raise his daughter.
After drummer Jo Kent's struggle with heroin the band went on hiatus from 1983 to 1991.
"""


def minimal_pdf(text):
    """A one-page PDF whose page shows ``text`` (enough for pypdf to extract it)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


class FakeOllama:
    model = "fake-model"

    def __init__(self):
        self.calls = []

    def extract(self, system, text, schema):
        self.calls.append(text)
        if "lyrics of the songs" in system:
            return {"themes": ["Loss", "the sea"], "keywords": ["drowning", "never written"], "moods": ["despair"]}
        return {
            "summary": "A heavy record about loss.",
            "release_date": "1982-03-29",
            "keywords": ["Crushing", "melancholic", "raw production", "Tom Hale", "made-up word"],
            "moods": ["grief", "anger"],
            "events": [
                {"type": "member_left", "who": "Tom Hale", "year": 1979, "end_year": None, "cause": "parenthood",
                 "reason": "to raise his daughter", "evidence": "Guitarist Tom Hale left the band in 1979 to raise his daughter."},
                {"type": "hiatus", "who": None, "year": 1983, "end_year": 1991, "cause": "drugs_or_alcohol",
                 "reason": "heroin", "evidence": "the band went on hiatus from 1983 to 1991"},
                {"type": "split_up", "who": None, "year": 1995, "end_year": None, "cause": "money",
                 "reason": "invented", "evidence": "The band split in 1995 over money."},
            ],
        }


class FakeLrclib:
    def __init__(self):
        self.calls = []

    def find(self, artist, title, album="", duration=None):
        self.calls.append(title)
        return {"plainLyrics": "Down below the waves we sink\nDrowning in the dark", "syncedLyrics": "[00:01.00]Down"}


class InsightTestCase(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "Iron Roots" / "Rust and Bone"
        (self.folder / "CD1").mkdir(parents=True)
        self.track1 = self.folder / "CD1" / "01 - Tide.mp3"
        self.track2 = self.folder / "CD1" / "02 - Undertow.mp3"
        self.track3 = self.folder / "CD1" / "03 - Ashes.mp3"
        for path in (self.track1, self.track2, self.track3):
            path.write_bytes(b"\xff\xfb" + b"0" * 64)
        tags = ID3()
        tags.add(USLT(encoding=3, lang="eng", desc="", text="Ashes fall on the shore"))
        tags.save(self.track3)
        (self.folder / "CD1" / "01 - Tide.lrc").write_text("[ar:Iron Roots]\n[00:12.10]The tide comes in\n[00:15.00]and takes us", encoding="utf-8")
        (self.folder / "notes.txt").write_text(NOTES, encoding="utf-8")
        (self.folder / "bio.pdf").write_bytes(minimal_pdf("Iron Roots formed in Leeds in 1977."))
        (self.folder / "release.nfo").write_bytes("Scene info \xb0 cp437".encode("cp437"))

        roots = patch("library.repository.get_allowed_roots", return_value=[self.root])
        roots.start()
        self.addCleanup(roots.stop)
        import_records([
            {"artist": "Iron Roots", "album": "Rust and Bone", "title": title, "tracknumber": str(number),
             "file_path": str(path), "length": "200"}
            for number, (title, path) in enumerate([("Tide", self.track1), ("Undertow", self.track2), ("Ashes", self.track3)], 1)
        ])
        self.album = Album.objects.get()


class NotesAndLyricsTests(InsightTestCase):
    def test_reads_txt_pdf_and_nfo_from_album_folder_above_disc_folder(self):
        documents = album_notes.load_album_documents(self.album)
        by_type = {document.file_type: document for document in documents}
        self.assertEqual(set(by_type), {"txt", "pdf", "nfo"})
        self.assertIn("formed in Leeds", by_type["pdf"].text)
        self.assertIn("°", by_type["nfo"].text)
        self.assertEqual(by_type["txt"].release_date, "1982-03-29")
        # The .lrc lyrics file is not taken for liner notes.
        self.assertFalse(any(document.file_path.endswith(".lrc") for document in documents))

    def test_unchanged_files_are_not_read_again_and_removed_ones_are_dropped(self):
        album_notes.load_album_documents(self.album)
        with patch("library.services.album_notes.read_text") as read_text:
            album_notes.load_album_documents(self.album)
        read_text.assert_not_called()
        (self.folder / "release.nfo").unlink()
        album_notes.load_album_documents(self.album)
        self.assertEqual(AlbumDocument.objects.count(), 2)

    def test_release_date_forms(self):
        self.assertEqual(album_notes.find_release_date("Released: March 29, 1982"), "1982-03-29")
        self.assertEqual(album_notes.find_release_date("Release date: 29/03/1982"), "1982-03-29")
        self.assertEqual(album_notes.find_release_date("Out on Sept 1999"), "1999-09")
        self.assertEqual(album_notes.find_release_date("released in 1975 on vinyl"), "")
        self.assertEqual(album_notes.find_release_date("Release date: 1975"), "1975")

    def test_lyrics_from_tag_file_and_lrclib(self):
        lrclib = FakeLrclib()
        found = {t.track_title: lyrics.load_track_lyrics(t, client=lrclib) for t in Track.objects.all()}
        self.assertEqual((found["Ashes"].source, found["Ashes"].text), ("tag", "Ashes fall on the shore"))
        self.assertEqual((found["Tide"].source, found["Tide"].text, found["Tide"].synced), ("file", "The tide comes in\nand takes us", True))
        self.assertEqual(found["Undertow"].source, "lrclib")
        self.assertEqual(lrclib.calls, ["Undertow"])  # only the track without lyrics of its own

    def test_without_online_lookup_missing_lyrics_are_not_found(self):
        undertow = Track.objects.get(track_title="Undertow")
        self.assertEqual(lyrics.load_track_lyrics(undertow).status, EnrichmentStatus.NOT_FOUND)
        # Enabling LRCLIB later looks it up after all.
        self.assertEqual(lyrics.load_track_lyrics(undertow, client=FakeLrclib()).source, "lrclib")


class ExtractionTests(InsightTestCase):
    def run_extraction(self, client=None):
        return insights.run(client=client or FakeOllama(), lyrics_client=FakeLrclib())

    def test_facts_are_checked_against_the_text(self):
        stats = self.run_extraction()
        self.assertEqual(stats.albums_extracted, 1)
        insight = AlbumInsight.objects.get()
        self.assertEqual(insight.status, EnrichmentStatus.MATCHED)
        self.assertEqual(insight.release_date, "1982-03-29")
        # Names and words that are not in the notes are dropped.
        self.assertEqual(insight.keywords, ["crushing", "melancholic", "raw production"])
        # The split has no quote in the text, so it is treated as invented.
        self.assertEqual([event["type"] for event in insight.events], ["member_left", "hiatus"])
        self.assertEqual(insight.lyric_themes, ["loss", "the sea", "drowning"])
        self.assertEqual(insight.moods, ["grief", "anger", "despair"])

    def test_tags_for_album_and_artist(self):
        self.run_extraction()
        album_tags = set(AlbumFacet.objects.values_list("facet__kind", "facet__name"))
        self.assertTrue({("keyword", "crushing"), ("mood", "grief"), ("mood", "despair"), ("lyric_theme", "loss")} <= album_tags)
        artist_tags = {(link.facet.kind, link.facet.name, link.detail) for link in ArtistFacet.objects.select_related("facet")}
        self.assertIn(("band_event", "Member left", "Tom Hale · 1979 · Parenthood"), artist_tags)
        self.assertIn(("cause", "Drugs or alcohol", "Hiatus: 1983–1991 · Drugs or alcohol"), artist_tags)
        self.assertIn(("hiatus", "5–10 years", "1983–1991 · Drugs or alcohol"), artist_tags)

        result = explorer.explore(explorer.parse_selection(["hiatus:5–10 years", "mood:despair"]))
        self.assertEqual(result.album_ids, {self.album.pk})

    def test_unchanged_text_is_not_sent_to_the_model_again(self):
        self.run_extraction()
        client = FakeOllama()
        self.run_extraction(client)
        self.assertEqual(client.calls, [])
        (self.folder / "notes.txt").write_text(NOTES + "\nA new paragraph.", encoding="utf-8")
        self.run_extraction(client)
        self.assertEqual(len(client.calls), 2)  # notes and lyrics again

    def test_unreachable_ollama_still_reads_notes_and_lyrics(self):
        with patch("library.services.insights.OllamaClient") as ollama:
            ollama.return_value.status.return_value = {"reachable": False, "model_ready": False, "models": [], "error": "Could not reach Ollama"}
            ollama.return_value.model = "qwen3:8b"
            stats = insights.run(lyrics_client=FakeLrclib())
        self.assertEqual(stats.extraction_skipped, "Could not reach Ollama")
        self.assertEqual(AlbumDocument.objects.count(), 3)
        self.assertEqual(TrackLyrics.objects.filter(status=EnrichmentStatus.MATCHED).count(), 3)
        self.assertEqual(AlbumInsight.objects.get().status, EnrichmentStatus.PENDING)

    def test_a_failing_model_is_recorded_per_album(self):
        class Broken(FakeOllama):
            def extract(self, system, text, schema):
                raise insights.OllamaError("model crashed")

        with patch("library.services.insights.perf_counter", side_effect=[1.0, 3.5]):
            stats = self.run_extraction(Broken())
        self.assertIn("model crashed", stats.errors[0])
        self.assertEqual(AlbumInsight.objects.get().status, EnrichmentStatus.ERROR)
        self.assertEqual(AlbumInsight.objects.get().llm_duration_seconds, Decimal("2.5"))
        self.assertAlmostEqual(stats.llm_duration_seconds, 2.5)
        self.assertFalse(stats.model_running)

    def test_successful_model_runtime_is_saved_per_album_and_run(self):
        with patch("library.services.insights.perf_counter", side_effect=[1.0, 2.234]):
            stats = self.run_extraction()
        self.assertEqual(AlbumInsight.objects.get().llm_duration_seconds, Decimal("1.234"))
        self.assertAlmostEqual(stats.llm_duration_seconds, 1.234)
        self.assertFalse(stats.model_running)

    def test_long_text_is_split_into_paragraph_chunks(self):
        text = "\n\n".join(["a" * 50] * 5)
        pieces = insights.chunks(text, 120)
        self.assertEqual(len(pieces), 3)
        self.assertTrue(all(len(piece) <= 120 for piece in pieces))
        self.assertEqual(insights.chunks("b" * 250, 100), ["b" * 100, "b" * 100, "b" * 50])

    def test_pages_show_notes_lyrics_and_history(self):
        self.run_extraction()
        user = get_user_model().objects.create_user(username="notes", password="not-a-real-password")
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)
        album_page = client.get(reverse("library:album-detail", args=(self.album.pk,)))
        self.assertContains(album_page, "A heavy record about loss.")
        self.assertContains(album_page, "raw production")
        self.assertContains(album_page, "Drowning in the dark")
        self.assertContains(album_page, "formed in Leeds")
        artist_page = client.get(reverse("library:artist-detail", args=(Artist.objects.get().pk,)))
        self.assertContains(artist_page, "Member left: Tom Hale")
        self.assertContains(artist_page, "(8 years)")
        with patch("library.views.OllamaClient") as ollama:
            ollama.return_value.status.return_value = {"reachable": False, "model_ready": False, "models": [], "error": "down"}
            ollama.return_value.url, ollama.return_value.model = "http://other-mac:11434", "qwen3:8b"
            lookup_page = client.get(reverse("library:enrichment"))
        self.assertContains(lookup_page, "Not reachable")
        self.assertContains(lookup_page, "http://other-mac:11434")

    def test_lookup_page_lists_enriched_and_failed_albums(self):
        self.run_extraction()
        failed_album = Album.objects.create(artist=self.album.artist, album_name="Failed album")
        AlbumInsight.objects.create(album=failed_album, status=EnrichmentStatus.ERROR, note="model crashed")

        user = get_user_model().objects.create_user(username="lookup", password="not-a-real-password")
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)
        response = client.get(reverse("library:enrichment"))

        self.assertContains(response, "Iron Roots — Rust and Bone")
        self.assertContains(response, "Iron Roots — Failed album")
        self.assertContains(response, "model crashed")
        self.assertContains(response, "s LLM time")

    def test_running_model_is_animated_on_lookup_page(self):
        user = get_user_model().objects.create_user(username="running-model", password="not-a-real-password")
        client = Client(HTTP_HOST="localhost")
        client.force_login(user)
        job = SimpleNamespace(
            kind="notes",
            running=True,
            stats=SimpleNamespace(model_running=True, current="Iron Roots — Rust and Bone"),
        )
        with (
            patch("library.views.enrichment_service.get_job", side_effect=lambda kind: job if kind == "notes" else None),
            patch("library.views.enrichment_service.running_job", return_value=job),
            patch("library.views.OllamaClient") as ollama,
        ):
            ollama.return_value.status.return_value = {"reachable": True, "model_ready": True, "models": ["model"]}
            response = client.get(reverse("library:enrichment"))

        self.assertContains(response, "Model running")
        self.assertContains(response, "status-dot status-scanning")
