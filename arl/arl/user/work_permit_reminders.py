"""One work-permit reminder at 90 days, then 60, then 30.

Days remaining = permit expiration date minus today in Django TIME_ZONE
(America/New_York in production settings). Same subtraction the
immigration audit uses.

Milestone bands (one send each, per employee and permit date):

- 90-day notice when 61–90 days remain, if that notice was not sent yet
- 60-day notice when 31–60 days remain, if that notice was not sent yet
- 30-day notice when 0–30 days remain (today counts), if not sent yet

A missed night still sends once the first time the employee is seen
inside that band. It does not email them again on later nights in the
same band. If the whole 90-day band is missed, the next run sends the
60-day notice only (not a late 90-day email, and not both). Same for a
missed 60-day band: the 30-day notice is the next one. Already-expired
permits (days remaining below 0) are not in this mailer.

Records live on WorkPermitMilestoneNotice (user + permit date +
milestone). A new permit date starts the sequence over. Delete a row
in admin to allow that milestone to send again.

Bypass: work_permit_extension_requested (extension letter on file).

When anything is actually due, one email per employer goes to each
active user in the immigration_email group for that employer. The
message lists only tonight's milestones, not everyone still inside a
window. Empty nights send nothing.

The Celery task name is work_permit_milestone_reminders. Turn it on
or off in Django Admin → Periodic Tasks (django-celery-beat). The
management command is for a dry-run list and a manual send.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.utils import timezone
from django.utils.html import escape

from arl.msg.email_utils import EMAIL_SOURCE_IN_APP, wrap_in_app_email_html
from arl.msg.models import EmailLog
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, WorkPermitMilestoneNotice

IMMIGRATION_EMAIL_GROUP = "immigration_email"
REMINDER_TEMPLATE_NAME = "Work permit milestone reminder"
TASK_NAME = "work_permit_milestone_reminders"
MILESTONES = (90, 60, 30)


def due_milestone(days_left):
    """Return 90, 60, or 30 for the band days_left is in, else None.

    91 or more, or already expired (below 0), is outside this mailer.
    """
    if days_left is None or days_left < 0 or days_left > 90:
        return None
    if days_left > 60:
        return 90
    if days_left > 30:
        return 60
    return 30


def _today(today=None):
    return today or timezone.localdate()


def _display_name(user):
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or user.username or user.email or f"User {user.pk}"


def _store_label(user):
    if user.store_id and user.store and user.store.number is not None:
        return str(user.store.number)
    return "—"


def _candidate_queryset(today):
    return (
        CustomUser.objects.filter(
            is_active=True,
            employer__isnull=False,
            work_permit_expiration_date__isnull=False,
            work_permit_extension_requested=False,
            work_permit_expiration_date__gte=today,
            work_permit_expiration_date__lte=today + timedelta(days=90),
        )
        .select_related("employer", "store")
        .order_by("employer_id", "last_name", "first_name", "id")
    )


def pending_milestones(today=None):
    """Employees who should get exactly one milestone notice tonight."""
    today = _today(today)
    users = list(_candidate_queryset(today))
    if not users:
        return []

    sent = set(
        WorkPermitMilestoneNotice.objects.filter(
            user_id__in=[user.pk for user in users],
        ).values_list("user_id", "permit_expiration_date", "milestone")
    )
    rows = []
    for user in users:
        expiry = user.work_permit_expiration_date
        days_left = (expiry - today).days
        milestone = due_milestone(days_left)
        if milestone is None:
            continue
        if (user.pk, expiry, milestone) in sent:
            continue
        rows.append(
            {
                "user_id": user.pk,
                "name": _display_name(user),
                "email": user.email or "",
                "employer_id": user.employer_id,
                "employer_name": user.employer.name,
                "employer": user.employer,
                "store": _store_label(user),
                "expiry": expiry,
                "days_left": days_left,
                "milestone": milestone,
            }
        )
    rows.sort(
        key=lambda row: (
            -row["milestone"],
            row["employer_name"].lower(),
            row["name"].lower(),
            row["user_id"],
        )
    )
    return rows


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


def _public_candidate(row):
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "email": row["email"],
        "employer_id": row["employer_id"],
        "employer_name": row["employer_name"],
        "store": row["store"],
        "expiry": row["expiry"].isoformat(),
        "days_left": row["days_left"],
        "milestone": row["milestone"],
    }


def render_reminder_html(employer_name, rows, today):
    parts = [
        (
            f"<p>Work permit reminders for <strong>{escape(employer_name)}</strong> "
            f"as of {escape(today.strftime('%B %d, %Y'))}.</p>"
        ),
        (
            "<p>Each person is listed once, for the milestone they reached. "
            "They are not listed again until the next milestone "
            "(90, then 60, then 30). Employees with an extension letter "
            "on file are not included.</p>"
        ),
    ]
    for milestone in MILESTONES:
        people = [row for row in rows if row["milestone"] == milestone]
        if not people:
            continue
        items = []
        for person in people:
            email = person["email"] or "no email"
            items.append(
                "<li>"
                f"{escape(person['name'])} — {escape(email)} — "
                f"Store {escape(person['store'])} — "
                f"expires {escape(person['expiry'].isoformat())} "
                f"({person['days_left']} days left)"
                "</li>"
            )
        parts.append(
            f"<h2>{milestone}-day reminder ({len(people)})</h2>"
            f"<ul>{''.join(items)}</ul>"
        )
    return wrap_in_app_email_html("".join(parts))


def reminder_subject(employer_name, rows):
    bits = []
    for milestone in MILESTONES:
        count = sum(1 for row in rows if row["milestone"] == milestone)
        if count:
            bits.append(f"{milestone}-day: {count}")
    return f"Work permit reminders ({', '.join(bits)}) — {employer_name}"


def _verified_sender(employer):
    tenant = TenantApiKeys.objects.filter(employer=employer).first()
    if tenant and tenant.verified_sender_email:
        return tenant.verified_sender_email
    return getattr(settings, "MAIL_DEFAULT_SENDER", None) or ""


def _group_by_employer(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["employer_id"], []).append(row)
    return grouped


def send_work_permit_milestone_reminders(today=None, dry_run=False):
    """Send tonight's milestone notices. dry_run does not email or record."""
    today = _today(today)
    Group.objects.get_or_create(name=IMMIGRATION_EMAIL_GROUP)
    rows = pending_milestones(today)
    grouped = _group_by_employer(rows)
    employers = []

    for employer_id, employer_rows in grouped.items():
        employer = employer_rows[0]["employer"]
        recipients = immigration_email_recipients(employer_id)
        recipient_emails = [user.email for user in recipients]
        summary = {
            "employer_id": employer_id,
            "employer_name": employer.name,
            "recipients": recipient_emails,
            "milestones": [row["milestone"] for row in employer_rows],
        }
        if not recipient_emails:
            summary["status"] = "skipped_no_recipients"
            employers.append(summary)
            continue
        if dry_run:
            summary["status"] = "dry_run"
            employers.append(summary)
            continue

        from arl.msg.helpers import create_master_email

        subject = reminder_subject(employer.name, employer_rows)
        html = render_reminder_html(employer.name, employer_rows, today)
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
                    "template_name": REMINDER_TEMPLATE_NAME,
                    "source": EMAIL_SOURCE_IN_APP,
                },
            )
            if ok:
                any_success = True
            else:
                failed.append(email)

        if any_success:
            WorkPermitMilestoneNotice.objects.bulk_create(
                [
                    WorkPermitMilestoneNotice(
                        user_id=row["user_id"],
                        employer_id=employer_id,
                        permit_expiration_date=row["expiry"],
                        milestone=row["milestone"],
                    )
                    for row in employer_rows
                ],
                ignore_conflicts=True,
            )
        EmailLog.objects.create(
            employer=employer,
            sender_email=sender or "no-reply@example.com",
            template_id="",
            template_name=REMINDER_TEMPLATE_NAME,
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
        "task": TASK_NAME,
        "candidates": [_public_candidate(row) for row in rows],
        "employers": employers,
    }


def format_milestone_report(result):
    """Plain-text list for the management command."""
    lines = []
    if result["dry_run"]:
        lines.append(f"Dry run for {result['today']}. No email sent.")
    else:
        lines.append(f"Work permit milestones for {result['today']}.")
    candidates = result["candidates"]
    if not candidates:
        lines.append("No milestone emails tonight.")
        return "\n".join(lines) + "\n"

    lines.append("")
    for row in candidates:
        lines.append(
            f"{row['milestone']:>2}-day reminder  |  {row['name']}  |  "
            f"{row['employer_name']}  |  expires {row['expiry']}  |  "
            f"{row['days_left']} days left  |  store {row['store']}"
        )
    lines.append("")
    for employer in result["employers"]:
        status = employer["status"]
        name = employer["employer_name"]
        who = ", ".join(employer["recipients"])
        if status == "skipped_no_recipients":
            lines.append(
                f"{name}: no one in the immigration_email group. Nothing sent."
            )
        elif status == "dry_run":
            lines.append(f"Would email {name}: {who}")
        elif status == "sent":
            lines.append(f"Emailed {name}: {who}")
        else:
            lines.append(f"Failed to email {name}: {who or '(no address)'}")
    lines.append("")
    lines.append(f"{len(candidates)} milestone(s).")
    return "\n".join(lines) + "\n"
