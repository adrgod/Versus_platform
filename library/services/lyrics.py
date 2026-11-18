"""Lyrics for each track: from the MP3's own lyrics tag, a lyrics file beside it, or LRCLIB.

LRCLIB (https://lrclib.net) is a free lyrics database with no API key. It is only asked
for tracks that have no lyrics of their own, and only when online lookups are enabled.
"""
import re
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError

from library import repository
from library.models import EnrichmentStatus, TrackLyrics
from library.services.http_cache import CachedJsonClient, ServiceError

LRC_TIMESTAMP = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
LRC_METADATA = re.compile(r"^\s*\[(?:ar|al|ti|au|by|length|offset|re|ve|#):[^\]]*\]\s*$", re.I)
DURATION_TOLERANCE = 3  # seconds


class LyricsError(ServiceError):
    pass


class LrclibClient(CachedJsonClient):
    base_url = "https://lrclib.net/api/"
    service_name = "LRCLIB"
    min_interval = 0.3
    cache_name = "lrclib_cache"
    error_class = LyricsError

    def find(self, artist, title, album="", duration=None):
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = int(round(float(duration)))
        exact = self.get("get", **params)
        if exact:
            return exact
        candidates = self.get("search", artist_name=artist, track_name=title) or []
        if duration:
            candidates = [c for c in candidates if c.get("duration") and abs(c["duration"] - float(duration)) <= DURATION_TOLERANCE]
        return candidates[0] if candidates else None


def strip_lrc(text):
    """Plain lyrics from LRC (timestamps and [ar:...] header lines removed)."""
    lines = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if LRC_METADATA.match(line):
            continue
        lines.append(LRC_TIMESTAMP.sub("", line).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def embedded_lyrics(file_path):
    try:
        frames = ID3(file_path).getall("USLT")
    except (ID3NoHeaderError, OSError):
        return ""
    texts = [str(frame.text).strip() for frame in frames if str(frame.text).strip()]
    return max(texts, key=len) if texts else ""


def lyrics_file(file_path):
    """(text, synced) from "<track name>.lrc" or "<track name>.txt" next to the MP3."""
    path = Path(file_path)
    for suffix in (".lrc", ".txt"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            raw = candidate.read_bytes()
            for encoding in ("utf-8-sig", "cp1252", "latin-1"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            synced = bool(LRC_TIMESTAMP.search(text))
            return (strip_lrc(text) if synced or suffix == ".lrc" else text.strip()), synced
    return "", False


def load_track_lyrics(track, *, client=None, refresh=False):
    """Find and store the track's lyrics. ``client`` (an LrclibClient) enables the online lookup."""
    current = TrackLyrics.objects.filter(track=track).first()
    if current and not refresh and current.status == EnrichmentStatus.MATCHED:
        return current
    if current and not refresh and current.status == EnrichmentStatus.NOT_FOUND and (client is None or current.source == "lrclib"):
        return current

    lyrics = current or TrackLyrics(track=track)
    lyrics.text, lyrics.source, lyrics.synced, lyrics.instrumental, lyrics.note = "", "", False, False, ""
    path = track.track_file_path
    if path and repository.is_allowed_file(path):
        text = embedded_lyrics(path)
        if text:
            lyrics.text, lyrics.source = text, "tag"
        else:
            text, synced = lyrics_file(path)
            if text:
                lyrics.text, lyrics.source, lyrics.synced = text, "file", synced

    if not lyrics.text and client is not None:
        album = track.album
        found = client.find(album.artist.artist_name, track.track_title, album.album_name, track.track_duration)
        lyrics.source = "lrclib"
        if found and found.get("instrumental"):
            lyrics.instrumental = True
            lyrics.note = "Instrumental"
        elif found:
            plain = (found.get("plainLyrics") or "").strip()
            lyrics.text = plain or strip_lrc(found.get("syncedLyrics") or "")
            lyrics.synced = bool(found.get("syncedLyrics"))

    lyrics.status = EnrichmentStatus.MATCHED if (lyrics.text or lyrics.instrumental) else EnrichmentStatus.NOT_FOUND
    lyrics.save()
    return lyrics
