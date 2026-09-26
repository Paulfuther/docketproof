"""Paul — work permit milestone reminders.

Dry-run (prints the list, does not send)
    python manage.py work_permit_expiry_digest --dry-run

Send whatever that list shows
    python manage.py work_permit_expiry_digest

The list is one line per employee who would get a notice tonight:
milestone (90, 60, or 30), name, employer, permit expiry, days left.

Rule
    One email at 90 days (or the first night they are seen with
    61–90 days left), then nothing until 60 (31–60 days left),
    then nothing until 30 (0–30 days left). A missed night does not
    send every following night. Already expired is not listed.
    Extension letter on file (work_permit_extension_requested) is skipped.

Turn the nightly job on or off
    Django Admin → Periodic Tasks → Add (django-celery-beat).
    Task name: work_permit_milestone_reminders
    Crontab example: minute 15, hour 2, every day, timezone America/New_York.
    Uncheck Enabled to stop it. Check Enabled to start it again.
    Beat has to be running with the database scheduler:
        celery -A arl beat -S django

Who receives a real send
    Active users in the Django group immigration_email, same employer
    as the employee. The dry-run prints those addresses as "Would email".

Send one milestone again
    Django Admin → Work permit milestone notices → delete that row.
    The next run will include it.
"""

from django.core.management.base import BaseCommand

from arl.user.work_permit_reminders import (
    format_milestone_report,
    send_work_permit_milestone_reminders,
)


class Command(BaseCommand):
    help = (
        "List or send one-time 90, 60, and 30 day work-permit reminders. "
        "Use --dry-run to print tonight's list without sending."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print who would get a 90, 60, or 30 day notice tonight. Do not send.",
        )

    def handle(self, *args, **options):
        result = send_work_permit_milestone_reminders(dry_run=options["dry_run"])
        self.stdout.write(format_milestone_report(result))
