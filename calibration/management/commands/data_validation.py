# calibration/management/commands/data_validation.py
import sys
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from calibration.util.forcing_validation import data_validation_job


class Command(BaseCommand):
    help = 'Triggers forcing validation manually.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--gages', '--gage',
            nargs='*',
            dest='gages',
            help='Optional list of gage IDs to validate (space-separated)'
        )
        parser.add_argument(
            '--forcing_dir',
            type=str,
            help='Optional forcing directory (overrides default)'
        )
        parser.add_argument(
            '--start',
            type=int,
            help='Start index of gage slice (0-based)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Number of gages to process from start index'
        )

    def handle(self, *args, **options):
        def print_flush(msg, file=sys.stdout):
            now = datetime.now(timezone.utc).isoformat()
            print(f"{now} - {msg}", file=file, flush=True)

        print_flush("Starting data_validation_job...")

        try:
            data_validation_job(
                gages=options.get('gages'),
                forcing_dir=options.get('forcing_dir'),
                start=options.get('start'),
                limit=options.get('limit')
            )
            print_flush("Validation completed successfully.")
        except Exception as e:
            print_flush(f"Validation failed: {e}", file=sys.stderr)
