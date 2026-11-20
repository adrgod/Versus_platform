"""Album reviews: a formatted text, a 1-10 score, who wrote it and when."""
import json
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from .models import Album, Review, UserPreference

MAX_HTML = 30000
ALLOWED_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "s", "strike", "del", "ul", "ol", "li",
                "blockquote", "div", "a", "h3"}
BLOCK_TAGS = {"p", "div", "li", "blockquote", "h3", "ul", "ol"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript"}
SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


class _Cleaner(HTMLParser):
    """Rebuilds the HTML keeping only a few harmless tags (no attributes except safe link hrefs)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.text, self.stack, self.skip = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        if tag == "br":
            self.out.append("<br>")
            self.text.append("\n")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if urlparse(href.strip()).scheme.lower() not in SAFE_LINK_SCHEMES:
                self.stack.append(None)  # unsafe link: keep its text, drop the tag
                return
            self.out.append(f'<a href="{escape(href.strip(), quote=True)}" target="_blank" rel="noopener noreferrer">')
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED_TAGS or tag == "br":
            return
        if tag in self.stack:
            while self.stack:
                open_tag = self.stack.pop()
                if open_tag:
                    self.out.append(f"</{open_tag}>")
                if open_tag == tag or (open_tag is None and tag == "a"):
                    break
        if tag in BLOCK_TAGS:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))
            self.text.append(data)

    def finish(self):
        self.close()
        while self.stack:
            open_tag = self.stack.pop()
            if open_tag:
                self.out.append(f"</{open_tag}>")
        text = "".join(self.text)
        lines = [line.rstrip() for line in text.replace("\xa0", " ").split("\n")]
        text = "\n".join(lines).strip()
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return "".join(self.out), text


def clean_html(html):
    """(safe html, plain text) for the review editor's output."""
    cleaner = _Cleaner()
    cleaner.feed(html[:MAX_HTML])
    return cleaner.finish()


def reviewer_name(user):
    """The name shown on reviews: the one chosen in the account, else the full name, else the login."""
    row = UserPreference.objects.filter(user=user).only("display_name").first()
    return (row.display_name if row else "") or user.get_full_name() or user.get_username()


def default_body(album, label=""):
    """What a new review starts with: ARTIST, «Album», (Label), and an empty line to type in."""
    lines = [f"<p><strong>{escape(album.artist.artist_name.upper())}</strong></p>",
             f"<p>«{escape(album.album_name)}»</p>"]
    if label:
        lines.append(f"<p>({escape(label)})</p>")
    lines.append("<p><br></p>")
    return "".join(lines)


def review_context(request, album, label=""):
    review = Review.objects.filter(user=request.user, album=album).first()
    return {
        "review": review,
        "review_html": review.body_html if review else default_body(album, label),
        "review_author": review.author_name if review else reviewer_name(request.user),
        "review_score": review.score if review else 0,
    }


def _payload(review):
    return {"ok": True, "score": review.score, "author": review.author_name,
            "written": review.written_at.strftime("%d %b %Y").lstrip("0"),
            "updated": review.updated_at.strftime("%d %b %Y %H:%M")}


@login_required
@require_POST
def save_review(request, album_id):
    album = get_object_or_404(Album, pk=album_id)
    try:
        data = json.loads(request.body or b"{}")
        score = int(data.get("score", 0))
        html = str(data.get("html", ""))
        author = str(data.get("author", "")).strip()[:100]
    except (ValueError, TypeError, AttributeError):
        return HttpResponseBadRequest("Invalid request")
    if not 1 <= score <= 10:
        return JsonResponse({"ok": False, "error": "Pick a score from 1 to 10 first."}, status=400)
    safe_html, text = clean_html(html)
    # The name is remembered on the account; left empty it falls back to full name / login.
    row, _ = UserPreference.objects.get_or_create(user=request.user)
    if row.display_name != author:
        row.display_name = author
        row.save(update_fields=["display_name", "updated_at"])
    review, _ = Review.objects.update_or_create(
        user=request.user, album=album,
        defaults={"author_name": author or reviewer_name(request.user), "score": score,
                  "body_html": safe_html, "body_text": text})
    return JsonResponse(_payload(review))


@login_required
@require_POST
def delete_review(request, album_id):
    Review.objects.filter(user=request.user, album_id=album_id).delete()
    return JsonResponse({"ok": True})


REVIEW_SORTS = {
    "newest": ("Newest", ["-written_at"]),
    "score": ("Highest score", ["-score", "-written_at"]),
    "lowest": ("Lowest score", ["score", "-written_at"]),
    "album": ("Album", ["album__album_name"]),
    "artist": ("Artist", ["album__artist__artist_name", "album__album_name"]),
}


@login_required
@require_GET
def review_list(request):
    search = request.GET.get("q", "").strip()
    sort = request.GET.get("sort") if request.GET.get("sort") in REVIEW_SORTS else "newest"
    reviews = Review.objects.select_related("album__artist")
    if search:
        reviews = reviews.filter(
            Q(album__album_name__icontains=search) | Q(album__artist__artist_name__icontains=search)
            | Q(album__resolved_label__icontains=search) | Q(author_name__icontains=search)
            | Q(body_text__icontains=search))
    reviews = list(reviews.order_by(*REVIEW_SORTS[sort][1])[:300])
    for review in reviews:
        excerpt = " ".join(review.body_text.split())
        review.excerpt = excerpt[:320] + ("…" if len(excerpt) > 320 else "")
    return render(request, "library/review_list.html", {
        "reviews": reviews, "search": search, "sort": sort,
        "sorts": [(key, label) for key, (label, _) in REVIEW_SORTS.items()]})
