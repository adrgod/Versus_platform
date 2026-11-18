#!/usr/bin/env python3
"""
The report for tracker.py's logs.

    python report.py --serve     live page at http://localhost:8765 that updates itself
    python report.py --check     test genre lookups, see what lands in "Other", today's breaks
    python report.py --check --artist "Metallica"   test the genre lookup for one artist
    python report.py             one-off report.html file (no server needed)
    python report.py --days 30   only the last 30 days of logs (any mode)

Things you can change are in the SETTINGS section just below.
"""
import csv
import datetime as dt
import functools
import json
import re
import sys
import threading
import time
import webbrowser
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
OUT = HERE / "report.html"

# =========================================================================== SETTINGS
IDLE_AFTER = 300  # no keyboard/mouse for this many seconds => idle
IDLE_EXEMPT = {"Meetings"}  # count as working even with no input (calls)

# Lunch = the longest break that STARTS inside this window and lasts between min and max minutes.
LUNCH_START_WINDOW = ("11:30", "14:30")
LUNCH_MIN_MINUTES = 15
LUNCH_MAX_MINUTES = 120
BLIP_MINUTES = 2  # a short bit of activity inside a break (checking something) doesn't end it

# Topics: tag work by name patterns. Checked against the project (git repo / VS Code folder),
# the window title and the website. First match wins; untagged work uses the project name.
# Example: "Acme renewal": r"acme|renewal",
TOPICS = {
}

# Genres come from MusicBrainz (musicbrainz.org, free, no account): each artist's name is
# looked up once and saved in logs/genres.json. Set to False to turn this off.
LOOKUP_GENRES = True
GENRE_FILE = LOG_DIR / "genres.json"
# MusicBrainz asks every app to say who runs it, so they can get in touch if it misbehaves.
# Put your email (or a web page of yours) here. It's only sent to MusicBrainz, in each request.
MUSICBRAINZ_CONTACT = ""

# Mood from an artist's genres. Each genre picks the first mood whose pattern matches,
# and the artist's top genres vote (the first genre counts most).
MOODS = [
    ("Focus", r"ambient|classical|piano|instrumental|lo-?fi|chill|downtempo|post-rock|minimal|drone"
              r"|soundtrack|score|new age|jazz|bossa|neo-?classical|idm|study|meditation|sleep"),
    ("Emotive", r"soul|r&b|rnb|blues|folk|singer-songwriter|indie|ballad|emo\b|trip hop|dream pop"
                r"|shoegaze|acoustic|country|gospel|fado|chanson|mpb|bolero|samba|sad"),
    ("Energetic", r"rock|punk|metal|hardcore|dance|house|techno|trance|edm|electro|drum and bass"
                  r"|dubstep|hip ?hop|rap|trap|funk|disco|reggaeton|ska|grunge|big beat|garage|pop"),
]

# --------------------------------------------------------------------------- categories
# (category, app regex, text regex). First match wins. None = match anything. Case-insensitive.
# App is the process name ("Google Chrome", "Code"). Text is the window title, the website, and for
# terminals the program running in the active tab (e.g. "cortex", "claude").
BROWSERS = r"chrome|msedge|edge|firefox|safari|brave|^arc|opera|vivaldi"
TERMINALS = (r"terminal|iterm|warp|windowsterminal|^cmd|powershell|pwsh|alacritty"
             r"|kitty|wezterm|ghostty|konsole|tilix|^wt\b")

RULES = [
    ("Cortex Code",       TERMINALS,  r"\bcortex\b|\bcoco\b"),
    ("Cortex Code",       r"^cortex code|^coco\b", None),
    ("Cortex Code",       BROWSERS,   r"cortex code|\bcoco\b"),
    ("Claude Code",       TERMINALS,  r"claude|✳"),
    ("Claude chat",       r"^claude", None),
    ("Claude chat",       BROWSERS,   r"\bclaude\b|claude\.ai"),
    ("AI (other)",        r"chatgpt|gemini|copilot|perplexity", None),
    ("AI (other)",        BROWSERS,   r"chatgpt|chat\.openai|gemini\.google|perplexity"),
    ("Meetings",          r"zoom|teams|webex|facetime|around|tuple", None),
    ("Meetings",          r"slack",   r"huddle"),
    ("Meetings",          BROWSERS,   r"google meet|^meet [-–]|meet\.google|zoom\.us|teams\.microsoft"),
    ("Documents",         BROWSERS,   r"docs\.google|google docs|google sheets|google slides|quip|sharepoint"
                                      r"|onedrive|confluence|notion\.so|box\.com|dropbox|office\.com"),
    ("Learning",          BROWSERS,   r"trailhead|youtube|udemy|coursera|tutorial|course"
                                      r"|documentation|docs\.|developer\.|stack ?overflow|mdn|wikipedia|learn"),
    ("Salesforce",        BROWSERS,   r"salesforce|lightning|force\.com"),
    ("Snowflake",         BROWSERS,   r"snowflake|snowsight"),
    ("VS Code",           r"^code(\.exe)?$|visual studio code", None),
    ("Coding (other)",    r"pycharm|idea|webstorm|cursor|sublime|xcode|vim|zed|" + TERMINALS, None),
    ("Coding (other)",    BROWSERS,   r"github|gitlab|bitbucket|localhost|127\.0\.0\.1"),
    ("Documents",         r"word|excel|powerpoint|pages|numbers|keynote|preview|acrobat|quip|libreoffice"
                          r"|notes|notion|obsidian|evernote|bear|onenote|craft", None),
    ("Design & diagrams", r"figma|miro|lucid|canva|sketch|whimsical|excalidraw", None),
    ("Design & diagrams", BROWSERS,   r"figma|miro|lucid|canva|whimsical|excalidraw"),
    ("Calendar & tasks",  r"calendar|reminders|things|todoist|fantastical|asana|jira|linear|trello|omnifocus", None),
    ("Calendar & tasks",  BROWSERS,   r"calendar\.google|jira|atlassian|asana|linear\.app|trello|monday\.com"),
    ("Spotify",           r"spotify", None),
    ("Chat & email",      r"slack|outlook|mail|thunderbird|discord|whatsapp|telegram|messages|signal", None),
    ("Chat & email",      BROWSERS,   r"gmail|outlook|inbox|slack|mail\."),
    ("Files & system",    r"finder|system settings|system preferences|activity monitor|app store|installer"
                          r"|1password|keychain|raycast|alfred|spotlight|dock|archive|disk utility"
                          r"|explorer\.exe|taskmgr|notificationcenter|usernotification", None),
    ("Browser (other)",   BROWSERS,   None),
]
RULES = [(n, re.compile(a, re.I) if a else None, re.compile(t, re.I) if t else None)
         for n, a, t in RULES]
COLORS = {
    "Cortex Code": "#29b5e8", "Snowflake": "#1971c2", "Claude Code": "#3b5bdb", "Claude chat": "#7c8ff0", "AI (other)": "#9775fa",
    "VS Code": "#1c7ed6", "Coding (other)": "#0b7285", "Salesforce": "#2f9e44",
    "Learning": "#e8a317", "Documents": "#5f3dc4", "Design & diagrams": "#f06595",
    "Calendar & tasks": "#1098ad", "Meetings": "#c2255c", "Chat & email": "#9c36b5",
    "Spotify": "#37b24d", "Files & system": "#b08968", "Browser (other)": "#868e96",
    "Other": "#adb5bd",
}
# The big filter buttons on the page. Categories not listed here appear under "More".
GROUPS = {
    "Coding": ["Cortex Code", "Claude Code", "VS Code", "Coding (other)"],
    "Browsing": ["Salesforce", "Snowflake", "Browser (other)"],
    "AI assistants": ["Claude chat", "AI (other)"],
    "Docs & planning": ["Documents", "Design & diagrams", "Calendar & tasks"],
    "Reading & learning": ["Learning"],
    "Meetings & chat": ["Meetings", "Chat & email"],
    "Other apps": ["Spotify", "Files & system", "Other"],
}
# ======================================================================= end of settings

TOPIC_RULES = [(name, re.compile(rx, re.I)) for name, rx in TOPICS.items()]
MOOD_RULES = [(name, re.compile(rx, re.I)) for name, rx in MOODS]
SEP = "\x1f"  # separator inside combined keys (titles can contain "|")
CODE_EDITORS = re.compile(r"^code(\.exe)?$|visual studio code|cursor", re.I)
EDITOR_SUFFIX = re.compile(r"^(visual studio code|cursor|code)$", re.I)


def categorize(app, text):
    for name, app_re, text_re in RULES:
        if app_re and not app_re.search(app):
            continue
        if text_re and not text_re.search(text):
            continue
        return name
    return "Other"


def work_context(app, title, site, project):
    """(topic, detail): what you were working on, and the file/task/page within it."""
    detail = title
    if CODE_EDITORS.search(app) and title:
        # VS Code titles look like "● report.py — TaskTracker — Visual Studio Code"
        parts = [p.strip() for p in re.split(r" [—–-] ", title) if p.strip()]
        if parts and EDITOR_SUFFIX.match(parts[-1]):
            parts = parts[:-1]
        parts = [re.sub(r"^[●•]\s*", "", p) for p in parts]
        if len(parts) >= 2:
            detail, project = parts[0], project or re.sub(r"\s*\((workspace|wsl.*)\)$", "", parts[1], flags=re.I)
        elif parts:
            project = project or parts[0]
            detail = ""
    elif title.startswith("✳"):
        detail = title.lstrip("✳ ").strip()  # Claude Code puts a summary of the task in the title
    text = f"{project} {title} {site}"
    topic = next((name for name, rx in TOPIC_RULES if rx.search(text)), project)
    return topic, detail[:90]


def artist_of(track):
    return track.split(" - ")[0].split(",")[0].strip()


def mood_of(genres):
    votes = Counter()
    for i, g in enumerate(genres[:5]):
        for mood, rx in MOOD_RULES:
            if rx.search(g):
                votes[mood] += 5 - i
                break
    return votes.most_common(1)[0][0] if votes else "Unknown"


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# --------------------------------------------------------------------------- loading
# Same order as tracker.py. Older logs have fewer columns; missing ones read as empty.
COLUMNS = ["timestamp", "seconds", "locked", "idle_s", "app", "title",
           "music", "site", "keys", "clicks", "project", "proc"]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for raw in reader:
            r = dict(zip(COLUMNS, raw + [""] * (len(COLUMNS) - len(raw))))
            try:
                ts = dt.datetime.fromisoformat(r["timestamp"])
            except ValueError:
                continue
            music = r["music"]
            rows.append({
                "ts": ts, "sec": num(r["seconds"]) or 5,
                "locked": r["locked"] == "1", "idle": num(r["idle_s"]),
                "app": r["app"], "title": r["title"], "site": r["site"], "project": r["project"], "proc": r["proc"],
                "music": music not in ("", "0"),
                "track": music if music not in ("", "0", "1") else "",
                "keys": int(num(r["keys"])), "clicks": int(num(r["clicks"])),
            })
    rows.sort(key=lambda r: r["ts"])

    for r in rows:
        r["cat"] = None if r["locked"] else categorize(r["app"], f'{r["title"]} {r["site"]} {r["proc"]}')
        if r["locked"]:
            r["state"] = "locked"
        elif r["idle"] >= IDLE_AFTER and r["cat"] not in IDLE_EXEMPT:
            r["state"] = "idle"
        else:
            r["state"] = "active"
        r["topic"], r["detail"] = ("", "") if r["locked"] else work_context(r["app"], r["title"], r["site"], r["project"])

    # The minutes just before an idle stretch had no input either: mark them idle too.
    for i, r in enumerate(rows):
        if r["state"] == "idle" and (i == 0 or rows[i - 1]["state"] != "idle"):
            since = r["ts"] - dt.timedelta(seconds=r["idle"])
            j = i - 1
            while (j >= 0 and rows[j]["ts"] >= since and rows[j]["state"] == "active"
                   and rows[j]["cat"] not in IDLE_EXEMPT):
                rows[j]["state"] = "idle"
                j -= 1
    return rows


@functools.lru_cache(maxsize=45)
def _rows(path_str, stamp):
    return load(Path(path_str))


def rows_of(path):
    st = path.stat()
    return _rows(str(path), (st.st_mtime_ns, st.st_size))


# --------------------------------------------------------------------------- summaries
def summarize(day, rows):
    """Per-day data at hour and minute resolution, so the page can filter and slice it."""
    hours = defaultdict(lambda: [0.0] * 24)
    idle, locked, music, keys, clicks = ([0.0] * 24 for _ in range(5))
    tracks, sites, unsorted, projects, pfiles = Counter(), Counter(), Counter(), Counter(), Counter()
    mins = defaultdict(Counter)              # label -> minute of day -> seconds
    mkeys, mclicks, mmusic = Counter(), Counter(), Counter()
    segments, msegs = [], []
    for r in rows:
        hr, s = r["ts"].hour, r["sec"]
        mi = hr * 60 + r["ts"].minute
        a = mi + r["ts"].second / 60
        b = a + s / 60
        mkeys[mi] += r["keys"]
        mclicks[mi] += r["clicks"]
        keys[hr] += r["keys"]
        clicks[hr] += r["clicks"]
        if r["music"]:
            mmusic[mi] += s
            music[hr] += s
        if r["track"]:
            tracks[r["track"]] += s
            if msegs and msegs[-1][2] == r["track"] and a - msegs[-1][1] < 0.5:
                msegs[-1][1] = b
            else:
                msegs.append([a, b, r["track"]])
        if r["state"] == "active":
            mins[r["cat"]][mi] += s
            hours[r["cat"]][hr] += s
            if r["site"]:
                sites[f'{r["site"]}|{r["cat"]}'] += s
            if r["cat"] in ("Other", "Browser (other)"):
                unsorted[f'{r["cat"]}|{r["app"]}  ·  {r["title"][:90]}'] += s
            if r["topic"]:
                projects[r["topic"] + SEP + r["cat"]] += s
                if r["detail"]:
                    pfiles[r["topic"] + SEP + r["cat"] + SEP + r["detail"]] += s
        elif r["state"] == "idle":
            mins["idle"][mi] += s
            idle[hr] += s
        else:
            locked[hr] += s

        label = r["cat"] if r["state"] == "active" else r["state"]
        if segments and segments[-1][2] == label and a - segments[-1][1] < 0.5:
            segments[-1][1] = b
        else:
            segments.append([a, b, label])

    ints = lambda xs: [int(x) for x in xs]
    top = lambda c, n=100: {k: int(v) for k, v in c.most_common(n)}
    sparse = lambda c: [[m, int(v)] for m, v in sorted(c.items()) if v]
    rnd = lambda segs: [[round(a, 1), round(b, 1), c] for a, b, c in segs]
    return {
        "day": day,
        "hours": {c: ints(v) for c, v in hours.items()},
        "idle": ints(idle), "locked": ints(locked), "music": ints(music),
        "keys": ints(keys), "clicks": ints(clicks),
        "segments": rnd(s for s in segments if s[2] != "locked"),
        "msegs": rnd(msegs),
        "tracks": top(tracks), "sites": top(sites), "unsorted": top(unsorted),
        "projects": top(projects), "pfiles": top(pfiles, 200),
        "min": {label: sparse(c) for label, c in mins.items()},
        "mk": sparse(mkeys), "mc": sparse(mclicks), "mm": sparse(mmusic),
    }


def log_files(days):
    files = [p for p in LOG_DIR.glob("*.csv") if re.fullmatch(r"\d{4}-\d\d-\d\d", p.stem)]
    return sorted(files)[-days:]


_cache, _cache_lock = {}, threading.Lock()


def day_summary(path):
    """Summarize one log file, reusing the last result if the file hasn't changed."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    summary = summarize(path.stem, rows_of(path))
    with _cache_lock:
        _cache[path] = (key, summary)
    return summary


def detail(days, day_from, day_to, m0, m1, cats):
    """Drill-down for a time slot: projects, files/tasks, windows and sites."""
    projects, files, windows, sites = Counter(), Counter(), Counter(), Counter()
    for p in log_files(days):
        if not (day_from <= p.stem <= day_to):
            continue
        for r in rows_of(p):
            mi = r["ts"].hour * 60 + r["ts"].minute
            if r["state"] != "active" or not (m0 <= mi < m1) or (cats and r["cat"] not in cats):
                continue
            s = r["sec"]
            windows[f'{r["app"]}{SEP}{r["title"] or "(no title)"}{SEP}{r["cat"]}'] += s
            if r["topic"]:
                projects[r["topic"]] += s
                if r["detail"]:
                    files[r["topic"] + SEP + r["detail"]] += s
            if r["site"]:
                sites[r["site"]] += s
    pairs = lambda c, n: [[k, int(v)] for k, v in c.most_common(n)]
    return {"projects": pairs(projects, 10), "files": pairs(files, 12),
            "windows": pairs(windows, 12), "sites": pairs(sites, 8)}


# --------------------------------------------------------------------------- genres
try:  # use the operating system's certificates (works with company networks that inspect traffic)
    import truststore
    truststore.inject_into_ssl()
    _TRUSTSTORE = True
except Exception:
    _TRUSTSTORE = False


APP_VERSION = "1.2"


def _user_agent():
    contact = MUSICBRAINZ_CONTACT.strip() or "contact not set"
    return f"TaskTracker/{APP_VERSION} ( {contact} )"


def _get_json(url, log=None):
    """GET a MusicBrainz URL as JSON, waiting and retrying when it says it's busy (HTTP 503)."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    req = Request(url, headers={"User-Agent": _user_agent(), "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urlopen(req, timeout=10) as r:
                body, ctype = r.read(), r.headers.get("Content-Type", "")
                if log:
                    log(f"HTTP {r.status}, {ctype}, {len(body)} bytes")
            break
        except HTTPError as e:
            if log:
                log(f"HTTP {e.code} {e.reason}")
            if e.code != 503 or attempt == 3:
                raise
            wait = float(e.headers.get("Retry-After") or 2 ** (attempt + 1))
            if log:
                log(f"MusicBrainz is busy, waiting {wait:.0f}s and trying again")
            time.sleep(wait)
    if "json" not in ctype:
        raise RuntimeError(f"MusicBrainz sent back a web page instead of data ({ctype}). "
                           "A browser check or a company proxy may be in the way.")
    return json.loads(body)


def lookup_genres(artist, log=None):
    """Top genres for an artist ([] if MusicBrainz has none). Raises if MusicBrainz can't be reached."""
    from urllib.parse import quote, urlencode
    base = "https://musicbrainz.org/ws/2/artist/"
    name = artist.replace("\\", " ").replace('"', " ")
    url = base + "?" + urlencode({"query": f'artist:"{name}"', "fmt": "json", "limit": 3})
    if log:
        log(f"1. Search: {url}")
    found = _get_json(url, log)
    candidates = found.get("artists", [])
    if log:
        for a in candidates:
            log(f"   candidate: {a.get('name')} ({a.get('disambiguation') or a.get('country') or '-'}), score {a.get('score')}")
    match = next((a for a in candidates if int(a.get("score", 0)) >= 90), None)
    if not match:
        if log:
            log("   no candidate with a score of 90 or more, so no genre")
        return []
    time.sleep(1.1)  # MusicBrainz asks for at most one request per second
    url = base + quote(match["id"]) + "?inc=genres+tags&fmt=json"
    if log:
        log(f"2. Lookup: {url}")
    info = _get_json(url, log)
    for field in ("genres", "tags"):
        items = sorted((g for g in info.get(field, []) if g.get("count", 0) > 0), key=lambda g: -g["count"])
        if log:
            log(f"   {field}: " + (", ".join(f"{g['name']} ({g['count']})" for g in items[:8]) or "none"))
        if items:
            return [g["name"].title() for g in items[:5]]
    return []


def explain(error):
    text = f"{type(error).__name__}: {error}"
    if "CERTIFICATE_VERIFY_FAILED" in text:
        text += ("  -> Your network inspects secure connections and Python doesn't trust it. "
                 "Install truststore into the tracker's Python (see requirements.txt) and restart.")
    elif "timed out" in text or "Name or service" in text or "nodename" in text or "Errno 8" in text:
        text += "  -> MusicBrainz can't be reached. Your network or VPN may block it."
    elif "503" in text:
        text += ("  -> MusicBrainz is refusing requests for now. This happens when requests come too fast or "
                 "the app doesn't identify itself: set MUSICBRAINZ_CONTACT in report.py.")
    elif "403" in text or "Forbidden" in text:
        text += "  -> Blocked by your network's proxy, or MusicBrainz refused the request."
    return text


class GenreBook:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            raw = json.loads(GENRE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        # older versions stored one genre as a string
        self.map = {a: ([g] if isinstance(g, str) and g else g if isinstance(g, list) else []) for a, g in raw.items()}
        self.queue, self.error = [], ""

    def want(self, summaries):
        if not LOOKUP_GENRES:
            return
        with self.lock:
            for s in summaries:
                for t in s["tracks"]:
                    a = artist_of(t)
                    if a and a not in self.map and a not in self.queue:
                        self.queue.append(a)

    def music_info(self):
        """{artist: [genre, mood]} for the page, plus lookups still to do and the last error."""
        with self.lock:
            info = {a: [g[0] if g else "", mood_of(g)] for a, g in self.map.items()}
            return info, len(self.queue), self.error

    def _one(self):
        with self.lock:
            if not self.queue:
                return False
            artist = self.queue[0]
        try:
            genres = lookup_genres(artist)
        except Exception as e:
            msg = explain(e)
            if msg != self.error:
                print(time.strftime("%H:%M:%S"), "Genre lookup failed:", msg, flush=True)
            self.error = msg
            return None
        with self.lock:
            self.error = ""
            self.queue.remove(artist)
            self.map[artist] = genres
            tmp = GENRE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.map, ensure_ascii=False, indent=0), encoding="utf-8")
            tmp.replace(GENRE_FILE)
        return True

    def run_forever(self):
        while True:
            result = self._one()
            time.sleep(1.2 if result else (120 if result is None else 10))

    def fill_now(self, limit=60):
        done = 0
        while done < limit and self.queue:
            if self._one() is None:
                print("Couldn't look up genres:", self.error)
                return
            done += 1
            print(f"\rLooking up genres: {done}", end="", flush=True)
            time.sleep(1.1)
        if done:
            print()


GENRES = GenreBook()


def payload(days, since=0.0):
    """Data for the page. With since, only days whose log changed after that time."""
    files = log_files(days)
    summaries = [day_summary(p) for p in files if p.stat().st_mtime >= since - 2]
    GENRES.want(summaries)
    music, pending, error = GENRES.music_info()
    return {
        "days": summaries,
        "all": [p.stem for p in files],
        "now": time.time(),
        "colors": COLORS, "order": list(COLORS), "groups": GROUPS,
        "music": music, "genresPending": pending if LOOKUP_GENRES else 0,
        "genreError": error, "genresOff": not LOOKUP_GENRES,
        "settings": {"lunchFrom": _minutes(LUNCH_START_WINDOW[0]), "lunchTo": _minutes(LUNCH_START_WINDOW[1]),
                     "lunchMin": LUNCH_MIN_MINUTES, "lunchMax": LUNCH_MAX_MINUTES, "blip": BLIP_MINUTES,
                     "idleAfter": IDLE_AFTER},
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- check
def check(days):
    print("Python:", sys.executable)
    print("truststore (company certificates):", "yes" if _TRUSTSTORE else "NOT installed")
    files = log_files(days)
    if not files:
        print("No logs found in", LOG_DIR)
        return
    week = files[-7:]
    rows = [r for p in week for r in rows_of(p)]
    unlocked = [r for r in rows if not r["locked"]]
    pct = lambda f: f"{100 * sum(1 for r in unlocked if f(r)) / max(1, len(unlocked)):.0f}%"
    print(f"\nLast 7 days of logs: {len(week)} files, last entry {rows[-1]['ts'] if rows else '-'}")
    print("  unlocked samples with a window title:", pct(lambda r: r["title"]))
    print("  with a website:", pct(lambda r: r["site"]), "  with a project:", pct(lambda r: r["project"] or r["topic"]),
          "  with a Spotify track:", pct(lambda r: r["track"]))
    if unlocked and not any(r["title"] for r in unlocked):
        print("  -> No window titles: give the tracker's Python Accessibility permission (see earlier steps).")

    other = Counter()
    for r in rows:
        if r["state"] == "active" and r["cat"] in ("Other", "Browser (other)"):
            other[(r["cat"], r["app"] or "(unknown app)")] += r["sec"]
    print("\nApps that land in Other / Browser (other), last 7 days (app names only):")
    for (cat, app), s in other.most_common(20):
        print(f"  {s / 3600:6.1f} h   {cat:16} {app}")
    if not other:
        print("  none")

    today = rows_of(files[-1])
    active = sorted({r["ts"].hour * 60 + r["ts"].minute for r in today if r["state"] == "active"})
    print(f"\nBreaks of 10+ minutes on {files[-1].stem} (any activity counts as working):")
    for a, b in zip(active, active[1:]):
        if b - a >= 10:
            print(f"  {a // 60:02d}:{a % 60:02d} - {b // 60:02d}:{b % 60:02d}  ({b - a} min)")

    artist = arg("--artist", "Metallica")
    print(f"\nGenre lookup test for {artist!r}:")
    print("  identifies itself as:", _user_agent())
    if not MUSICBRAINZ_CONTACT.strip():
        print("  -> MUSICBRAINZ_CONTACT is empty. Put your email in it (top of report.py) so MusicBrainz accepts the requests.")
    if not LOOKUP_GENRES:
        print("  turned off in settings (LOOKUP_GENRES = False)")
    else:
        try:
            genres = lookup_genres(artist, log=lambda m: print("  " + m))
            print("  RESULT:", ", ".join(genres) or "(no genres listed)", "->", mood_of(genres))
        except Exception as e:
            print("  FAILED:", explain(e))
    music, pending, _ = GENRES.music_info()
    print(f"  saved artists: {len(music)}, with a genre: {sum(1 for g, _ in music.values() if g)}")


# --------------------------------------------------------------------------- server
def serve(days, port, open_browser):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    page = TEMPLATE.replace("__DATA__", "null").encode("utf-8")
    if LOOKUP_GENRES:
        threading.Thread(target=GENRES.run_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    body, ctype = page, "text/html; charset=utf-8"
                elif url.path == "/api/data":
                    body = json.dumps(payload(days, float(q.get("since", 0))), separators=(",", ":")).encode()
                    ctype = "application/json"
                elif url.path == "/api/detail":
                    cats = set(json.loads(q.get("cats", "[]")))
                    result = detail(days, q["from"], q["to"], int(q["m0"]), int(q["m1"]), cats)
                    body, ctype = json.dumps(result).encode(), "application/json"
                else:
                    self.send_error(404)
                    return
            except (KeyError, ValueError) as e:
                self.send_error(400, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    # 127.0.0.1 only: the page is reachable from this computer, not from the network.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Live report at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    days = arg("--days", 365)
    if "--check" in sys.argv:
        check(days)
        return
    if "--serve" in sys.argv:
        serve(days, arg("--port", 8765), "--no-open" not in sys.argv)
        return
    if not log_files(days):
        sys.exit(f"No logs in {LOG_DIR}. Start tracker.py first.")
    data = payload(days)
    if LOOKUP_GENRES and GENRES.queue:
        GENRES.fill_now()
        data["music"], data["genresPending"], data["genreError"] = GENRES.music_info()
    OUT.write_text(TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":"))),
                   encoding="utf-8")
    print(f"Wrote {OUT}")
    webbrowser.open(OUT.as_uri())


# --------------------------------------------------------------------------- page
TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Where the hours went</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&display=swap">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--paper:#f3f5f7;--panel:#ffffff;--ink:#18202b;--muted:#5f6b7a;--rule:#d6dce3;--track:#e4e8ed;--idle:#c3cad3;--accent:#3b5bdb;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#1b2027;--panel:#232932;--ink:#e6eaef;--muted:#9aa5b3;--rule:#363f4b;--track:#2a313b;--idle:#4f5967;--accent:#7c95f5;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 "Instrument Sans",system-ui,-apple-system,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}
main.wrap{padding-bottom:64px}
h2{font-size:1.1rem;font-weight:600;margin:44px 0 10px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
h2 .aside{font-weight:400;color:var(--muted);font-size:.9rem}
button{font:inherit;color:inherit;cursor:pointer}
button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
[hidden]{display:none!important}

/* ---- sticky header */
.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--paper) 86%,transparent);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--rule)}
.bar{display:flex;align-items:center;gap:10px 14px;flex-wrap:wrap;padding-top:12px;padding-bottom:12px}
.brand{margin-right:auto;min-width:0}
.brand h1{font-size:1.15rem;font-weight:600;letter-spacing:-.01em;margin:0;line-height:1.2}
.status{display:block;font-size:12px;color:var(--muted)}
.status.live::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#2f9e44;margin-right:6px;vertical-align:1px}
.status.offline::before{background:#c2255c}
.rangebtn{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--rule);background:var(--panel);border-radius:999px;padding:6px 12px 6px 11px;font-weight:500}
.rangebtn:hover{border-color:var(--muted)}
.rangebtn svg{flex:none}
.rangebtn .chev{transition:transform .2s ease}
.rangebtn[aria-expanded=true] .chev{transform:rotate(180deg)}
.pills{display:inline-flex;background:var(--track);border-radius:999px;padding:3px}
.pills button{border:0;background:transparent;border-radius:999px;padding:4px 11px;font-size:13px;color:var(--muted)}
.pills button:hover{color:var(--ink)}
.pills button[aria-pressed=true]{background:var(--panel);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.12)}
.solo{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:5px 6px 5px 12px;font-size:13px;color:#fff;background:var(--c,var(--accent))}
.solo button{border:0;background:rgba(255,255,255,.22);color:#fff;border-radius:999px;padding:2px 9px;font-size:12px}

/* ---- calendar drawer */
.drawer{display:grid;grid-template-rows:0fr;transition:grid-template-rows .25s ease}
.drawer.open{grid-template-rows:1fr}
.drawer>div{overflow:hidden}
.drawer .inner{display:grid;grid-template-columns:1fr 240px;gap:24px;align-items:start;padding-top:16px;padding-bottom:18px}
.months{display:flex;flex-wrap:wrap;gap:22px}
.month{width:224px}
.mname{font-weight:500;margin-bottom:6px}
.mgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.wd{font-size:11px;color:var(--muted);text-align:center}
.cell{aspect-ratio:1;border:0;border-radius:4px;font-size:11px;padding:0;color:var(--ink);
  background:color-mix(in srgb,var(--accent) calc(var(--v,0)*85%),var(--track))}
.cell.hot{color:#fff}
.cell.empty{background:transparent;color:var(--muted);box-shadow:inset 0 0 0 1px var(--rule)}
.cell.in{box-shadow:0 0 0 2px var(--ink)}
.cell.edge{box-shadow:0 0 0 2px var(--ink),0 0 0 4px var(--paper),0 0 0 5px var(--ink)}
.range label{display:block;font-size:13px;color:var(--muted);margin-bottom:10px}
.range input{display:block;width:100%;margin-top:3px;padding:7px 9px;border:1px solid var(--rule);border-radius:6px;background:var(--panel);color:var(--ink);font:inherit}
.hint{font-size:12px;color:var(--muted);margin:4px 0 0}

/* ---- what to show */
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:10px;padding:16px 18px;margin-top:22px}
.whathead{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap}
.mini{border:1px solid var(--rule);background:var(--paper);border-radius:6px;padding:4px 10px;font-size:13px}
.mini:hover{border-color:var(--muted)}
.groups{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.gbtn{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:11px 13px;border-radius:8px;
  border:1px solid var(--rule);background:var(--paper);opacity:.55}
.gbtn.on,.gbtn.part{opacity:1;border-color:var(--ink)}
.gbtn.part{border-style:dashed}
.sw{width:14px;height:28px;border-radius:3px;flex:none}
.sw.hatch{background:repeating-linear-gradient(135deg,var(--idle) 0 3px,transparent 3px 6px);box-shadow:inset 0 0 0 1px var(--idle)}
.gname{font-weight:600;flex:1}
.ghrs{color:var(--muted);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.chip{border:1px solid var(--rule);background:transparent;border-radius:999px;padding:2px 10px 2px 8px;font-size:12px;color:var(--muted)}
.chip::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--c);opacity:.35}
.chip.on{color:var(--ink);border-color:var(--muted)}
.chip.on::before{opacity:1}
.summary{font-size:1.15rem;margin:22px 0 0;max-width:80ch}
.summary b{font-weight:600}

/* ---- charts & lists */
.chart{position:relative;height:340px}
.chart.tall{height:380px}
.note{color:var(--muted);font-size:13px;max-width:80ch;margin:0 0 10px}
.axis,.row{display:grid;grid-template-columns:96px 1fr 150px;gap:14px;align-items:center}
.axis{color:var(--muted);font-size:12px;margin:4px 0 6px}
.ticks{position:relative;height:16px}.ticks span{position:absolute;transform:translateX(-50%)}
.row{padding:5px 0;border-top:1px solid var(--rule)}
.date{font-weight:500}.date small{display:block;color:var(--muted);font-weight:400;font-size:12px}
.strips{display:grid;gap:3px}
.strip{position:relative;height:22px;background:var(--track);border-radius:3px;overflow:hidden}
.strip.music{height:7px;background:transparent}
.strip.presence{height:7px}
.strip.presence i{background:var(--ink);opacity:.75}
.strip.presence i.idle{opacity:1}
.ratio{display:flex;height:5px;border-radius:3px;overflow:hidden;background:var(--track);margin:4px 0 3px auto;width:100%;max-width:120px}
.ratio span{display:block;height:100%}
.ratio .a{background:var(--ink);opacity:.75}
.ratio .i{background:repeating-linear-gradient(135deg,var(--idle) 0 3px,transparent 3px 6px);box-shadow:inset 0 0 0 1px var(--idle)}
.legend span.k-active::before{background:var(--ink);opacity:.75}
.legend span.k-idle::before{background:repeating-linear-gradient(135deg,var(--idle) 0 3px,transparent 3px 6px);box-shadow:inset 0 0 0 1px var(--idle)}
.legend span.k-away::before{background:var(--track)}
.strip i{position:absolute;top:0;bottom:0}
.strip i.idle{background:repeating-linear-gradient(135deg,var(--idle) 0 3px,transparent 3px 6px)}
.strip b.lunch{position:absolute;top:0;bottom:0;border:1.5px dashed var(--ink);border-radius:3px;opacity:.55}
.hours{text-align:right}.hours b{font-size:1.15rem;font-weight:600}.hours small{display:block;color:var(--muted);font-size:12px}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--muted);margin-top:10px}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px;background:var(--c)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--rule);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{font-weight:500;color:var(--muted)}
.list td:first-child{white-space:normal;word-break:break-word}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:0}
.chart.lane{height:150px}
.lanehead{margin:14px 0 0}
.detail{background:var(--panel);border:1px solid var(--rule);border-radius:10px;padding:14px 18px 16px;margin-top:16px}
.dhead{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px}
.dgrid{display:grid;grid-template-columns:1fr 1fr 1.3fr;gap:24px}
.dgrid h4{font-size:13px;font-weight:500;color:var(--muted);margin:0 0 4px}
.dgrid table{font-size:13px}
.dgrid td{padding:5px 6px}
.dgrid td:first-child{white-space:normal;word-break:break-word}
.dgrid .sub{display:block;color:var(--muted);font-size:12px}
.onlybtn{border:1px solid var(--rule);background:transparent;border-radius:999px;padding:0 8px;font-size:11px;color:var(--muted);margin-left:6px}
.onlybtn:hover{color:var(--ink);border-color:var(--muted)}
.strip{cursor:pointer}
.mix{display:flex;height:10px;border-radius:3px;overflow:hidden;background:var(--track);min-width:120px}
.mix span{display:block;height:100%}
.projects td:first-child{position:relative;min-width:180px}
.share{position:absolute;left:0;top:4px;bottom:4px;background:color-mix(in srgb,var(--accent) 14%,transparent);border-radius:3px;z-index:0}
.projects td:first-child span.t{position:relative;z-index:1;font-weight:500}
.quad{display:grid;grid-template-columns:1fr 1fr;gap:24px}
.projects th:nth-child(n+6),.projects td:nth-child(6){text-align:left}
.warn{color:#c2255c}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:28px}
h3{font-size:.95rem;font-weight:500;color:var(--muted);margin:0 0 6px}
.legend span.lunchkey::before{background:transparent;border:1.5px dashed var(--muted);box-sizing:border-box}
.triple{display:grid;grid-template-columns:1fr 1fr 1.5fr;gap:24px}
.empty-state{color:var(--muted);padding:20px 0}
@media (max-width:900px){.triple,.duo,.quad,.dgrid{grid-template-columns:1fr}.drawer .inner{grid-template-columns:1fr}}
@media (max-width:640px){.axis,.row{grid-template-columns:64px 1fr 84px;gap:8px}.month{width:100%}.wrap{padding:0 14px}}
</style></head><body>
<header class="top">
  <div class="wrap bar">
    <div class="brand"><h1>Where the hours went</h1><span class="status" id="status"></span></div>
    <span class="solo" id="solo" hidden></span>
    <button type="button" class="rangebtn" id="rangeBtn" aria-expanded="false" aria-controls="drawer">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="2" y="3" width="12" height="11" rx="2"/><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3"/></svg>
      <span id="rangeText">Loading…</span>
      <svg class="chev" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 4.5l3 3 3-3"/></svg>
    </button>
    <div class="pills" id="presets" role="group" aria-label="Quick ranges"></div>
  </div>
  <div class="drawer" id="drawer"><div>
    <div class="wrap inner">
      <div class="months" id="cal"></div>
      <div class="range">
        <label>From <input type="date" id="from"></label>
        <label>To <input type="date" id="to"></label>
        <p class="hint" id="hint"></p>
      </div>
    </div>
  </div></div>
</header>

<main class="wrap">
<section class="panel" aria-label="What to show">
  <div class="whathead"><strong>What to show</strong>
    <span><button class="mini" id="allOn" type="button">Show everything</button>
    <button class="mini" id="allOff" type="button">Hide all activities</button></span></div>
  <div class="groups" id="groups"></div>
  <div class="groups" id="signals" style="margin-top:12px"></div>
</section>

<p class="summary" id="summary"></p>

<h2>Through the day
  <span class="pills" role="group" aria-label="Detail">
    <button type="button" data-res="60">Hour</button><button type="button" data-res="15">15 min</button><button type="button" data-res="5">5 min</button><button type="button" data-res="1">Minute</button>
  </span>
  <span class="pills" role="group" aria-label="Scale">
    <button type="button" data-mode="avg">Average day</button><button type="button" data-mode="total">All days added up</button>
  </span></h2>
<p class="note">Click anywhere on these charts, or on a day's timeline further down, to see what you were working on and listening to at that time. Click a name in a legend to turn it on or off.</p>
<div class="chart tall"><canvas id="hourly"></canvas></div>
<div id="laneWrap"><h3 class="lanehead">Music, by mood</h3><div class="chart lane"><canvas id="lane"></canvas></div></div>
<section class="detail" id="detail" hidden aria-live="polite"></section>

<h2>Start, finish and lunch</h2>
<p class="note" id="worknote"></p>
<div class="duo">
  <div><h3>When</h3><div class="chart"><canvas id="workday"></canvas></div></div>
  <div><h3>How long</h3><div class="chart"><canvas id="worklen"></canvas></div></div>
</div>

<h2>Day by day <span class="aside" id="presnote"></span></h2>
<div class="axis"><span></span><div class="ticks" id="ticks"></div><span style="text-align:right">active / idle</span></div>
<div id="rows"></div>
<div class="legend" id="tlLegend"></div>

<h2>Hours per day</h2>
<div class="chart"><canvas id="daily"></canvas></div>

<section id="projSec"><h2>Projects and topics <span class="aside" id="projnote"></span></h2>
<div class="scroll"><table class="list projects" id="projects"></table></div></section>

<section id="inputSec"><h2>Keyboard and mouse</h2>
<div class="chart"><canvas id="input"></canvas></div></section>

<section id="musicSec"><h2>Music <span class="aside" id="musicnote"></span></h2>
<p class="note warn" id="genrewarn" hidden></p>
<h3>Music while you work</h3>
<div class="scroll"><table id="musicwork"></table></div>
<div class="quad" style="margin-top:24px">
<div class="scroll"><table class="list" id="moods"></table></div>
<div class="scroll"><table class="list" id="genres"></table></div>
<div class="scroll"><table class="list" id="artists"></table></div>
<div class="scroll"><table class="list" id="tracks"></table></div></div></section>

<section id="sitesSec"><h2>Websites while working</h2>
<div class="scroll"><table class="list" id="sites"></table></div></section>

<h2>Daily totals</h2>
<div class="scroll"><table id="table"></table></div>

<section id="unsortedSec"><h2>Time in Other or Browser (other)</h2>
<p class="note">If something here belongs in a category, add a pattern to RULES in report.py and run it again.</p>
<div class="scroll"><table class="list" id="unsorted"></table></div></section>
</main>
<script>
const EMBED = __DATA__;   // null when served live by report.py --serve
const LIVE = EMBED === null, POLL_SECONDS = 30;
const $ = id => document.getElementById(id);
const el = (tag, props = {}, text) => { const e = document.createElement(tag); Object.assign(e, props); if (text != null) e.textContent = text; return e; };
const dur = m => m > 0 && m < 1 ? "<1m" : (m = Math.round(m)) < 60 ? m + "m" : Math.floor(m / 60) + "h " + pad(m % 60) + "m";
const fmt = h => { const m = Math.round(h * 60); return Math.floor(m / 60) + "h " + String(m % 60).padStart(2, "0") + "m"; };
const num = n => Math.round(n).toLocaleString();
const sum = a => a.reduce((x, y) => x + y, 0);
const pad = n => String(n).padStart(2, "0");
const hhmm = m => { m = Math.round(m); return pad(Math.floor(m / 60) % 24) + ":" + pad(m % 60); };
const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = s => new Date(s + "T12:00");
const shortDay = s => parse(s).toLocaleDateString(undefined, { day: "numeric", month: "short" });
const css = getComputedStyle(document.documentElement);
const INK = css.getPropertyValue("--ink").trim() || "#18202b";
const IDLE_FILL = "rgba(140,150,165,.35)";
const MUSIC = "#37b24d", CLICKS = "#d9480f";

let D, ALL, GROUPS, FIRST, LAST, BYDAY, H0, H1, COLORS = {}, MUSICINFO = {}, SET = {}, lastStamp = 0;
const SEP = "\x1f";

// ---- genres
const artistOf = t => t.split(" - ")[0].split(",")[0].trim();
const genreOf = t => (MUSICINFO[artistOf(t)] || [])[0] || "Unknown";
const moodOf = t => (MUSICINFO[artistOf(t)] || [])[1] || "Unknown";
const MOODS = ["Focus", "Emotive", "Energetic", "Unknown"];
const MOOD_COLOR = { Focus: "#15aabf", Emotive: "#e64980", Energetic: "#fd7e14", Unknown: "#adb5bd" };
const GPAL = ["#e64980", "#7950f2", "#15aabf", "#f59f00", "#40c057", "#fd7e14", "#228be6", "#be4bdb", "#12b886", "#f03e3e", "#82c91e", "#4c6ef5"];
const hash = s => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);
const genreColor = g => g === "Unknown" ? "#adb5bd" : GPAL[hash(g) % GPAL.length];

function index() {
  D.days.sort((a, b) => a.day < b.day ? -1 : 1);
  COLORS = D.colors; MUSICINFO = D.music || {}; SET = D.settings;
  ALL = D.order.filter(c => D.days.some(d => d.hours[c]));
  GROUPS = Object.entries(D.groups).map(([g, cs]) => [g, cs.filter(c => ALL.includes(c))]).filter(([, cs]) => cs.length);
  const grouped = new Set(GROUPS.flatMap(([, cs]) => cs));
  const leftover = ALL.filter(c => !grouped.has(c));
  if (leftover.length) GROUPS.push(["More", leftover]);
  FIRST = D.days[0].day; LAST = D.days[D.days.length - 1].day;
  BYDAY = Object.fromEntries(D.days.map(d => [d.day, d]));
  let lo = 1440, hi = 0;
  D.days.forEach(d => d.segments.forEach(([a, b]) => { lo = Math.min(lo, a); hi = Math.max(hi, b); }));
  if (hi <= lo) { lo = 480; hi = 1080; }
  H0 = Math.floor(lo / 60); H1 = Math.min(23, Math.ceil(hi / 60) - 1);
  for (const id of ["from", "to"]) { $(id).min = FIRST; $(id).max = LAST; }
}

// ---- state (what you hide, chart settings and the calendar being open are remembered here)
const KEY = "activity-report-filters-v3";
const state = { start: "", end: "", hidden: new Set(), sig: { idle: true, music: true, keys: true, clicks: true },
  mode: "avg", res: 60, pending: null, calOpen: false, solo: null, detail: null };
try {
  const s = JSON.parse(localStorage.getItem(KEY) || "null");
  if (s) { state.hidden = new Set(s.hidden || []); Object.assign(state.sig, s.sig || {});
    state.mode = s.mode || "avg"; state.res = s.res || 60; state.calOpen = !!s.calOpen; }
} catch (e) {}
const save = () => { try { localStorage.setItem(KEY, JSON.stringify({ hidden: [...state.hidden], sig: state.sig,
  mode: state.mode, res: state.res, calOpen: state.calOpen })); } catch (e) {} };
const isOn = c => !state.hidden.has(c);
const toggleCat = c => { state.solo = null; state.hidden.has(c) ? state.hidden.delete(c) : state.hidden.add(c); };
function solo(cat) {
  if (state.solo && state.solo.cat === cat) { state.hidden = new Set(state.solo.prev); state.solo = null; }
  else { const prev = state.solo ? state.solo.prev : [...state.hidden]; state.hidden = new Set(ALL.filter(c => c !== cat)); state.solo = { cat, prev }; }
  render();
}

function setRange(a, b) { if (a > b) [a, b] = [b, a]; state.start = a; state.end = b; state.pending = null; render(); }
const PRESETS = [["Today", 1], ["7 days", 7], ["30 days", 30], ["This month", 0], ["All", -1]];
function presetRange(n) {
  const today = iso(new Date());
  if (n === -1) return [FIRST, LAST];
  if (n === 0) return [today.slice(0, 8) + "01", today];
  const s = new Date(); s.setDate(s.getDate() - n + 1); return [iso(s), today];
}

const selDays = () => D.days.filter(d => d.day >= state.start && d.day <= state.end);
const onCats = () => ALL.filter(isOn);
const catSec = (d, c) => sum(d.hours[c] || []);
const workedSec = d => sum(onCats().map(c => catSec(d, c)));

// ---- start, end and lunch from minute data (follows the activity filters)
function workday(d) {
  const act = new Uint8Array(1440);
  onCats().forEach(c => (d.min[c] || []).forEach(([m, s]) => { if (s >= 10) act[m] = 1; }));
  let start = -1, end = -1;
  for (let m = 0; m < 1440; m++) if (act[m]) { if (start < 0) start = m; end = m + 1; }
  if (start < 0) return null;
  const gaps = [];
  for (let m = start; m < end;) {
    if (act[m]) { m++; continue; }
    let g = m; while (g < end && !act[g]) g++;
    const prev = gaps[gaps.length - 1];
    if (prev && m - prev[1] <= SET.blip) prev[1] = g; else gaps.push([m, g]);
    m = g;
  }
  let lunch = null;
  gaps.forEach(([a, b]) => {
    if (b - a < SET.lunchMin || b - a > SET.lunchMax || a < SET.lunchFrom || a > SET.lunchTo) return;
    if (!lunch || b - a > lunch[1] - lunch[0]) lunch = [a, b];
  });
  return { start, end, lunch, worked: workedSec(d) / 3600 };
}

// ---- header and calendar
function renderHeader() {
  const same = state.start === state.end;
  const a = parse(state.start), b = parse(state.end);
  const opt = { day: "numeric", month: "short" };
  $("rangeText").textContent = same ? a.toLocaleDateString(undefined, { weekday: "short", ...opt })
    : `${a.toLocaleDateString(undefined, opt)} – ${b.toLocaleDateString(undefined, opt)}`;
  $("rangeBtn").setAttribute("aria-expanded", state.calOpen);
  $("drawer").classList.toggle("open", state.calOpen);
  $("drawer").inert = !state.calOpen;
  $("presets").querySelectorAll("button").forEach((btn, i) => {
    const [s, e] = presetRange(PRESETS[i][1]);
    btn.setAttribute("aria-pressed", s === state.start && e === state.end);
  });
  const so = $("solo"); so.replaceChildren();
  so.hidden = !state.solo;
  if (state.solo) {
    so.style.setProperty("--c", COLORS[state.solo.cat]);
    const back = el("button", { type: "button" }, "Show all");
    back.onclick = () => solo(state.solo.cat);
    so.append(el("span", {}, `Only ${state.solo.cat}`), back);
  }
}

function renderCalendar() {
  const box = $("cal"); box.replaceChildren();
  const max = Math.max(1, ...D.days.map(workedSec));
  const months = [];
  for (let m = parse(FIRST); iso(m).slice(0, 7) <= LAST.slice(0, 7); m.setMonth(m.getMonth() + 1)) { m.setDate(1); months.push(new Date(m)); }
  for (const m of months.slice(-3)) {
    const wrap = el("div", { className: "month" });
    wrap.append(el("div", { className: "mname" }, m.toLocaleDateString(undefined, { month: "long", year: "numeric" })));
    const grid = el("div", { className: "mgrid" });
    ["M", "T", "W", "T", "F", "S", "S"].forEach(w => grid.append(el("span", { className: "wd" }, w)));
    for (let i = 0; i < (m.getDay() + 6) % 7; i++) grid.append(el("span"));
    const dim = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
    for (let n = 1; n <= dim; n++) {
      const key = `${m.getFullYear()}-${pad(m.getMonth() + 1)}-${pad(n)}`, d = BYDAY[key];
      const v = d ? workedSec(d) / max : 0;
      const b = el("button", { type: "button", className: "cell" }, String(n));
      b.style.setProperty("--v", v.toFixed(3));
      if (!d) b.classList.add("empty");
      if (v > .55) b.classList.add("hot");
      if (key >= state.start && key <= state.end) b.classList.add("in");
      if (key === state.pending) b.classList.add("edge");
      b.title = `${key}: ${d ? fmt(workedSec(d) / 3600) + " worked" : "no data"}`;
      b.setAttribute("aria-label", b.title);
      b.onclick = () => { if (state.pending) setRange(state.pending, key); else { state.start = state.end = state.pending = key; render(); } };
      grid.append(b);
    }
    wrap.append(grid); box.append(wrap);
  }
  $("from").value = state.start; $("to").value = state.end;
  $("hint").textContent = state.pending ? "Now click the last day of the range, or the same day again for just that day."
    : "Click a day, then another, to pick a range. Darker days had more hours worked.";
}

// ---- filter buttons
function groupButton(label, value, color, status, onClick, swClass = "") {
  const b = el("button", { type: "button", className: "gbtn " + status });
  b.setAttribute("aria-pressed", status === "on");
  const sw = el("span", { className: "sw " + swClass });
  if (color) sw.style.background = color;
  b.append(sw, el("span", { className: "gname" }, label), el("span", { className: "ghrs" }, value));
  b.onclick = onClick;
  return b;
}
const SIGNALS = [["idle", "Idle time", ""], ["music", "Listening", MUSIC], ["keys", "Typing", INK], ["clicks", "Clicks", CLICKS]];
function renderControls() {
  const days = selDays(), box = $("groups"); box.replaceChildren();
  for (const [g, cs] of GROUPS) {
    const n = cs.filter(isOn).length, status = n === cs.length ? "on" : n ? "part" : "";
    const hrs = sum(cs.map(c => sum(days.map(d => catSec(d, c))))) / 3600;
    const stripes = cs.map((c, i) => `${COLORS[c]} ${i / cs.length * 100}% ${(i + 1) / cs.length * 100}%`).join(",");
    const block = el("div");
    block.append(groupButton(g, fmt(hrs), `linear-gradient(${stripes})`, status, () => {
      state.solo = null; cs.forEach(c => n === cs.length ? state.hidden.add(c) : state.hidden.delete(c)); render();
    }));
    const chips = el("div", { className: "chips" });
    cs.forEach(c => {
      const chip = el("button", { type: "button", className: "chip" + (isOn(c) ? " on" : "") }, c);
      chip.style.setProperty("--c", COLORS[c]); chip.setAttribute("aria-pressed", isOn(c));
      chip.onclick = () => { toggleCat(c); render(); };
      chips.append(chip);
    });
    block.append(chips); box.append(block);
  }
  const sig = $("signals"); sig.replaceChildren();
  for (const [k, label, color] of SIGNALS) {
    const total = sum(days.map(d => sum(d[k])));
    const value = k === "keys" || k === "clicks" ? num(total) : fmt(total / 3600);
    sig.append(groupButton(label, value, color, state.sig[k] ? "on" : "", () => { state.sig[k] = !state.sig[k]; render(); }, k === "idle" ? "hatch" : ""));
  }
}

function renderSummary() {
  const days = selDays(), worked = sum(days.map(workedSec)) / 3600, s = $("summary"); s.replaceChildren();
  if (!days.length) { s.textContent = "No logs in this date range."; return; }
  const what = state.solo ? state.solo.cat : "the selected activities";
  const bits = [el("b", {}, fmt(worked)), el("span", {}, ` on ${what}`)];
  if (days.length > 1) bits.push(el("span", {}, ` across ${days.length} days, `), el("b", {}, fmt(worked / days.length)), el("span", {}, " a day on average"));
  bits.push(el("span", {}, "."));
  s.append(...bits);
}

// ---- charts
const charts = {};
function chart(id, config) {
  charts[id]?.destroy();
  if (!window.Chart) { $(id).parentElement.textContent = "Charts need an internet connection to load Chart.js."; return; }
  charts[id] = new Chart($(id), config);
}
const RES_NAME = { 60: "hour", 15: "15 minutes", 5: "5 minutes", 1: "minute" };
// Both time-of-day charts use the same axis widths so their time slots line up vertically.
const fitAxis = scale => { scale.width = 62; };
const pointer = (evt, els, ch) => { ch.canvas.style.cursor = "pointer"; };
const timeAxis = (lo, res, labels) => ({ grid: { display: false }, ticks: { autoSkip: false, maxRotation: 0,
  callback: (v, i) => { const m = lo + i * res, every = (H1 - H0) > 10 ? 120 : 60; return m % every === 0 ? labels[i] : null; } } });
// Clicking a time slot opens the details for it (deferred: re-rendering destroys the clicked chart).
function slotClick(evt, _els, ch) {
  const i = Math.round(ch.scales.x.getValueForPixel(evt.x));
  if (!(i >= 0)) return;
  const m0 = H0 * 60 + i * state.res;
  setTimeout(() => openDetail(state.start, state.end, m0, m0 + state.res));
}

function renderLane() {
  $("laneWrap").hidden = !state.sig.music;
  if (!state.sig.music) { charts.lane?.destroy(); delete charts.lane; return; }
  const days = selDays(), n = Math.max(1, days.length), avg = state.mode === "avg", res = state.res;
  const lo = H0 * 60, slots = Math.ceil(((H1 + 1) * 60 - lo) / res), hoursUnit = !avg && res === 60;
  const by = Object.fromEntries(MOODS.map(m => [m, new Array(slots).fill(0)]));
  days.forEach(d => d.msegs.forEach(([a, b, t]) => {
    const arr = by[moodOf(t)];
    for (let i = Math.max(0, Math.floor((a - lo) / res)); i < slots; i++) {
      const s0 = lo + i * res; if (s0 >= b) break;
      arr[i] += Math.max(0, Math.min(b, s0 + res) - Math.max(a, s0));
    }
  }));
  const fine = res < 15, labels = Array.from({ length: slots }, (_, i) => hhmm(lo + i * res));
  const ds = MOODS.map(m => ({ label: m, data: by[m].map(v => v / (avg ? n : 1) / (hoursUnit ? 60 : 1)),
    borderColor: MOOD_COLOR[m], backgroundColor: MOOD_COLOR[m] + "aa", fill: true, stack: "m", pointRadius: 0,
    borderWidth: fine ? 0 : 1, stepped: fine, cubicInterpolationMode: "monotone", hidden: !by[m].some(v => v > 0) }));
  chart("lane", { type: "line", data: { labels, datasets: ds },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      onClick: slotClick, onHover: pointer,
      scales: { x: timeAxis(lo, res, labels),
        y: { stacked: true, beginAtZero: true, max: avg ? res : undefined, afterFit: fitAxis, ticks: { maxTicksLimit: 4 },
             title: { display: true, text: hoursUnit ? "hours" : "minutes" } },
        y1: { position: "right", grid: { display: false }, ticks: { display: false }, afterFit: fitAxis } },
      plugins: { legend: { labels: { boxWidth: 12 } },
        tooltip: { filter: c => c.raw > 0.0005, callbacks: { label: c =>
          `${c.dataset.label}: ${hoursUnit ? fmt(c.raw) : (c.raw < 10 ? c.raw.toFixed(1) : Math.round(c.raw)) + " min"}` } } } } });
}

// ---- drill-down for a time slot
const detailCache = new Map();
function openDetail(from, to, m0, m1) {
  state.detail = { from, to, m0, m1 };
  renderDetail();
  $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function miniTable(head, rows, render) {
  const box = el("div"); box.append(el("h4", {}, head));
  const t = el("table");
  if (!rows.length) { const tr = el("tr"); tr.append(el("td", { className: "sub" }, "nothing here")); t.append(tr); }
  rows.forEach(r => { const tr = el("tr"); render(r).forEach(td => tr.append(td)); t.append(tr); });
  box.append(t); return box;
}
const cell = (...kids) => { const td = el("td"); kids.forEach(k => td.append(k instanceof Node ? k : document.createTextNode(k))); return td; };
const dotOf = color => { const s = el("span", { className: "dot" }); s.style.background = color; return s; };
function renderDetail() {
  const box = $("detail"), q = state.detail;
  box.hidden = !q; if (!q) return;
  box.replaceChildren();
  const days = D.days.filter(d => d.day >= q.from && d.day <= q.to);
  const when = q.from === q.to ? parse(q.from).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })
    : `${days.length} days added up (${shortDay(q.from)} – ${shortDay(q.to)})`;
  const head = el("div", { className: "dhead" });
  const close = el("button", { type: "button", className: "mini" }, "Close");
  close.onclick = () => { state.detail = null; renderDetail(); };
  head.append(el("strong", {}, `${hhmm(q.m0)}–${hhmm(q.m1)}, ${when}`), close);
  box.append(head);

  const act = {};
  days.forEach(d => Object.entries(d.min).forEach(([lab, arr]) => arr.forEach(([m, s]) => {
    if (m >= q.m0 && m < q.m1 && (lab === "idle" ? state.sig.idle : isOn(lab))) act[lab] = (act[lab] || 0) + s;
  })));
  const mus = {};
  days.forEach(d => d.msegs.forEach(([a, b, t]) => { const o = Math.min(b, q.m1) - Math.max(a, q.m0); if (o > 0) mus[t] = (mus[t] || 0) + o; }));

  const grid = el("div", { className: "dgrid" });
  grid.append(miniTable("Doing", sortObj(act), ([lab, s]) => {
    const name = cell(dotOf(lab === "idle" ? IDLE_FILL : COLORS[lab]), lab === "idle" ? "Idle" : lab);
    if (lab !== "idle" && !(state.solo && state.solo.cat === lab)) {
      const b = el("button", { type: "button", className: "onlybtn", title: "Show only this on the whole page" }, "only this");
      b.onclick = () => solo(lab); name.append(b);
    }
    return [name, cell(dur(s / 60))];
  }));
  grid.append(miniTable(state.sig.music ? "Listening" : "Listening (hidden)", state.sig.music ? sortObj(mus) : [], ([t, m]) =>
    [cell(dotOf(MOOD_COLOR[moodOf(t)]), t, el("span", { className: "sub" }, `${moodOf(t)} · ${genreOf(t)}`)), cell(dur(m))]));

  const work = el("div");
  if (!LIVE) work.append(el("h4", {}, "Working on"), el("p", { className: "note" }, "Projects, files and windows show here in the live page (report.py --serve)."));
  else {
    const key = JSON.stringify([q, onCats()]);
    const res = detailCache.get(key);
    if (!res) {
      work.append(el("h4", {}, "Working on"), el("p", { className: "note" }, "Loading…"));
      fetch(`/api/detail?from=${q.from}&to=${q.to}&m0=${q.m0}&m1=${q.m1}&cats=${encodeURIComponent(JSON.stringify(onCats()))}`)
        .then(r => r.json()).then(r => { detailCache.set(key, r); if (state.detail === q) renderDetail(); })
        .catch(() => { work.replaceChildren(el("h4", {}, "Working on"), el("p", { className: "note" }, "Couldn't load details.")); });
    } else {
      work.append(miniTable("Projects and topics", res.projects, ([p, s]) => [cell(p), cell(dur(s / 60))]));
      work.append(miniTable("Files and tasks", res.files, ([k, s]) => { const [p, f] = k.split(SEP); return [cell(f, el("span", { className: "sub" }, p)), cell(dur(s / 60))]; }));
      work.append(miniTable("Windows", res.windows, ([k, s]) => { const [app, title, cat] = k.split(SEP);
        return [cell(dotOf(COLORS[cat] || COLORS.Other), title, el("span", { className: "sub" }, app)), cell(dur(s / 60))]; }));
      if (res.sites.length) work.append(miniTable("Websites", res.sites, ([site, s]) => [cell(site), cell(dur(s / 60))]));
    }
  }
  grid.append(work);
  box.append(grid);
}

function renderHourly() {
  const days = selDays(), n = Math.max(1, days.length), avg = state.mode === "avg", res = state.res;
  const lo = H0 * 60, slots = Math.ceil(((H1 + 1) * 60 - lo) / res);
  const bucket = pick => {
    const out = new Array(slots).fill(0);
    days.forEach(d => (pick(d) || []).forEach(([m, v]) => { const i = Math.floor((m - lo) / res); if (i >= 0 && i < slots) out[i] += v; }));
    return out;
  };
  const hoursUnit = !avg && res === 60;
  const time = arr => arr.map(s => s / (hoursUnit ? 3600 : 60) / (avg ? n : 1));
  const count = arr => arr.map(v => v / (avg ? n : 1));
  const fine = res < 15;
  const area = (key, label, data, color, fill, hidden) => ({ key, label, data, hidden, borderColor: color, backgroundColor: fill, fill: true,
    stack: "work", yAxisID: "y", pointRadius: 0, borderWidth: fine ? 0 : 1, stepped: fine, cubicInterpolationMode: "monotone" });
  const line = (key, label, data, color, hidden) => ({ key, label, data, hidden, borderColor: color, backgroundColor: color, fill: false,
    stack: key, yAxisID: "y1", pointRadius: 0, borderWidth: fine ? 1 : 2, cubicInterpolationMode: "monotone" });
  const ds = ALL.map(c => area("cat:" + c, c, time(bucket(d => d.min[c])), COLORS[c], COLORS[c] + "bb", !isOn(c)));
  ds.push(area("sig:idle", "Idle", time(bucket(d => d.min.idle)), IDLE_FILL, IDLE_FILL, !state.sig.idle));
  ds.push(line("sig:keys", "Keys pressed", count(bucket(d => d.mk)), INK, !state.sig.keys));
  ds.push(line("sig:clicks", "Mouse clicks", count(bucket(d => d.mc)), CLICKS, !state.sig.clicks));
  const labels = Array.from({ length: slots }, (_, i) => hhmm(lo + i * res));
  const per = RES_NAME[res];
  const yTitle = hoursUnit ? "hours, all days added up" : `minutes per ${per}` + (avg ? ", average day" : ", all days added up");
  chart("hourly", { type: "line", data: { labels, datasets: ds },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      onClick: slotClick, onHover: pointer,
      scales: {
        x: timeAxis(lo, res, labels),
        y: { stacked: true, beginAtZero: true, max: avg ? res : undefined, title: { display: true, text: yTitle }, afterFit: fitAxis },
        y1: { position: "right", beginAtZero: true, grid: { display: false }, afterFit: fitAxis,
              ticks: { display: state.sig.keys || state.sig.clicks },
              title: { display: state.sig.keys || state.sig.clicks, text: `keys / clicks per ${per}` + (avg ? ", average day" : "") } } },
      plugins: {
        legend: { onClick: (e, item, legend) => {
          const key = legend.chart.data.datasets[item.datasetIndex].key;
          setTimeout(() => {
            if (key.startsWith("cat:")) toggleCat(key.slice(4)); else state.sig[key.slice(4)] = !state.sig[key.slice(4)];
            render();
          });
        } },
        tooltip: { filter: c => c.raw > 0.0005, callbacks: { label: c => {
          const d = c.dataset;
          if (d.yAxisID === "y1") return `${d.label}: ${num(c.raw)}`;
          return `${d.label}: ${hoursUnit ? fmt(c.raw) : (c.raw < 10 ? c.raw.toFixed(1) : Math.round(c.raw)) + " min"}`;
        } } } } } });
}

function renderWorkday() {
  const days = selDays(), w = days.map(workday), have = w.filter(Boolean);
  const pick = f => w.map(x => x ? f(x) : null);
  const avg = f => { const v = have.map(f).filter(x => x != null); return v.length ? sum(v) / v.length : null; };
  const note = $("worknote");
  if (!have.length) note.textContent = "No activity in this range.";
  else {
    const s = avg(x => x.start), e = avg(x => x.end), ls = avg(x => x.lunch ? x.lunch[0] : null), ll = avg(x => x.lunch ? x.lunch[1] - x.lunch[0] : null);
    const lunchDays = have.filter(x => x.lunch).length;
    note.textContent = (days.length > 1 ? "On average you start at " : "You started at ") + hhmm(s) +
      (ls != null ? `, take lunch at ${hhmm(ls)} for ${dur(ll)}` : ", with no lunch break found") +
      `, and finish at ${hhmm(e)}.` + (days.length > 1 && ls != null ? ` A lunch break was found on ${lunchDays} of ${have.length} days.` : "") +
      ` Lunch is the longest break that starts between ${hhmm(SET.lunchFrom)} and ${hhmm(SET.lunchTo)} and lasts ${SET.lunchMin} to ${SET.lunchMax} minutes.`;
  }
  const labels = days.map(d => shortDay(d.day));
  const L = (label, data, color, axis, extra = {}) => ({ type: "line", label, data, borderColor: color, backgroundColor: color, yAxisID: axis,
    pointRadius: 3, pointHoverRadius: 5, borderWidth: 2, tension: .25, spanGaps: false, ...extra });
  const times = have.flatMap(x => [x.start, x.end]);
  const lunchBar = { type: "bar", label: "Lunch", data: pick(x => x.lunch ? [x.lunch[0] / 60, x.lunch[1] / 60] : null), yAxisID: "t",
    backgroundColor: "rgba(245,159,0,.35)", borderColor: "#f59f00", borderWidth: 1, borderRadius: 3, borderSkipped: false, barPercentage: .5 };
  chart("workday", { data: { labels, datasets: [
      L("Start", pick(x => x.start / 60), "#2f9e44", "t"), lunchBar, L("Finish", pick(x => x.end / 60), "#c2255c", "t")] },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { x: { grid: { display: false } },
        t: { min: times.length ? Math.floor(Math.min(...times) / 60) - 1 : 6, max: times.length ? Math.ceil(Math.max(...times) / 60) + 1 : 20,
             ticks: { stepSize: 1, callback: v => hhmm(v * 60) }, title: { display: true, text: "time of day" } } },
      plugins: { tooltip: { callbacks: { label: c => {
        if (c.raw == null) return null;
        if (Array.isArray(c.raw)) return `Lunch: ${hhmm(c.raw[0] * 60)}–${hhmm(c.raw[1] * 60)} (${dur((c.raw[1] - c.raw[0]) * 60)})`;
        return `${c.dataset.label}: ${hhmm(c.raw * 60)}`;
      } } } } } });
  chart("worklen", { data: { labels, datasets: [
      L("Hours worked", pick(x => x.worked), "#3b5bdb", "h"),
      L("Lunch length", pick(x => x.lunch ? x.lunch[1] - x.lunch[0] : null), "#f59f00", "m", { borderDash: [5, 4] })] },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: { x: { grid: { display: false } },
        h: { position: "left", beginAtZero: true, title: { display: true, text: "hours worked" } },
        m: { position: "right", beginAtZero: true, grid: { display: false }, title: { display: true, text: "lunch, minutes" } } },
      plugins: { tooltip: { callbacks: { label: c => c.raw == null ? null :
        `${c.dataset.label}: ${c.dataset.yAxisID === "h" ? fmt(c.raw) : dur(c.raw)}` } } } } });
}

function renderTimeline() {
  const days = selDays(), rows = $("rows"), ticks = $("ticks"), leg = $("tlLegend");
  rows.replaceChildren(); ticks.replaceChildren(); leg.replaceChildren();
  const lo = H0 * 60, hi = (H1 + 1) * 60, span = hi - lo;
  const place = (node, a, b) => { node.style.left = (a - lo) / span * 100 + "%"; node.style.width = Math.max(.15, (b - a) / span * 100) + "%"; };
  for (let m = lo; m <= hi; m += span > 720 ? 180 : 120) ticks.append(el("span", { style: `left:${(m - lo) / span * 100}%` }, hhmm(m)));
  if (!days.length) { rows.append(el("p", { className: "empty-state" }, "No logs in this date range.")); return; }
  const moodsSeen = new Set();
  let totAct = 0, totIdle = 0;
  [...days].reverse().forEach(d => {
    const w = workday(d);
    const row = el("div", { className: "row" });
    const lab = el("div", { className: "date" }, parse(d.day).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }));
    lab.append(el("small", {}, w ? `${hhmm(w.start)}–${hhmm(w.end)}` : "no activity"));
    const strips = el("div", { className: "strips" }), strip = el("div", { className: "strip" });
    d.segments.forEach(([a, b, c]) => {
      if (c === "idle" ? !state.sig.idle : !isOn(c)) return;
      const seg = el("i", { className: c === "idle" ? "idle" : "", title: `${hhmm(a)}–${hhmm(b)}  ${c}` });
      place(seg, a, b); if (c !== "idle") seg.style.background = COLORS[c] || COLORS.Other;
      strip.append(seg);
    });
    if (w && w.lunch) { const l = el("b", { className: "lunch", title: `Lunch ${hhmm(w.lunch[0])}–${hhmm(w.lunch[1])}` }); place(l, ...w.lunch); strip.append(l); }
    strips.append(strip);
    const atClick = (node, segs) => node.onclick = e => {
      const r = node.getBoundingClientRect(), m = lo + (e.clientX - r.left) / r.width * span;
      const s = segs.find(([a, b]) => m >= a && m < b);
      const [a, b] = s ? [Math.floor(s[0]), Math.ceil(s[1])] : [Math.floor(m / 15) * 15, Math.floor(m / 15) * 15 + 15];
      openDetail(d.day, d.day, a, b);
    };
    atClick(strip, d.segments);
    // Presence: active vs idle at the computer; the empty background is away, locked or off
    const pres = el("div", { className: "strip presence" }), runs = [];
    d.segments.forEach(([a, b, c]) => {
      if (c !== "idle" && !isOn(c)) return;
      const kind = c === "idle" ? "idle" : "active", last = runs[runs.length - 1];
      if (last && last[2] === kind && a - last[1] < 0.5) last[1] = b; else runs.push([a, b, kind]);
    });
    runs.forEach(([a, b, kind]) => {
      const seg = el("i", { className: kind === "idle" ? "idle" : "", title: `${hhmm(a)}–${hhmm(b)}  ${kind} (${dur(b - a)})` });
      place(seg, a, b); pres.append(seg);
    });
    atClick(pres, d.segments);
    strips.append(pres);
    if (state.sig.music && d.msegs.length) {
      const ms = el("div", { className: "strip music" });
      d.msegs.forEach(([a, b, t]) => {
        const mood = moodOf(t); moodsSeen.add(mood);
        const seg = el("i", { title: `${hhmm(a)}–${hhmm(b)}  ${t}  (${mood} · ${genreOf(t)})` }); place(seg, a, b);
        seg.style.background = MOOD_COLOR[mood]; ms.append(seg);
      });
      atClick(ms, d.msegs);
      strips.append(ms);
    }
    const act = workedSec(d) / 60, idl = sum(d.idle) / 60, atPc = act + idl;
    totAct += act; totIdle += idl;
    const hrs = el("div", { className: "hours" }); hrs.append(el("b", {}, fmt(act / 60)));
    const ratio = el("div", { className: "ratio", title: `${dur(act)} active, ${dur(idl)} idle` });
    if (atPc) { const a = el("span", { className: "a" }), i = el("span", { className: "i" });
      a.style.width = act / atPc * 100 + "%"; i.style.width = idl / atPc * 100 + "%"; ratio.append(a, i); }
    hrs.append(ratio, el("small", {}, `${dur(idl)} idle` + (atPc ? ` (${Math.round(idl / atPc * 100)}%)` : "")));
    if (w && w.lunch) hrs.append(el("small", {}, `lunch ${dur(w.lunch[1] - w.lunch[0])}`));
    row.append(lab, strips, hrs); rows.append(row);
  });
  const key = (label, c) => { const s = el("span", {}, label); s.style.setProperty("--c", c); leg.append(s); };
  const tot = totAct + totIdle;
  $("presnote").textContent = tot ? `${fmt(totAct / 60)} active and ${fmt(totIdle / 60)} idle; idle is ${Math.round(totIdle / tot * 100)}% of your time at the computer` : "";
  leg.append(el("span", {}, "Thin strips:"), el("span", { className: "k-active" }, "Active"),
    el("span", { className: "k-idle" }, `Idle (no keyboard or mouse for ${Math.round(SET.idleAfter / 60)}+ min)`),
    el("span", { className: "k-away" }, "Away, locked or off"));
  leg.append(el("span", { className: "lunchkey" }, "Lunch break"));
  if (moodsSeen.size) { leg.append(el("span", {}, "Music by mood:")); MOODS.filter(m => moodsSeen.has(m)).forEach(m => key(m, MOOD_COLOR[m])); }
}

function renderDaily() {
  const days = selDays(), labels = days.map(d => shortDay(d.day));
  const ds = onCats().map(c => ({ label: c, data: days.map(d => catSec(d, c) / 3600), backgroundColor: COLORS[c] }));
  if (state.sig.idle) ds.push({ label: "Idle", data: days.map(d => sum(d.idle) / 3600), backgroundColor: IDLE_FILL });
  if (state.sig.music) ds.push({ label: "Listening", type: "line", stack: "music", data: days.map(d => sum(d.music) / 3600),
    borderColor: MUSIC, backgroundColor: MUSIC, borderDash: [6, 4], tension: .3 });
  chart("daily", { type: "bar", data: { labels, datasets: ds }, options: { responsive: true, maintainAspectRatio: false,
    scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, title: { display: true, text: "hours" } } },
    plugins: { tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmt(c.raw)}` } } } } });
  const showInput = state.sig.keys || state.sig.clicks;
  $("inputSec").hidden = !showInput;
  if (showInput) {
    const ids = [];
    if (state.sig.keys) ids.push({ label: "Keys pressed", data: days.map(d => sum(d.keys)), backgroundColor: INK, yAxisID: "y" });
    if (state.sig.clicks) ids.push({ label: "Mouse clicks", data: days.map(d => sum(d.clicks)), backgroundColor: CLICKS, yAxisID: state.sig.keys ? "y1" : "y" });
    chart("input", { type: "bar", data: { labels, datasets: ids }, options: { responsive: true, maintainAspectRatio: false,
      scales: { x: { grid: { display: false } }, y: { beginAtZero: true },
        y1: { display: state.sig.keys && state.sig.clicks, position: "right", beginAtZero: true, grid: { display: false } } } } });
  }
}

// ---- lists and table
function merged(key, keep = () => true) {
  const out = {};
  selDays().forEach(d => Object.entries(d[key]).forEach(([k, v]) => { if (keep(k)) out[k] = (out[k] || 0) + v; }));
  return Object.entries(out).sort((a, b) => b[1] - a[1]);
}
function listTable(id, head, rows, empty, colorOf) {
  const t = $(id); t.replaceChildren();
  const tr = el("tr"); tr.append(el("th", {}, head), el("th", {}, "Time")); t.append(tr);
  if (!rows.length) { const r = el("tr"); r.append(el("td", {}, empty), el("td")); t.append(r); }
  rows.slice(0, 15).forEach(([k, s]) => {
    const r = el("tr"), td = el("td");
    if (colorOf) { const dot = el("span", { className: "dot" }); dot.style.background = colorOf(k); td.append(dot); }
    td.append(document.createTextNode(k));
    r.append(td, el("td", {}, fmt(s / 3600))); t.append(r);
  });
}
const sortObj = o => Object.entries(o).sort((a, b) => b[1] - a[1]);
function renderLists() {
  const days = selDays();
  $("musicSec").hidden = !state.sig.music;
  if (state.sig.music) {
    const tracks = merged("tracks"), artists = {}, genres = {}, moods = {};
    tracks.forEach(([t, s]) => { const a = artistOf(t), g = genreOf(t), m = moodOf(t);
      artists[a] = (artists[a] || 0) + s; genres[g] = (genres[g] || 0) + s; moods[m] = (moods[m] || 0) + s; });
    const pending = D.genresPending && !D.genreError ? ` Looking up genres for ${D.genresPending} more artist${D.genresPending > 1 ? "s" : ""}…` : "";
    $("musicnote").textContent = `${fmt(sum(days.map(d => sum(d.music))) / 3600)} of Spotify, whether or not it was in front.${pending}`;
    const warn = $("genrewarn");
    warn.hidden = !(D.genreError || D.genresOff);
    warn.textContent = D.genresOff ? "Genre lookup is turned off (LOOKUP_GENRES in report.py), so moods show as Unknown."
      : `Genre lookup isn't working, so moods show as Unknown. ${D.genreError} For details, run: python3 report.py --check`;
    listTable("moods", "Mood", sortObj(moods), "No tracks logged in this range.", m => MOOD_COLOR[m]);
    listTable("genres", "Genre", sortObj(genres), "No tracks logged in this range.", genreColor);
    listTable("artists", "Artist", sortObj(artists), "No tracks logged in this range.");
    listTable("tracks", "Track", tracks, "No tracks logged in this range.", t => MOOD_COLOR[moodOf(t)]);
    renderMusicWork();
  }
  const sites = merged("sites", k => isOn(k.split("|")[1])).map(([k, s]) => { const [site, cat] = k.split("|"); return [`${site}  (${cat})`, s]; });
  $("sitesSec").hidden = !sites.length;
  listTable("sites", "Site (category)", sites, "");
  const uns = merged("unsorted", k => isOn(k.split("|")[0])).map(([k, s]) => [k.slice(k.indexOf("|") + 1), s]);
  $("unsortedSec").hidden = !uns.length;
  listTable("unsorted", "App and window", uns, "");
}
function renderMusicWork() {
  const rows = {};
  selDays().forEach(d => d.segments.forEach(([a, b, c]) => {
    if (c === "idle" || !isOn(c)) return;
    const r = rows[c] || (rows[c] = { total: 0, music: 0, moods: {} });
    r.total += b - a;
    d.msegs.forEach(([x, y, t]) => { const o = Math.min(b, y) - Math.max(a, x);
      if (o > 0) { r.music += o; const m = moodOf(t); r.moods[m] = (r.moods[m] || 0) + o; } });
  }));
  const t = $("musicwork"); t.replaceChildren();
  const head = el("tr"); ["Activity", "Time", "With music", "Mood mix", ...MOODS].forEach(h => head.append(el("th", {}, h))); t.append(head);
  Object.entries(rows).sort((a, b) => b[1].total - a[1].total).forEach(([c, r]) => {
    const tr = el("tr"), mix = el("div", { className: "mix" });
    MOODS.forEach(m => { if (!r.moods[m]) return; const s = el("span", { title: `${m}: ${dur(r.moods[m])}` });
      s.style.width = r.moods[m] / r.music * 100 + "%"; s.style.background = MOOD_COLOR[m]; mix.append(s); });
    const pct = m => r.music ? Math.round((r.moods[m] || 0) / r.music * 100) + "%" : "–";
    tr.append(cell(dotOf(COLORS[c]), c), cell(dur(r.total)), cell(r.total ? Math.round(r.music / r.total * 100) + "%" : "–"),
      cell(mix), ...MOODS.map(m => cell(pct(m))));
    t.append(tr);
  });
}

function renderProjects() {
  const P = {}, F = {};
  selDays().forEach(d => {
    Object.entries(d.projects || {}).forEach(([k, s]) => {
      const [topic, c] = k.split(SEP); if (!isOn(c)) return;
      const p = P[topic] || (P[topic] = { s: 0, days: new Set(), first: d.day, last: d.day, cats: {} });
      p.s += s; p.days.add(d.day); p.last = d.day; p.cats[c] = (p.cats[c] || 0) + s;
    });
    Object.entries(d.pfiles || {}).forEach(([k, s]) => {
      const [topic, c, f] = k.split(SEP); if (!isOn(c)) return;
      const files = F[topic] || (F[topic] = {}); files[f] = (files[f] || 0) + s;
    });
  });
  const list = Object.entries(P).sort((a, b) => b[1].s - a[1].s).slice(0, 30);
  $("projSec").hidden = !list.length;
  $("projnote").textContent = "From git repos and VS Code folders, plus your own TOPICS patterns in report.py.";
  const t = $("projects"); t.replaceChildren();
  const head = el("tr"); ["Project or topic", "Time", "Days", "First", "Last", "Mostly", "Top files and tasks"].forEach(h => head.append(el("th", {}, h))); t.append(head);
  const max = list.length ? list[0][1].s : 1;
  list.forEach(([topic, p]) => {
    const tr = el("tr"), name = el("td"), bar = el("span", { className: "share" });
    bar.style.width = p.s / max * 100 + "%"; name.append(bar, el("span", { className: "t" }, topic));
    const mostly = sortObj(p.cats)[0][0];
    const files = sortObj(F[topic] || {}).slice(0, 3).map(([f]) => f).join(", ");
    tr.append(name, cell(fmt(p.s / 3600)), cell(String(p.days.size)), cell(shortDay(p.first)), cell(shortDay(p.last)),
      cell(dotOf(COLORS[mostly]), mostly), el("td", { style: "text-align:left;white-space:normal" }, files));
    t.append(tr);
  });
}

function renderTable() {
  const days = selDays(), cats = onCats(), t = $("table"); t.replaceChildren();
  const W = new Map(days.map(d => [d.day, workday(d)]));
  const cols = [["Day", d => d.day], ["Start", d => W.get(d.day) ? hhmm(W.get(d.day).start) : "–"],
    ["Finish", d => W.get(d.day) ? hhmm(W.get(d.day).end) : "–"],
    ["Lunch", d => { const w = W.get(d.day); return w && w.lunch ? `${hhmm(w.lunch[0])} (${dur(w.lunch[1] - w.lunch[0])})` : "–"; }],
    ["Worked", d => fmt(workedSec(d) / 3600)]];
  if (state.sig.idle) cols.push(["Idle", d => fmt(sum(d.idle) / 3600)]);
  cols.push(["Locked", d => fmt(sum(d.locked) / 3600)]);
  if (state.sig.music) cols.push(["Listening", d => fmt(sum(d.music) / 3600)]);
  if (state.sig.keys) cols.push(["Keys", d => num(sum(d.keys))]);
  if (state.sig.clicks) cols.push(["Clicks", d => num(sum(d.clicks))]);
  cats.forEach(c => cols.push([c, d => catSec(d, c) ? fmt(catSec(d, c) / 3600) : "–"]));
  const head = el("tr"); cols.forEach(([h]) => head.append(el("th", {}, h))); t.append(head);
  [...days].reverse().forEach(d => { const tr = el("tr"); cols.forEach(([, f]) => tr.append(el("td", {}, f(d)))); t.append(tr); });
}

function render() {
  if (!D || !D.days.length) return;
  save(); renderHeader(); renderCalendar(); renderControls(); renderSummary();
  renderHourly(); renderLane(); renderDetail(); renderWorkday(); renderTimeline(); renderDaily(); renderProjects();
  renderLists(); renderTable();
}

// ---- fixed controls
PRESETS.forEach(([label, n]) => {
  const b = el("button", { type: "button" }, label);
  b.onclick = () => D && setRange(...presetRange(n));
  $("presets").append(b);
});
$("rangeBtn").onclick = () => { state.calOpen = !state.calOpen; save(); renderHeader(); };
$("from").onchange = e => e.target.value && setRange(e.target.value, state.end < e.target.value ? e.target.value : state.end);
$("to").onchange = e => e.target.value && setRange(state.start > e.target.value ? e.target.value : state.start, e.target.value);
const markPills = () => {
  document.querySelectorAll("button[data-mode]").forEach(b => b.setAttribute("aria-pressed", b.dataset.mode === state.mode));
  document.querySelectorAll("button[data-res]").forEach(b => b.setAttribute("aria-pressed", +b.dataset.res === state.res));
};
document.querySelectorAll("button[data-mode]").forEach(b => b.onclick = () => { state.mode = b.dataset.mode; markPills(); render(); });
document.querySelectorAll("button[data-res]").forEach(b => b.onclick = () => { state.res = +b.dataset.res; markPills(); render(); });
markPills();
$("allOn").onclick = () => { state.solo = null; state.hidden.clear(); Object.keys(state.sig).forEach(k => state.sig[k] = true); render(); };
$("allOff").onclick = () => { state.solo = null; ALL.forEach(c => state.hidden.add(c)); render(); };
document.addEventListener("keydown", e => { if (e.key === "Escape" && state.calOpen) { state.calOpen = false; save(); renderHeader(); } });

function status(text, cls) { const g = $("status"); g.textContent = text; g.className = "status " + (cls || ""); }
const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function start(data) {
  D = data;
  if (!D.days.length) { status("No logs yet. Start tracker.py and this page fills in by itself.", LIVE ? "live" : ""); return false; }
  index();
  state.start = D.days[Math.max(0, D.days.length - 7)].day; state.end = LAST;
  render();
  return true;
}
async function poll() {
  try {
    const r = await fetch(`/api/data?since=${lastStamp}`, { cache: "no-store" });
    const p = await r.json();
    lastStamp = p.now;
    if (!D || !D.days.length) { if (start(p)) status(`Live, updated ${clock()}`, "live"); return; }
    const byDay = new Map(D.days.map(d => [d.day, d]));
    p.days.forEach(d => byDay.set(d.day, d));
    const keep = new Set(p.all), oldLast = LAST, oldMusic = JSON.stringify(MUSICINFO) + D.genreError;
    D = { ...p, days: [...byDay.values()].filter(d => keep.has(d.day)) };
    index();
    if (state.end === oldLast && LAST !== oldLast) state.end = LAST;
    if (p.days.length || JSON.stringify(MUSICINFO) + D.genreError !== oldMusic) render();
    status(`Live, updated ${clock()}`, "live");
  } catch (e) {
    status("Can't reach the report server. Start it with: python report.py --serve", "live offline");
  }
}

if (window.Chart) {
  Chart.defaults.color = css.getPropertyValue("--muted").trim();
  Chart.defaults.borderColor = css.getPropertyValue("--rule").trim();
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.animation = false;
  Chart.defaults.plugins.legend.labels.boxWidth = 14;
}
if (LIVE) { status("Loading your logs…", "live"); poll(); setInterval(poll, POLL_SECONDS * 1000); }
else if (start(EMBED)) status(`Saved ${EMBED.generated}. Run report.py --serve for a live page.`);
</script></body></html>
"""

if __name__ == "__main__":
    main()
