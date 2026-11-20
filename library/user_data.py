"""Per-user data: preferences (display, equalizer, volume), play history and star ratings."""
import json
import re

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from .models import Album, AlbumRating, PlayEvent, Review, Track, TrackRating, UserPreference

DISPLAY_KEYS = ("theme", "accent", "grid", "corners", "motion")
EQ_BANDS = ("60", "250", "1000", "4000", "12000")
_WORD = re.compile(r"^[a-z-]{1,20}$")


def preferences_for(user):
    """What to send to the browser: {"display": {...}, "eq": {...}, "volume": 0.8|None}."""
    if not getattr(user, "is_authenticated", False):
        return None
    row = UserPreference.objects.filter(user=user).first()
    if row is None:
        return {"display": {}, "eq": {}, "volume": None}
    return {"display": row.display, "eq": row.eq, "volume": row.volume}


def clean_preferences(data):
    """Keep only known keys with sane values; anything else is dropped."""
    clean = {}
    display = data.get("display")
    if isinstance(display, dict):
        clean["display"] = {key: value for key, value in display.items()
                            if key in DISPLAY_KEYS and isinstance(value, str) and _WORD.match(value)}
    eq = data.get("eq")
    if isinstance(eq, dict):
        clean["eq"] = {band: max(-12, min(12, float(value))) for band, value in eq.items()
                       if band in EQ_BANDS and isinstance(value, (int, float)) and not isinstance(value, bool)}
    volume = data.get("volume")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        clean["volume"] = max(0.0, min(1.0, float(volume)))
    return clean


def user_prefs(request):
    """Template context: the user's saved preferences, for the first paint."""
    return {"user_prefs": preferences_for(request.user)}


@login_required
def preferences(request):
    if request.method == "GET":
        return JsonResponse(preferences_for(request.user))
    if request.method != "POST":
        return HttpResponseBadRequest("GET or POST only")
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponseBadRequest("Invalid JSON")
    if not isinstance(data, dict):
        return HttpResponseBadRequest("Expected an object")
    clean = clean_preferences(data)
    row, _ = UserPreference.objects.get_or_create(user=request.user)
    if "display" in clean:
        row.display = clean["display"]
    if "eq" in clean:
        row.eq = {**row.eq, **clean["eq"]}
    if "volume" in clean:
        row.volume = clean["volume"]
    row.save()
    return JsonResponse(preferences_for(request.user))


@login_required
@require_POST
def record_play(request, track_id):
    track = get_object_or_404(Track, pk=track_id)
    PlayEvent.objects.create(user=request.user, track=track)
    return JsonResponse({"ok": True})


MAX_STARS = {"track": 5, "album": 10}  # an album is rated 1-10, the same score as its review


def _stars(request, top=5):
    try:
        stars = int(json.loads(request.body or b"{}").get("stars", 0))
    except (ValueError, AttributeError, TypeError):
        return None
    return stars if 0 <= stars <= top else None


@login_required
@require_POST
def rate(request, kind, object_id):
    """Set or clear (0) the user's rating: 1-5 stars for a track, 1-10 for an album (the same score as its review)."""
    stars = _stars(request, MAX_STARS.get(kind, 5))
    if stars is None:
        return HttpResponseBadRequest(f"stars must be 0-{MAX_STARS.get(kind, 5)}")
    if kind == "track":
        model, field, target = TrackRating, "track", get_object_or_404(Track, pk=object_id)
    elif kind == "album":
        model, field, target = AlbumRating, "album", get_object_or_404(Album, pk=object_id)
    else:
        return HttpResponseBadRequest("kind must be track or album")
    lookup = {"user": request.user, field: target}
    review = Review.objects.filter(user=request.user, album=target).first() if kind == "album" else None
    if stars == 0:
        model.objects.filter(**lookup).delete()
    else:
        model.objects.update_or_create(**lookup, defaults={"stars": stars})
    if review and review.score != stars:  # the album's rating and its review's score are one value (0 = none)
        review.score = stars
        review.save(update_fields=["score", "updated_at"])
    return JsonResponse({"stars": stars})


@login_required
@require_GET
def history(request):
    plays = list(PlayEvent.objects.filter(user=request.user)
                 .select_related("track__album__artist")[:200])
    ratings = {r.track_id: r.stars for r in TrackRating.objects.filter(
        user=request.user, track_id__in=[p.track_id for p in plays])}
    for play in plays:
        play.stars = ratings.get(play.track_id, 0)
    return render(request, "library/history.html", {"plays": plays})
