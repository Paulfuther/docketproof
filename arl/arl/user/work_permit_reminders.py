"""Nightly digest of work permits coming due in 90, 60, and 30 days.

Window rule (same day-count the immigration audit uses: expiry minus today):

- 30-day bucket: expires in 0 to 30 days (today counts)
- 60-day bucket: expires in 31 to 60 days
- 90-day bucket: expires in 61 to 90 days

Buckets do not overlap. A person sits in one bucket and moves
90 -> 60 -> 30 as the date gets closer. Already-expired permits
(days remaining below 0) are not in this mailer.

Bypass: ``work_permit_extension_requested`` (extension letter on file).
That government letter has no expiry. Those employees stay legal and
are left out, including when the permit date is inside a window or
already past.

Delivery: one digest per employer per local calendar day, emailed to
each active user in the ``immigration_email`` Django group for that
employer. A successful send is recorded on EmailLog so a second run
the same day does not send again. ``force=True`` sends anyway.
An empty digest sends nothing.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.utils import timezone
from django.utils.html import escape

from arl.msg.email_utils import EMAIL_SOURCE_IN_APP, wrap_in_app_email_html
from arl.msg.models import EmailLog
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser

IMMIGRATION_EMAIL_GROUP = "immigration_email"
DIGEST_TEMPLATE_NAME = "Work permit expiry digest"

# (bucket label, inclusive min days, inclusive max days)
PERMIT_WINDOWS = (
    (30, 0, 30),
    (60, 31, 60),
    (90, 61, 90),
)
BUCKET_ORDER = (30, 60, 90)
BUCKET_HEADINGS = {
    30: "30 days — expires within 30 days (including today)",
    60: "60 days — expires in 31 to 60 days",
    90: "90 days — expires in 61 to 90 days",
}


def permit_bucket(days_left):
    """Return 30, 60, 90, or None when the permit is outside this mailer."""
    if days_left is None:
        return None
    for bucket, low, high in PERMIT_WINDOWS:
        if low <= days_left <= high:
            return bucket
    return None


def _today(today=None):
    return today or timezone.localdate()


def _display_name(user):
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or user.username or user.email or f"User {user.pk}"


def _store_label(user):
    if user.store_id and user.store and user.store.number is not None:
        return str(user.store.number)
    return "—"


def employee_row(user, today):
    expiry = user.work_permit_expiration_date
    days_left = (expiry - today).days if expiry else None
    return {
        "id": user.pk,
        "name": _display_name(user),
        "email": user.email or "",
        "store": _store_label(user),
        "expiry": expiry.isoformat() if expiry else "",
        "days_left": days_left,
        "bucket": permit_bucket(days_left),
    }


def due_employees(today=None):
    """Active employees with a permit date inside 0–90 days, not bypassed."""
    today = _today(today)
    latest = today + timedelta(days=90)
    return (
        CustomUser.objects.filter(
            is_active=True,
            employer__isnull=False,
            work_permit_expiration_date__isnull=False,
            work_permit_extension_requested=False,
            work_permit_expiration_date__gte=today,
            work_permit_expiration_date__lte=latest,
        )
        .select_related("employer", "store")
        .order_by(
            "employer_id",
            "work_permit_expiration_date",
            "last_name",
            "first_name",
            "id",
        )
    )


def bucket_employees(today=None):
    """Map employer id -> {30/60/90: [row, ...], 'employer': Employer}."""
    today = _today(today)
    grouped = {}
    for user in due_employees(today):
        row = employee_row(user, today)
        if row["bucket"] is None:
            continue
        entry = grouped.setdefault(
            user.employer_id,
            {
                "employer": user.employer,
                "buckets": {30: [], 60: [], 90: []},
            },
        )
        entry["buckets"][row["bucket"]].append(row)
    return grouped


def immigration_email_recipients(employer_id):
    Group.objects.get_or_create(name=IMMIGRATION_EMAIL_GROUP)
    return list(
        CustomUser.objects.filter(
            is_active=True,
            employer_id=employer_id,
            groups__name=IMMIGRATION_EMAIL_GROUP,
        )
        .exclude(email__isnull=True)
        .exclude(email="")
        .distinct()
        .order_by("email", "id")
    )


def render_digest_html(employer_name, buckets, today):
    parts = [
        (
            f"<p>Work permits coming due for <strong>{escape(employer_name)}</strong> "
            f"as of {escape(today.strftime('%B %d, %Y'))}.</p>"
        ),
        (
            "<p>Most urgent first. Employees with an extension letter on file "
            "(still legal to work; the letter has no expiry) are not listed.</p>"
        ),
    ]
    for bucket in BUCKET_ORDER:
        people = buckets.get(bucket) or []
        if not people:
            continue
        items = []
        for person in people:
            email = person["email"] or "no email"
            items.append(
                "<li>"
                f"{escape(person['name'])} — {escape(email)} — "
                f"Store {escape(person['store'])} — "
                f"expires {escape(person['expiry'])} "
                f"({person['days_left']} days)"
                "</li>"
            )
        heading = BUCKET_HEADINGS[bucket]
        parts.append(
            f"<h2>{escape(heading)} ({len(people)})</h2><ul>{''.join(items)}</ul>"
        )
    return wrap_in_app_email_html("".join(parts))


def digest_subject(employer_name, buckets):
    counts = ", ".join(
        f"{bucket}: {len(buckets.get(bucket) or [])}" for bucket in BUCKET_ORDER
    )
    return f"Work permits coming due ({counts}) — {employer_name}"


def _local_day_bounds(today):
    start = datetime.combine(today, time.min)
    if timezone.is_aware(timezone.now()):
        start = timezone.make_aware(start, timezone.get_current_timezone())
    return start, start + timedelta(days=1)


def already_sent_today(employer_id, today):
    start, end = _local_day_bounds(today)
    return EmailLog.objects.filter(
        employer_id=employer_id,
        template_name=DIGEST_TEMPLATE_NAME,
        status="SUCCESS",
        sent_at__gte=start,
        sent_at__lt=end,
    ).exists()


def _verified_sender(employer):
    tenant = TenantApiKeys.objects.filter(employer=employer).first()
    if tenant and tenant.verified_sender_email:
        return tenant.verified_sender_email
    return getattr(settings, "MAIL_DEFAULT_SENDER", None) or ""


def _counts(buckets):
    return {str(bucket): len(buckets.get(bucket) or []) for bucket in BUCKET_ORDER}


def _flat_employees(buckets):
    rows = []
    for bucket in BUCKET_ORDER:
        rows.extend(buckets.get(bucket) or [])
    return rows


def send_work_permit_expiry_digest(today=None, dry_run=False, force=False):
    """Build and send one digest per employer. Returns a JSON-safe summary."""
    today = _today(today)
    Group.objects.get_or_create(name=IMMIGRATION_EMAIL_GROUP)
    grouped = bucket_employees(today)
    employers = []

    for employer_id, entry in grouped.items():
        employer = entry["employer"]
        buckets = entry["buckets"]
        recipients = immigration_email_recipients(employer_id)
        recipient_emails = [user.email for user in recipients]
        summary = {
            "employer_id": employer_id,
            "employer_name": employer.name,
            "counts": _counts(buckets),
            "employees": _flat_employees(buckets),
            "recipients": recipient_emails,
        }

        if already_sent_today(employer_id, today) and not force:
            summary["status"] = "skipped_already_sent"
            employers.append(summary)
            continue

        if not recipient_emails:
            summary["status"] = "skipped_no_recipients"
            employers.append(summary)
            continue

        subject = digest_subject(employer.name, buckets)
        html = render_digest_html(employer.name, buckets, today)
        summary["subject"] = subject

        if dry_run:
            summary["status"] = "dry_run"
            employers.append(summary)
            continue

        from arl.msg.helpers import create_master_email

        sender = _verified_sender(employer)
        any_success = False
        failed = []
        for email in recipient_emails:
            ok = create_master_email(
                to_email=email,
                sendgrid_id=None,
                template_data={"subject": subject},
                verified_sender=sender or None,
                html_content=html,
                custom_args={
                    "subject": subject,
                    "template_name": DIGEST_TEMPLATE_NAME,
                    "source": EMAIL_SOURCE_IN_APP,
                },
            )
            if ok:
                any_success = True
            else:
                failed.append(email)

        EmailLog.objects.create(
            employer=employer,
            sender_email=sender or "no-reply@example.com",
            template_id="",
            template_name=DIGEST_TEMPLATE_NAME,
            source=EMAIL_SOURCE_IN_APP,
            subject=subject[:255],
            status="SUCCESS" if any_success else "FAILED",
            error_message=", ".join(failed) if failed else "",
        )
        summary["status"] = "sent" if any_success else "failed"
        summary["failed_recipients"] = failed
        employers.append(summary)

    return {
        "today": today.isoformat(),
        "dry_run": bool(dry_run),
        "force": bool(force),
        "employers": employers,
    }


def ensure_work_permit_digest_schedule():
    """Create the django-celery-beat crontab for 2:15am America/New_York.

    Beat must be started with the database scheduler
    (``celery -A arl beat -S django``). This does not send email.
    """
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    schedule, _created_schedule = CrontabSchedule.objects.get_or_create(
        minute="15",
        hour="2",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="America/New_York",
    )
    task, created = PeriodicTask.objects.update_or_create(
        name="Work permit expiry digest (90/60/30)",
        defaults={
            "crontab": schedule,
            "task": "work_permit_expiry_digest",
            "enabled": True,
            "description": (
                "Nightly digest of work permits expiring in the 90, 60, "
                "and 30 day windows. Emails active users in the "
                "immigration_email group for each employer."
            ),
        },
    )
    return {
        "created": created,
        "name": task.name,
        "task": task.task,
        "enabled": task.enabled,
        "crontab": "15 2 * * * America/New_York",
    }
