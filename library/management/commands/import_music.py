import json
from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from library.services.files_scanner import read_all_metadata

PROJECT_ROOT = Path(__file__).resolve().parents[3]

FIELD_ALIASES = {
    "title": "track_title",
    "track_title": "track_title",
    "tracknumber": "track_number",
    "track_number": "track_number",
    "length": "track_duration",
    "track_duration": "track_duration",
    "file_path": "track_file_path",
    "filepath": "track_file_path",
    "file_path": "track_file_path",
}


class Command(BaseCommand):
    help = "Import metadata from an MP3 file or a folder of MP3s."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path, help="Path to an MP3 file or folder.")
        parser.add_argument(
            "--model",
            required=True,
            help="Target model in app_label.ModelName format, e.g. library.Track",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview the import without saving to the database.",
        )

    def load_config(self):
        config_path = PROJECT_ROOT / "config.json"
        with config_path.open(encoding="utf-8") as config_file:
            return json.load(config_file)

    def resolve_path(self, value: str, *, base_dir: Path) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        return (base_dir / path).resolve()

    def handle(self, *args, **options):
        path = options["path"]
        if not path.exists():
            raise CommandError(f"Path not found: {path}")

        try:
            model = apps.get_model(options["model"])
        except (LookupError, ValueError):
            raise CommandError(
                f"Model not found: {options['model']}. Use app_label.ModelName."
            )

        config = self.load_config()

        if path.is_dir():
            metadata_records = read_all_metadata(str(path), config)
            if isinstance(metadata_records, dict):
                metadata_records = [metadata_records]
            if not metadata_records:
                raise CommandError(f"No metadata found in {path}")

            records_to_import = []
            for record in metadata_records:
                if record.get("file_path"):
                    records_to_import.append(record)
            if not records_to_import:
                raise CommandError(f"No MP3 records found in {path}")

            for record in records_to_import:
                self._save_record(record, model, options["dry_run"])

            return

        if path.suffix.lower() != ".mp3":
            raise CommandError(f"Not an MP3 file: {path}")

        metadata_records = read_all_metadata(str(path.parent), config)
        if isinstance(metadata_records, dict):
            metadata_records = [metadata_records]

        target_record = None
        for record in metadata_records:
            record_file = str(record.get("file_path") or record.get("path") or record.get("filename") or "")
            if record_file.endswith(path.name) or Path(record_file).name == path.name:
                target_record = record
                break

        if target_record is None:
            raise CommandError(
                f"No scanner metadata matched file: {path.name}. "
                "Check the folder and the scanner output."
            )

        self._save_record(target_record, model, options["dry_run"])

    def _normalize_field_name(self, field_name):
        key = str(field_name).strip().lower()
        return FIELD_ALIASES.get(key, key)

    def _resolve_album_for_track(self, record):
        artist_name = str(record.get("artist") or record.get("albumartist") or "").strip()
        album_name = str(record.get("album") or "").strip()
        publisher = str(record.get("publisher") or "").strip()
        date_value = record.get("date") or record.get("originaldate") or ""

        release_year = None
        if date_value:
            try:
                release_year = int(str(date_value).split("-")[0])
            except (TypeError, ValueError):
                release_year = None

        artist_model = apps.get_model("library", "Artist")
        album_model = apps.get_model("library", "Album")

        artist_obj, _ = artist_model.objects.get_or_create(
            artist_name=artist_name or "",
            defaults={
                "artist_country": "",
                "artist_formed_year": None,
            },
        )

        album_obj, _ = album_model.objects.get_or_create(
            artist=artist_obj,
            album_name=album_name or "",
            album_release_year=release_year,
            album_publisher=publisher,
            defaults={
                "album_discnumber": 1,
                "album_genre": str(record.get("genre") or "").strip(),
            },
        )
        return album_obj

    def _artist_identity(self, record):
        artist_name = str(record.get("artist") or record.get("albumartist") or "").strip()
        artist_country = str(record.get("artist_country") or record.get("country") or "").strip()
        if not artist_name:
            return None
        return {
            "artist_name": artist_name,
            "artist_country": artist_country,
        }

    def _album_identity(self, record):
        artist_name = str(record.get("artist") or record.get("albumartist") or "").strip()
        album_name = str(record.get("album") or "").strip()
        date_value = record.get("date") or record.get("originaldate") or ""
        release_year = None
        if date_value:
            try:
                release_year = int(str(date_value).split("-")[0])
            except (TypeError, ValueError):
                release_year = None

        if not artist_name or not album_name:
            return None

        artist_obj = apps.get_model("library", "Artist").objects.filter(
            artist_name=artist_name
        ).first()
        if artist_obj is None:
            return None

        return {
            "artist": artist_obj,
            "album_name": album_name,
            "album_release_year": release_year,
        }

    def _track_identity(self, record, cleaned):
        album_obj = self._ensure_album(record)
        track_title = str(cleaned.get("track_title") or record.get("title") or "").strip()
        track_duration = cleaned.get("track_duration")
        if album_obj is None or not track_title or track_duration in (None, "", [], {}):
            return None
        return {
            "album": album_obj,
            "track_title": track_title,
            "track_duration": track_duration,
        }

    def _identity_for_model(self, model, record, cleaned):
        if model.__name__ == "Artist":
            return self._artist_identity(record)
        if model.__name__ == "Album":
            return self._album_identity(record)
        if model.__name__ == "Track":
            return self._track_identity(record, cleaned)
        return {}

    def _ensure_artist(self, record):
        artist_identity = self._artist_identity(record)
        if not artist_identity:
            return None

        artist_model = apps.get_model("library", "Artist")
        artist_obj, _ = artist_model.objects.get_or_create(
            artist_name=artist_identity["artist_name"],
            artist_country=artist_identity["artist_country"],
            defaults={
                "artist_formed_year": None,
                "artist_photo": None,
            },
        )
        return artist_obj

    def _ensure_album(self, record):
        artist_obj = self._ensure_artist(record)
        if artist_obj is None:
            return None

        album_name = str(record.get("album") or "").strip()
        if not album_name:
            return None

        date_value = record.get("date") or record.get("originaldate") or ""
        release_year = None
        if date_value:
            try:
                release_year = int(str(date_value).split("-")[0])
            except (TypeError, ValueError):
                release_year = None

        album_model = apps.get_model("library", "Album")
        album_obj, _ = album_model.objects.get_or_create(
            artist=artist_obj,
            album_name=album_name,
            album_release_year=release_year,
            defaults={
                "album_discnumber": 1,
                "album_genre": str(record.get("genre") or "").strip(),
                "album_publisher": str(record.get("publisher") or "").strip(),
            },
        )
        return album_obj

    def _ensure_track(self, record, cleaned):
        album_obj = self._ensure_album(record)
        if album_obj is None:
            return None

        track_title = str(cleaned.get("track_title") or record.get("title") or "").strip()
        track_duration = cleaned.get("track_duration")
        if not track_title or track_duration in (None, "", [], {}):
            return None

        track_model = apps.get_model("library", "Track")
        track_obj, _ = track_model.objects.get_or_create(
            album=album_obj,
            track_title=track_title,
            track_duration=track_duration,
            defaults={
                "track_number": cleaned.get("track_number"),
                "track_file_path": str(record.get("file_path") or record.get("path") or "").strip(),
            },
        )
        return track_obj

    def _record_exists(self, model, cleaned, record=None):
        identity = self._identity_for_model(model, record or {}, cleaned)
        if not identity:
            return False
        return model.objects.filter(**identity).exists()

    def _save_record(self, record, model, dry_run):
        model_fields = {field.name.lower(): field.name for field in model._meta.fields}
        cleaned = {}

        for key, value in record.items():
            field_name = self._normalize_field_name(key)
            if field_name in model_fields and value not in (None, "", [], {}):
                cleaned[model_fields[field_name]] = value

        if model.__name__ == "Artist":
            if self._record_exists(model, cleaned, record):
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipped existing {model.__name__} with identity "
                        f"{self._artist_identity(record)}"
                    )
                )
                return
            with transaction.atomic():
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Dry run: would create {model.__name__} with "
                            f"{self._artist_identity(record)}"
                        )
                    )
                    return
                obj, created = model.objects.get_or_create(**self._artist_identity(record))
                if created:
                    self.stdout.write(self.style.SUCCESS(f"Created {model.__name__}: {obj}"))
                else:
                    self.stdout.write(self.style.WARNING(f"Skipped existing {model.__name__}: {obj}"))
            return

        if model.__name__ == "Album":
            if self._record_exists(model, cleaned, record):
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipped existing {model.__name__} with identity "
                        f"{self._album_identity(record)}"
                    )
                )
                return
            artist_obj = self._ensure_artist(record)
            if artist_obj is None:
                raise CommandError("Album metadata is missing the required artist and album name.")
            cleaned["artist"] = artist_obj
            cleaned["album_name"] = str(record.get("album") or "").strip()
            if record.get("date") or record.get("originaldate"):
                cleaned["album_release_year"] = int(str((record.get("date") or record.get("originaldate") or "").split("-")[0]))
            with transaction.atomic():
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Dry run: would create {model.__name__} with "
                            f"{self._album_identity(record)}"
                        )
                    )
                    return
                obj, created = model.objects.get_or_create(
                    artist=artist_obj,
                    album_name=cleaned["album_name"],
                    album_release_year=cleaned.get("album_release_year"),
                    defaults={
                        "album_discnumber": cleaned.get("album_discnumber", 1),
                        "album_genre": cleaned.get("album_genre", ""),
                        "album_publisher": cleaned.get("album_publisher", ""),
                    },
                )
                if created:
                    self.stdout.write(self.style.SUCCESS(f"Created {model.__name__}: {obj}"))
                else:
                    self.stdout.write(self.style.WARNING(f"Skipped existing {model.__name__}: {obj}"))
            return

        if model.__name__ == "Track":
            album_obj = self._ensure_album(record)
            if album_obj is None:
                raise CommandError("Track metadata is missing the required album and artist data.")
            cleaned["album"] = album_obj
            cleaned["track_title"] = str(cleaned.get("track_title") or record.get("title") or "").strip()
            cleaned["track_duration"] = cleaned.get("track_duration") or record.get("length") or record.get("track_duration")

        if not cleaned:
            raise CommandError(
                "No imported metadata matched the target model fields. "
                "Check the scanner output and the model definition."
            )

        if self._record_exists(model, cleaned, record):
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped existing {model.__name__} with identity "
                    f"{self._identity_for_model(model, record, cleaned)}"
                )
            )
            return

        with transaction.atomic():
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"Dry run: would create {model.__name__} with "
                        f"{list(cleaned.keys())}"
                    )
                )
                return

            obj = model.objects.create(**cleaned)
            self.stdout.write(self.style.SUCCESS(f"Created {model.__name__}: {obj}"))