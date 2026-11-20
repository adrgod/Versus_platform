import json
from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from library.services.files_scanner import read_all_metadata
from library.services.importer import SUPPORTED_MODELS, import_records
from library.services.carry_over import import_and_carry_over

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Command(BaseCommand):
    help = (
        "Import Data: read local MP3 tags from an MP3 file or a folder of MP3s into the catalogue. "
        "Rows are matched on the model's unique constraint, so re-running an import "
        "never duplicates them, and a row (and its when_updated) is only written "
        "when a value actually changed."
    )

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
        with (PROJECT_ROOT / "config.json").open(encoding="utf-8") as config_file:
            return json.load(config_file)

    def handle(self, *args, **options):
        path = options["path"]
        dry_run = options["dry_run"]
        if not path.exists():
            raise CommandError(f"Path not found: {path}")

        try:
            model = apps.get_model(options["model"])
        except (LookupError, ValueError):
            raise CommandError(
                f"Model not found: {options['model']}. Use app_label.ModelName."
            )
        if model._meta.app_label != "library" or model.__name__ not in SUPPORTED_MODELS:
            raise CommandError(
                f"Unsupported model: {options['model']}. "
                f"Use one of: {', '.join('library.' + name for name in SUPPORTED_MODELS)}."
            )

        records = self._collect_records(path)

        # One transaction for the whole run: all-or-nothing, and a dry run
        # executes the real logic (so the report is accurate) then rolls back.
        with transaction.atomic():
            if model.__name__ == "Track":
                # Re-tagged files keep their old entry's details (see services.carry_over).
                stats, _ = import_and_carry_over(records, on_skip=self._warn_skipped)
            else:
                stats = import_records(records, model.__name__, on_skip=self._warn_skipped)
            if dry_run:
                transaction.set_rollback(True)

        self._report(stats, dry_run)

    def _collect_records(self, path):
        config = self.load_config()

        if path.is_dir():
            records = read_all_metadata(str(path), config)
            if isinstance(records, dict):
                records = [records]
            if not records:
                raise CommandError(f"No metadata found in {path}")
            records = [record for record in records if record.get("file_path")]
            if not records:
                raise CommandError(f"No MP3 records found in {path}")
            return records

        if path.suffix.lower() != ".mp3":
            raise CommandError(f"Not an MP3 file: {path}")

        records = read_all_metadata(str(path.parent), config)
        if isinstance(records, dict):
            records = [records]

        target = path.resolve()
        for record in records:
            record_file = record.get("file_path")
            if record_file and Path(record_file).resolve() == target:
                return [record]

        raise CommandError(
            f"No scanner metadata matched file: {path.name}. "
            "Check the folder and the scanner output."
        )

    def _warn_skipped(self, record, reason):
        self.stdout.write(
            self.style.WARNING(f"Skipped ({reason}): {record.get('file_path', '<unknown file>')}")
        )

    def _report(self, stats, dry_run):
        prefix = "Dry run, nothing saved. " if dry_run else ""
        for model_name in SUPPORTED_MODELS:
            counts = {outcome: stats[model_name, outcome] for outcome in ("created", "updated", "unchanged")}
            if any(counts.values()):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{prefix}{model_name}: {counts['created']} created, "
                        f"{counts['updated']} updated, {counts['unchanged']} unchanged"
                    )
                )
        if stats["skipped"]:
            self.stdout.write(self.style.WARNING(f"{stats['skipped']} record(s) skipped"))
