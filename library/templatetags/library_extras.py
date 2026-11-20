from django import template
from django.utils.html import format_html

from library.services.flags import flag_code

register = template.Library()


@register.filter
def flag(country):
    """A small flag image for a country name; nothing when the country is unknown.

    Flag emoji do not draw on Windows, so an image from flagcdn.com is used. If it cannot load
    (offline) the image removes itself and only the country name shows.
    """
    code = flag_code(country)
    if not code:
        return ""
    return format_html(
        '<img class="flag" src="https://flagcdn.com/24x18/{}.png" srcset="https://flagcdn.com/48x36/{}.png 2x" '
        'width="20" height="15" alt="" loading="lazy" onerror="this.remove()"> ',
        code, code,
    )
