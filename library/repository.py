import base64
import json
import os
import string
from pathlib import Path

from django.conf import settings


PROJECT_ROOT = Path(settings.BASE_DIR)
IS_WINDOWS = os.name == "nt"


def get_config():
    with (PROJECT_ROOT / "config.json").open(encoding="utf-8") as config_file:
        return json.load(config_file)


def default_source_folder():
    return Path(get_config()["source_data_folder"]).expanduser().resolve()


def get_log_directory():
    return PROJECT_ROOT / "db" / "tag_write_logs"


def windows_drives():
    """Every drive letter that is present (C:\\, D:\\, a USB stick...), Windows only."""
    drives = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        try:
            if drive.is_dir():
                drives.append(drive)
        except OSError:  # an empty card reader, a disconnected network drive
            continue
    return drives


def get_allowed_roots():
    config = get_config()
    configured_roots = config.get("allowed_library_roots")
    if not configured_roots:
        configured_roots = [str(Path(config["source_data_folder"]).expanduser().resolve().parent)]
    roots = []
    for configured_root in configured_roots:
        root = Path(configured_root).expanduser().resolve()
        if IS_WINDOWS and root.parent == root:
            # "/" means the whole computer: on Windows that is every drive, not just the current one.
            candidates = windows_drives() or [root]
        else:
            candidates = [root]
        for candidate in candidates:
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
    return roots


def resolve_allowed_folder(folder_path):
    path = Path(folder_path).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("That folder does not exist or is not accessible.")
    roots = [Path(root).expanduser().resolve() for root in get_allowed_roots()]
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("That folder is outside the configured music-library roots.")
    return path


def is_allowed_file(file_path):
    path = Path(file_path).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in get_allowed_roots()]
    return path.is_file() and any(
        path == root or root in path.parents for root in roots
    )


def album_cover_data_uri(image_data):
    if not image_data:
        return None
    image_data = bytes(image_data)

    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
        mime_type = "image/webp"
    else:
        mime_type = "image/jpeg"
    return f"data:{mime_type};base64,{base64.b64encode(image_data).decode('ascii')}"
