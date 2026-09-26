"""One work-permit reminder on the exact 90, 60, and 30 day marks.

Days remaining = permit expiration date minus today in Django TIME_ZONE
(America/New_York in production settings). Same subtraction the
immigration audit uses.

Send only when days remaining is exactly one of these:

- 90 → 90-day notice, if that notice was not sent yet
- 60 → 60-day notice, if that notice was not sent yet
- 30 → 30-day notice, if that notice was not sent yet

There are no windows. 89 days left is not a 90-day notice. 4 days left
is not a 30-day notice. 0 days (expires today) and already-expired
permits (days remaining below 0) are not in this mailer, and neither
is any other count.

A missed night is not backfilled. If the server is down on the day
someone has exactly 90 days left, the next run (89 days left) does
not send a late 90-day notice. The next email for that permit is the
60-day notice, and only on the day 60 days remain. Same for a missed
60-day night: nothing until the exact 30-day night.

Records live on WorkPermitMilestoneNotice (user + permit date +
milestone). A new permit date starts the sequence over, and that new
date still fires only on an exact 90, 60, or 30 day match. Delete a
row in admin to allow that milestone to send again.

Who is included
    Only a temporary SIN: the digits of sin_plain start with 9.
    A permanent SIN (anything else) is skipped even when
    work_permit_expiration_date is set and lands on exactly 90, 60,
    or 30. A missing SIN is skipped. Old permit dates left on a
    permanent-SIN row do not email.

Bypass: work_permit_extension_requested (extension letter on file),
    and an active Studies Completed event that has a document or a
    reference number. That proof ranks Compliant on the audit, so the
    old permit date is not a milestone either.

When anything is actually due, one email per employer goes to each
active user in the immigration_email group for that employer. The
message lists only tonight's exact-day milestones. Empty nights send
nothing.

The Celery task name is work_permit_milestone_reminders. Turn it on
or off in Django Admin → Periodic Tasks (django-celery-beat). The
management command is for a dry-run list and a manual send.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.utils import timezone
from django.utils.html import escape

from arl.documentflow.services_immigration import requires_work_permit
from arl.msg.email_utils import EMAIL_SOURCE_IN_APP, wrap_in_app_email_html
from arl.msg.models import EmailLog
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, WorkPermitMilestoneNotice

IMMIGRATION_EMAIL_GROUP = "immigration_email"
REMINDER_TEMPLATE_NAME = "Work permit milestone reminder"
TASK_NAME = "work_permit_milestone_reminders"
MILESTONES = (90, 60, 30)


def due_milestone(days_left):
    """Return 90, 60, or 30 only when days_left is that exact count.

    Neighbouring days are not a late notice. 89 is not 90, 4 is not 30,
    and 0 (expires today) is not 30. Already expired (below 0) is outside
    this mailer. A missed exact day is not backfilled on the next night.
    """
    if days_left in MILESTONES:
        return days_left
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


def _exact_milestone_dates(today):
    return [today + timedelta(days=days) for days in MILESTONES]


def _candidate_queryset(today):
    return (
        CustomUser.objects.filter(
            is_active=True,
            employer__isnull=False,
            work_permit_extension_requested=False,
            work_permit_expiration_date__in=_exact_milestone_dates(today),
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
        # Skips a permanent or missing SIN, and Studies Completed proof.
        if not requires_work_permit(user):
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
            "<p>Each person below has a temporary SIN and a work permit that "
            "expires in exactly 90, 60, or 30 days. They are listed once "
            "for that day. Permanent SINs, employees with an extension "
            "letter on file, and employees with Studies Completed proof "
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
