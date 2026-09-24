"""Paul — work permit 90/60/30 reminder.

How to set the role
    Django Admin → Groups. The group name is exactly ``immigration_email``.
    This command creates that group if it is missing.
    On each person who should get the email: active user, same employer as
    the employees, add the ``immigration_email`` group.
    People in other groups (HR, new_hire_data_email, and so on) are not
    copied unless they are also in ``immigration_email``.

How to mark the bypass
    Django Admin → Users → the employee.
    Check “Extension letter on file” (work_permit_extension_requested).
    Set “Extension submitted” to the date it was filed with IRCC.
    That date is not an expiry. The government letter has no expiry date.
    Save. They drop out of this digest. The HR immigration audit shows
    Extension Pending / Extension Filed and “left out of the 90/60/30 reminder”.

How to dry-run or send locally
    python manage.py work_permit_expiry_digest --dry-run
    python manage.py work_permit_expiry_digest
    python manage.py work_permit_expiry_digest --force
    python manage.py work_permit_expiry_digest --schedule

``--schedule`` adds a django-celery-beat job at 2:15am America/New_York.
Beat has to use the database scheduler:

    celery -A arl beat -S django

Or call the task directly:

    celery -A arl call work_permit_expiry_digest
    celery -A arl call work_permit_expiry_digest --kwargs '{"dry_run": true}'

What the windows mean
    Days remaining = permit expiry minus today's date in Django TIME_ZONE
    (America/New_York in production settings). Same subtraction the
    immigration audit uses.
    30: expires in 0–30 days. 60: 31–60. 90: 61–90. One bucket each.
    Already expired is not in this mailer.
    One digest per employer per day. A later run the same day does not
    send again unless you pass --force. Empty nights send nothing.
"""

import json

from django.core.management.base import BaseCommand

from arl.user.work_permit_reminders import (
    ensure_work_permit_digest_schedule,
    send_work_permit_expiry_digest,
)


class Command(BaseCommand):
    help = (
        "Email the immigration_email group a digest of work permits "
        "expiring in 90, 60, and 30 days. See the module docstring."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print who would be emailed. Do not send.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even if this employer already got today's digest.",
        )
        parser.add_argument(
            "--schedule",
            action="store_true",
            help=(
                "Create the nightly django-celery-beat entry "
                "(2:15am America/New_York) and do not send "
                "unless --dry-run or --force is also set."
            ),
        )

    def handle(self, *args, **options):
        if options["schedule"]:
            scheduled = ensure_work_permit_digest_schedule()
            self.stdout.write(json.dumps({"schedule": scheduled}, indent=2))
            if not options["dry_run"] and not options["force"]:
                return

        result = send_work_permit_expiry_digest(
            dry_run=options["dry_run"],
            force=options["force"],
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
