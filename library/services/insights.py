"""Reads liner notes, biographies and lyrics with a local language model (Ollama) and turns
what they say into browsable tags.

From the notes: a short summary, the release date, descriptive keywords (usually the
adjectives a press sheet is full of), moods, and the band's history - members joining or
leaving, splits, reunions, hiatuses and why they happened (parenthood, drugs, emigration,
war...). From the lyrics: themes and moods.

Small local models sometimes invent things, so answers are checked against the text:
keywords must occur in it, and every history event needs a quote from it. Results are
stored on AlbumInsight and as facet links with source "notes" / "lyrics"; band history
goes to the artist, merged over all of the artist's albums.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from difflib import SequenceMatcher
from time import perf_counter

from django.db import transaction
from django.utils import timezone

from library.models import (
    Album, AlbumFacet, AlbumInsight, ArtistFacet, EnrichmentStatus, FacetKind, Track,
)
from library.services import album_notes, lyrics
from library.services.enrichment import link_facets
from library.services.ollama import OllamaClient, OllamaError, settings

PROMPT_VERSION = "1"
NOTES_SOURCE, LYRICS_SOURCE = "notes", "lyrics"
MAX_KEYWORDS, MAX_MOODS, MAX_THEMES = 15, 8, 12

EVENT_TYPES = {
    "formed": "Band formed",
    "member_joined": "Member joined",
    "member_left": "Member left",
    "member_died": "Member died",
    "split_up": "Split up",
    "hiatus": "Hiatus",
    "reunion": "Reunion",
    "label_change": "Label change",
    "other": "Other milestone",
}
CAUSES = {
    "none_given": "",
    "parenthood": "Parenthood",
    "drugs_or_alcohol": "Drugs or alcohol",
    "health": "Health",
    "death": "Death",
    "emigration": "Emigration or relocation",
    "war_or_politics": "War or politics",
    "creative_differences": "Creative differences",
    "personal_conflict": "Personal conflict",
    "money": "Money",
    "label_or_legal": "Label or legal trouble",
    "other_projects": "Other projects",
    "other": "Other reason",
}
HIATUS_BUCKETS = [(2, "under 2 years"), (5, "2–5 years"), (10, "5–10 years"), (None, "10 years or more")]

NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_YEAR = {"type": ["integer", "null"]}
STRING_LIST = {"type": "array", "items": {"type": "string"}}

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "release_date": NULLABLE_STRING,
        "keywords": STRING_LIST,
        "moods": STRING_LIST,
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(EVENT_TYPES)},
                    "who": NULLABLE_STRING,
                    "year": NULLABLE_YEAR,
                    "end_year": NULLABLE_YEAR,
                    "cause": {"type": "string", "enum": list(CAUSES)},
                    "reason": NULLABLE_STRING,
                    "evidence": {"type": "string"},
                },
                "required": ["type", "who", "year", "end_year", "cause", "reason", "evidence"],
            },
        },
    },
    "required": ["summary", "release_date", "keywords", "moods", "events"],
}
LYRICS_SCHEMA = {
    "type": "object",
    "properties": {"themes": STRING_LIST, "keywords": STRING_LIST, "moods": STRING_LIST},
    "required": ["themes", "keywords", "moods"],
}

NOTES_PROMPT = f"""You read the liner notes, press sheet or band biography that came with a music album \
and extract facts as JSON. Use only what the text states; never add outside knowledge.

- summary: two or three plain sentences on what the text says about the album and the band.
- release_date: the album's release date as YYYY-MM-DD, YYYY-MM or YYYY if the text gives it, else null.
- keywords: up to {MAX_KEYWORDS} words or short phrases the text uses to describe the music or the album \
("crushing", "melancholic", "raw production", "epic"). Copy them as written, in lowercase. \
No names of people, places, labels or songs.
- moods: up to {MAX_MOODS} feelings or atmospheres the text links the music to ("anger", "nostalgia", "hope"), lowercase.
- events: the band's history as the text tells it, one item per event: the band forming, a member \
joining, leaving or dying, the band splitting up, taking a hiatus or reuniting, a label change. \
"who" is the person (null for the whole band); "year" when it happened and, for a hiatus or a split, \
"end_year" when it ended (null when not stated). "cause" is the reason category when the text gives one \
(parenthood, drugs or alcohol, health, death, emigration, war or politics, creative differences, \
personal conflict, money, label or legal trouble, other projects), otherwise "none_given"; \
"reason" says it in a few words. "evidence" is a short exact quote (at most 25 words) from the text \
that states the event. Leave the list empty when the text says nothing about the band's history."""

LYRICS_PROMPT = f"""You read the lyrics of the songs on one music album and describe them as JSON. \
Use only the lyrics given.

- themes: up to {MAX_THEMES} subjects the songs are about ("war", "loss", "religion", "freedom"), lowercase.
- keywords: up to {MAX_THEMES} words or short phrases that stand out in the lyrics, lowercase, as written.
- moods: up to {MAX_MOODS} feelings the lyrics express ("anger", "despair", "defiance"), lowercase."""


# -- small helpers --------------------------------------------------------------

def _plain(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def quote_in_text(quote, text):
    """True when the quote (allowing small differences) is in the text."""
    quote, text = _plain(quote).strip("\"'“”‘’ "), _plain(text)
    if len(quote) < 8:
        return False
    if quote in text:
        return True
    match = SequenceMatcher(None, quote, text, autojunk=False).find_longest_match(0, len(quote), 0, len(text))
    return match.size >= 0.7 * len(quote)


def _clean_terms(values, limit, text=None, max_words=4, max_length=40):
    """Lowercase, trimmed, unique terms; with ``text``, only those that occur in it."""
    seen, terms = set(), []
    plain_text = _plain(text) if text is not None else None
    for value in values or []:
        if not isinstance(value, str):
            continue
        term = re.sub(r"\s+", " ", value).strip(" .,;:!?\"'“”").casefold()
        if not term or len(term) > max_length or len(term.split()) > max_words or term in seen:
            continue
        if plain_text is not None and not re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", plain_text):
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= datetime.now().year + 1 else None


def clean_events(events, text):
    cleaned, seen = [], set()
    for event in events or []:
        if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
            continue
        evidence = str(event.get("evidence") or "").strip()
        if not quote_in_text(evidence, text):
            continue  # no proof in the text: likely invented
        who = re.sub(r"\s+", " ", str(event.get("who") or "")).strip()[:100] or None
        year, end_year = _year(event.get("year")), _year(event.get("end_year"))
        if year and end_year and end_year < year:
            end_year = None
        cause = event.get("cause") if event.get("cause") in CAUSES else "none_given"
        key = (event["type"], (who or "").casefold(), year)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "type": event["type"],
            "who": who,
            "year": year,
            "end_year": end_year,
            "cause": cause,
            "reason": str(event.get("reason") or "").strip()[:200] or None,
            "evidence": evidence[:300],
        })
    return cleaned


def hiatus_bucket(years):
    for limit, label in HIATUS_BUCKETS:
        if limit is None or years < limit:
            return label
    return ""


def chunks(text, size):
    """Paragraph-aligned pieces of at most ``size`` characters (a long paragraph is cut)."""
    pieces, current = [], ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:size])
            paragraph = paragraph[size:]
        if len(current) + len(paragraph) + 2 > size and current:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        pieces.append(current)
    return pieces


# -- extraction -----------------------------------------------------------------

def known_names(album):
    """Names the model should not offer as keywords: band, album, tracks, members."""
    names = {album.artist.artist_name, album.album_name}
    names.update(Track.objects.filter(album=album).values_list("track_title", flat=True))
    profile = getattr(album.artist, "profile", None)
    if profile is not None:
        names.update(member["name"] for member in profile.lineup)
    return {name.casefold() for name in names if name}


def extract_from_notes(client, notes_text, chunk_chars, exclude=()):
    result = {"summary": "", "release_date": "", "keywords": [], "moods": [], "events": []}
    for piece in chunks(notes_text, chunk_chars):
        answer = client.extract(NOTES_PROMPT, piece, NOTES_SCHEMA)
        if not result["summary"]:
            result["summary"] = str(answer.get("summary") or "").strip()[:1500]
        release = str(answer.get("release_date") or "")
        if not result["release_date"] and re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", release) and release[:4] in piece:
            result["release_date"] = release
        result["keywords"] += answer.get("keywords") or []
        result["moods"] += answer.get("moods") or []
        result["events"] += answer.get("events") or []
    result["events"] = clean_events(result["events"], notes_text)
    exclude = set(exclude) | {(event["who"] or "").casefold() for event in result["events"]}
    result["keywords"] = [
        term for term in _clean_terms(result["keywords"], MAX_KEYWORDS * 2, text=notes_text) if term not in exclude
    ][:MAX_KEYWORDS]
    result["moods"] = _clean_terms(result["moods"], MAX_MOODS, max_words=3, max_length=30)
    return result


def extract_from_lyrics(client, lyrics_text, chunk_chars):
    themes, keywords, moods = [], [], []
    for piece in chunks(lyrics_text, chunk_chars):
        answer = client.extract(LYRICS_PROMPT, piece, LYRICS_SCHEMA)
        themes += answer.get("themes") or []
        keywords += answer.get("keywords") or []
        moods += answer.get("moods") or []
    return {
        "themes": _clean_terms(themes + _clean_terms(keywords, MAX_THEMES, text=lyrics_text), MAX_THEMES),
        "moods": _clean_terms(moods, MAX_MOODS, max_words=3, max_length=30),
    }


# -- the run --------------------------------------------------------------------

@dataclass
class InsightStats:
    albums_total: int = 0
    albums_done: int = 0
    albums_with_notes: int = 0
    tracks_with_lyrics: int = 0
    lyrics_found_online: int = 0
    albums_extracted: int = 0
    llm_duration_seconds: float = 0
    extraction_skipped: str = ""
    errors: list = field(default_factory=list)
    current: str = ""
    model_running: bool = False


def notes_text_of(documents):
    return "\n\n".join(f"=== {document.file_name} ===\n{document.text}" for document in documents)


def lyrics_text_of(track_lyrics):
    return "\n\n".join(
        f"=== {item.track.track_title} ===\n{item.text}" for item in track_lyrics if item.text
    )


def process_album(album, *, client=None, lyrics_client=None, refresh=False, stats=None):
    """Read the album's notes and lyrics; with ``client`` (OllamaClient) also extract facts."""
    stats = stats or InsightStats()
    tracks = list(Track.objects.filter(album=album).select_related("album__artist").order_by("track_discnumber", "track_number"))
    documents = album_notes.load_album_documents(album, [track.track_file_path for track in tracks])
    track_lyrics = []
    for track in tracks:
        item = lyrics.load_track_lyrics(track, client=lyrics_client, refresh=refresh)
        if item.text:
            track_lyrics.append(item)
            stats.lyrics_found_online += item.source == "lrclib"
    stats.albums_with_notes += bool(documents)
    stats.tracks_with_lyrics += len(track_lyrics)

    insight, _ = AlbumInsight.objects.get_or_create(album=album)
    file_release = next((document.release_date for document in documents if document.release_date), "")
    notes_text, lyrics_text = notes_text_of(documents), lyrics_text_of(track_lyrics)
    if not notes_text and not lyrics_text:
        _save_empty(insight, "No liner notes or lyrics found")
        return insight
    if client is None:
        if file_release and not insight.release_date:
            insight.release_date = file_release
            insight.save(update_fields=["release_date"])
        return insight

    input_hash = hashlib.sha256("\x00".join([PROMPT_VERSION, client.model, notes_text, lyrics_text]).encode()).hexdigest()
    if not refresh and insight.status == EnrichmentStatus.MATCHED and insight.input_hash == input_hash:
        return insight

    chunk_chars = settings()["chunk_chars"]
    model_started = perf_counter()
    stats.model_running = True
    try:
        from_notes = extract_from_notes(client, notes_text, chunk_chars, known_names(album)) if notes_text else {}
        from_lyrics = extract_from_lyrics(client, lyrics_text, chunk_chars) if lyrics_text else {}
    finally:
        stats.model_running = False
        duration = Decimal(str(round(perf_counter() - model_started, 3)))
        insight.llm_duration_seconds = duration
        insight.save(update_fields=["llm_duration_seconds"])
        stats.llm_duration_seconds += float(duration)

    with transaction.atomic():
        insight.status = EnrichmentStatus.MATCHED
        insight.note = ""
        insight.model_name = client.model
        insight.input_hash = input_hash
        insight.summary = from_notes.get("summary", "")
        insight.release_date = file_release or from_notes.get("release_date", "")
        insight.keywords = from_notes.get("keywords", [])
        insight.moods = _clean_terms(from_notes.get("moods", []) + from_lyrics.get("moods", []), MAX_MOODS)
        insight.lyric_themes = from_lyrics.get("themes", [])
        insight.events = from_notes.get("events", [])
        insight.extracted_at = timezone.now()
        insight.save()

        AlbumFacet.objects.filter(album=album, source__in=[NOTES_SOURCE, LYRICS_SOURCE]).delete()
        link_facets(AlbumFacet, "album", album, [(FacetKind.KEYWORD, k, "", "", 0) for k in insight.keywords]
                    + [(FacetKind.MOOD, m, "", "", 0) for m in from_notes.get("moods", [])], source=NOTES_SOURCE)
        link_facets(AlbumFacet, "album", album, [(FacetKind.LYRIC_THEME, t, "", "", 0) for t in insight.lyric_themes]
                    + [(FacetKind.MOOD, m, "", "", 0) for m in from_lyrics.get("moods", [])], source=LYRICS_SOURCE)
        rebuild_artist_history(album.artist)
    stats.albums_extracted += 1
    return insight


def _save_empty(insight, note):
    insight.status = EnrichmentStatus.NOT_FOUND
    insight.note = note
    insight.summary, insight.keywords, insight.moods, insight.lyric_themes, insight.events = "", [], [], [], []
    insight.input_hash = ""
    insight.save()
    AlbumFacet.objects.filter(album=insight.album, source__in=[NOTES_SOURCE, LYRICS_SOURCE]).delete()
    rebuild_artist_history(insight.album.artist)


def artist_history(artist):
    """The artist's history events from all of its albums' notes, oldest first, without repeats."""
    merged = {}
    insights = AlbumInsight.objects.filter(album__artist=artist, status=EnrichmentStatus.MATCHED).select_related("album")
    for insight in insights:
        for event in insight.events:
            key = (event["type"], (event.get("who") or "").casefold(), event.get("year"))
            entry = merged.setdefault(key, {**event, "albums": []})
            if insight.album.album_name not in entry["albums"]:
                entry["albums"].append(insight.album.album_name)
            for name in ("end_year", "reason"):
                entry[name] = entry.get(name) or event.get(name)
            if entry.get("cause") == "none_given":
                entry["cause"] = event.get("cause", "none_given")
    events = sorted(merged.values(), key=lambda e: (e.get("year") or 9999, list(EVENT_TYPES).index(e["type"])))
    for event in events:
        event["label"] = EVENT_TYPES[event["type"]]
        event["cause_label"] = CAUSES.get(event.get("cause"), "")
        event["years"] = (event["end_year"] - event["year"]) if event.get("year") and event.get("end_year") else None
    return events


def rebuild_artist_history(artist):
    ArtistFacet.objects.filter(artist=artist, source=NOTES_SOURCE).delete()
    links = []
    for event in artist_history(artist):
        when = f"{event['year']}–{event['end_year']}" if event.get("end_year") else str(event.get("year") or "")
        detail = " · ".join(part for part in (event.get("who") or "", when, event["cause_label"]) if part)
        links.append((FacetKind.BAND_EVENT, event["label"], "", detail, 0))
        if event["cause_label"]:
            links.append((FacetKind.CAUSE, event["cause_label"], "", f"{event['label']}: {detail}", 0))
        if event["type"] in ("hiatus", "split_up") and event["years"] is not None:
            links.append((FacetKind.HIATUS, hiatus_bucket(event["years"]), "", detail, event["years"]))
    link_facets(ArtistFacet, "artist", artist, links, source=NOTES_SOURCE)


def run(*, refresh=False, album_ids=None, artist_ids=None, extract=True, online_lyrics=True,
        client=None, lyrics_client=None, progress=None, should_stop=None):
    """Process albums (all, or the given ones); returns InsightStats."""
    albums = Album.objects.select_related("artist").order_by("artist__artist_name", "album_name")
    if album_ids is not None:
        albums = albums.filter(pk__in=album_ids)
    if artist_ids is not None:
        albums = albums.filter(artist_id__in=artist_ids)
    albums = list(albums)
    stats = InsightStats(albums_total=len(albums))

    if extract and client is None:
        client = OllamaClient()
        state = client.status()
        if not state["reachable"]:
            stats.extraction_skipped = state["error"]
            client = None
        elif not state["model_ready"]:
            stats.extraction_skipped = f'Ollama has no model "{client.model}". Run: ollama pull {client.model}'
            client = None
    elif not extract:
        client = None
    if online_lyrics and lyrics_client is None:
        lyrics_client = lyrics.LrclibClient()
    elif not online_lyrics:
        lyrics_client = None

    for album in albums:
        if should_stop and should_stop():
            break
        stats.current = f"{album.artist.artist_name} – {album.album_name}"
        if progress:
            progress(stats)
        try:
            process_album(album, client=client, lyrics_client=lyrics_client, refresh=refresh, stats=stats)
        except Exception as error:  # one album's problem must not end the run
            message = str(error) if isinstance(error, (OllamaError, lyrics.LyricsError)) else f"{type(error).__name__}: {error}"
            stats.errors.append(f"{stats.current}: {message}")
            AlbumInsight.objects.filter(album=album).update(status=EnrichmentStatus.ERROR, note=message[:300])
        stats.albums_done += 1
    stats.current = ""
    if progress:
        progress(stats)
    return stats
