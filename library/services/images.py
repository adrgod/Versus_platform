"""Download one picture from the web, safely: size-capped and checked to really be an image."""
import urllib.error
import urllib.request

MAX_BYTES = 8 * 1024 * 1024


def looks_like_image(data):
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def download_image(url, user_agent, *, referer=None, timeout=30):
    """The image bytes at ``url``; None when there is no picture (404, or not an image).

    Raises ``urllib.error.URLError`` / ``HTTPError`` for other failures so callers can report them.
    """
    headers = {"User-Agent": user_agent, "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in (404, 410):
            return None
        raise
    if len(data) > MAX_BYTES or not looks_like_image(data):
        return None
    return data
