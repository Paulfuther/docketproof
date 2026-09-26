from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from arl.documentflow.services_immigration import (
    build_immigration_audit,
    requires_work_permit,
)
from arl.user.models import CustomUser, Employer, Store, WorkPermitMilestoneNotice
from arl.user.services import set_user_sin
from arl.user.tasks import work_permit_milestone_reminders
from arl.user.work_permit_reminders import (
    IMMIGRATION_EMAIL_GROUP,
    TASK_NAME,
    due_milestone,
    format_milestone_report,
    send_work_permit_milestone_reminders,
)

# Temporary SIN: digits start with 9. Permanent SIN: anything else.
TEMPORARY_SIN = "946454286"
PERMANENT_SIN = "130692544"
FERNET_TEST_KEY = "iNKsr5HSwIPaWnIPgLRQLSluZRH0zEzZnJ_f6WdFZBQ="
SIN_SETTINGS = dict(
    SECRET_KEY="ci-test-secret-key-not-for-production",
    FERNET_PRIMARY_KEY=FERNET_TEST_KEY,
    FERNET_OLD_KEYS=[],
    SIN_HASH_SALT="work-permit-test-salt",
)


@override_settings(**SIN_SETTINGS)
class DueMilestoneTests(TestCase):
    def test_only_exact_days_match(self):
        self.assertEqual(due_milestone(90), 90)
        self.assertEqual(due_milestone(60), 60)
        self.assertEqual(due_milestone(30), 30)
        for days_left in (None, 91, 89, 61, 59, 31, 29, 4, 1, 0, -1):
            self.assertIsNone(due_milestone(days_left), days_left)

    def test_only_a_temporary_sin_needs_a_permit(self):
        self.assertTrue(requires_work_permit(_Sin("9 46-454 286")))
        self.assertFalse(requires_work_permit(_Sin("130 692 544")))
        self.assertFalse(requires_work_permit(_Sin("")))
        self.assertFalse(requires_work_permit(_Sin(None)))


class _Sin:
    def __init__(self, sin_plain):
        self.sin_plain = sin_plain


@override_settings(
    **SIN_SETTINGS,
    MAIL_DEFAULT_SENDER="digest@example.com",
)
class WorkPermitMilestoneTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.employer = Employer.objects.create(name="Acme Fuels")
        self.other = Employer.objects.create(name="Other Co")
        self.store = Store.objects.create(
            number=12,
            employer=self.employer,
            address="1 Main",
            city="Windsor",
            province="ON",
        )
        self.group = Group.objects.create(name=IMMIGRATION_EMAIL_GROUP)
        self.recipient = self._user(
            "immigration.lead",
            "lead@example.com",
            "+15195551001",
            employer=self.employer,
        )
        self.recipient.groups.add(self.group)
        self.other_recipient = self._user(
            "other.lead",
            "other-lead@example.com",
            "+15195551002",
            employer=self.other,
        )
        self.other_recipient.groups.add(self.group)
        self.hr_only = self._user(
            "hr.only",
            "hr@example.com",
            "+15195551003",
            employer=self.employer,
        )
        self.hr_only.groups.add(Group.objects.create(name="HR"))

    def _user(self, username, email, phone, employer, **extra):
        return CustomUser.objects.create_user(
            username=username,
            email=email,
            password="pass12345",
            phone_number=phone,
            employer=employer,
            first_name=extra.pop("first_name", username.split(".")[0].title()),
            last_name=extra.pop("last_name", "Worker"),
            **extra,
        )

    def _employee(self, username, days, sin=TEMPORARY_SIN, **extra):
        user = self._user(
            username,
            f"{username}@example.com",
            extra.pop("phone"),
            employer=extra.pop("employer", self.employer),
            work_permit_expiration_date=self.today + timedelta(days=days),
            store=extra.pop("store", self.store),
            **extra,
        )
        if sin:
            set_user_sin(user, sin, validate_luhn=False, save=True)
        return user

    def _milestones(self, today=None):
        result = send_work_permit_milestone_reminders(
            today=today or self.today, dry_run=True
        )
        return [(row["name"], row["milestone"]) for row in result["candidates"]]

    def test_only_exact_milestone_days_are_listed(self):
        self._employee(
            "ninety", 90, phone="+15195552090", first_name="Nan", last_name="Ninety"
        )
        self._employee(
            "almost-ninety",
            89,
            phone="+15195552089",
            first_name="Lana",
            last_name="Almost",
        )
        self._employee(
            "sixty", 60, phone="+15195552060", first_name="Sam", last_name="Sixty"
        )
        self._employee(
            "thirty", 30, phone="+15195552030", first_name="Tara", last_name="Thirty"
        )
        self._employee(
            "four-left", 4, phone="+15195552004", first_name="Finn", last_name="Four"
        )
        self._employee(
            "today", 0, phone="+15195552000", first_name="Tim", last_name="Today"
        )
        self._employee("too-far", 91, phone="+15195552091")
        self._employee("expired", -1, phone="+15195552099")

        self.assertEqual(
            self._milestones(),
            [
                ("Nan Ninety", 90),
                ("Sam Sixty", 60),
                ("Tara Thirty", 30),
            ],
        )

    def test_missed_exact_day_is_not_backfilled(self):
        self._employee(
            "missed-ninety",
            89,
            phone="+15195553089",
            first_name="Mia",
            last_name="Missed",
        )
        self._employee(
            "between", 45, phone="+15195553045", first_name="Bea", last_name="Between"
        )
        self._employee(
            "four-left", 4, phone="+15195553004", first_name="Finn", last_name="Four"
        )
        result = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(WorkPermitMilestoneNotice.objects.count(), 0)

    def test_extension_letter_is_excluded(self):
        self._employee(
            "bypassed",
            90,
            phone="+15195553001",
            work_permit_extension_requested=True,
            work_permit_extension_date=self.today - timedelta(days=10),
            first_name="Bo",
            last_name="Pass",
        )
        kept = self._employee(
            "kept", 30, phone="+15195553003", first_name="Kay", last_name="Kept"
        )
        self.assertEqual(self._milestones(), [("Kay Kept", 30)])
        self.assertNotIn("Bo Pass", [name for name, _milestone in self._milestones()])
        self.assertEqual(kept.work_permit_extension_requested, False)

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_one_notice_per_milestone_then_the_next(self, mock_send):
        person = self._employee(
            "ada", 90, phone="+15195554001", first_name="Ada", last_name="Due"
        )
        first = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(first["candidates"][0]["milestone"], 90)
        self.assertEqual(
            WorkPermitMilestoneNotice.objects.filter(user=person, milestone=90).count(),
            1,
        )

        again = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(again["candidates"], [])

        next_night = send_work_permit_milestone_reminders(
            today=self.today + timedelta(days=1)
        )
        self.assertEqual(next_night["candidates"], [])
        self.assertEqual(mock_send.call_count, 1)

        day_60 = self.today + timedelta(days=30)
        second = send_work_permit_milestone_reminders(today=day_60)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(second["candidates"][0]["milestone"], 60)
        self.assertIn("60-day reminder", mock_send.call_args.kwargs["html_content"])
        self.assertIn("Ada Due", mock_send.call_args.kwargs["html_content"])

        quiet = send_work_permit_milestone_reminders(today=day_60 + timedelta(days=5))
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(quiet["candidates"], [])

        day_30 = self.today + timedelta(days=60)
        third = send_work_permit_milestone_reminders(today=day_30)
        self.assertEqual(third["candidates"][0]["milestone"], 30)
        self.assertEqual(mock_send.call_count, 3)

        later = send_work_permit_milestone_reminders(today=day_30 + timedelta(days=10))
        self.assertEqual(later["candidates"], [])
        self.assertEqual(mock_send.call_count, 3)

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_new_permit_date_starts_over_only_on_an_exact_day(self, mock_send):
        person = self._employee(
            "ada", 90, phone="+15195554011", first_name="Ada", last_name="Due"
        )
        send_work_permit_milestone_reminders(today=self.today)
        person.work_permit_expiration_date = self.today + timedelta(days=88)
        person.save(update_fields=["work_permit_expiration_date"])
        off_day = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(off_day["candidates"], [])
        self.assertEqual(mock_send.call_count, 1)

        person.work_permit_expiration_date = self.today + timedelta(days=60)
        person.save(update_fields=["work_permit_expiration_date"])
        again = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(again["candidates"][0]["milestone"], 60)
        self.assertEqual(again["candidates"][0]["days_left"], 60)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(
            WorkPermitMilestoneNotice.objects.filter(user=person).count(), 2
        )

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_emails_only_immigration_email_for_that_employer(self, mock_send):
        self._employee(
            "due", 90, phone="+15195554021", first_name="Ada", last_name="Due"
        )
        self._employee(
            "other-due",
            60,
            phone="+15195554022",
            employer=self.other,
            store=None,
            first_name="Bea",
            last_name="Other",
        )
        inactive = self._user(
            "inactive.lead",
            "inactive-lead@example.com",
            "+15195554023",
            employer=self.employer,
        )
        inactive.groups.add(self.group)
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        result = send_work_permit_milestone_reminders(today=self.today)
        sent_to = sorted(call.kwargs["to_email"] for call in mock_send.call_args_list)
        self.assertEqual(sent_to, ["lead@example.com", "other-lead@example.com"])
        self.assertNotIn("hr@example.com", sent_to)

        bodies = {
            call.kwargs["to_email"]: call.kwargs["html_content"]
            for call in mock_send.call_args_list
        }
        self.assertIn("Ada Due", bodies["lead@example.com"])
        self.assertIn("90-day reminder", bodies["lead@example.com"])
        self.assertNotIn("Bea Other", bodies["lead@example.com"])
        self.assertIn("Bea Other", bodies["other-lead@example.com"])
        self.assertIn("60-day reminder", bodies["other-lead@example.com"])
        self.assertEqual({row["status"] for row in result["employers"]}, {"sent"})

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_dry_run_lists_candidates_and_does_not_send(self, mock_send):
        self._employee(
            "due", 60, phone="+15195556001", first_name="Cara", last_name="Soon"
        )
        result = send_work_permit_milestone_reminders(today=self.today, dry_run=True)
        report = format_milestone_report(result)
        self.assertFalse(mock_send.called)
        self.assertEqual(WorkPermitMilestoneNotice.objects.count(), 0)
        self.assertIn("Dry run", report)
        self.assertIn("No email sent", report)
        self.assertIn("Cara Soon", report)
        self.assertIn("60-day reminder", report)
        self.assertIn("Acme Fuels", report)
        self.assertIn(result["candidates"][0]["expiry"], report)
        self.assertIn("Would email Acme Fuels: lead@example.com", report)
        self.assertNotIn("hr@example.com", report)

        out = StringIO()
        call_command("work_permit_expiry_digest", "--dry-run", stdout=out)
        text = out.getvalue()
        self.assertIn("Cara Soon", text)
        self.assertIn("60-day reminder", text)
        self.assertIn("Acme Fuels", text)
        self.assertIn("No email sent", text)
        self.assertEqual(WorkPermitMilestoneNotice.objects.count(), 0)
        self.assertFalse(mock_send.called)

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_no_role_does_not_mark_the_milestone_sent(self, mock_send):
        self._employee(
            "due", 30, phone="+15195556011", first_name="Dee", last_name="Permit"
        )
        self.recipient.groups.clear()
        skipped = send_work_permit_milestone_reminders(today=self.today)
        self.assertFalse(mock_send.called)
        self.assertEqual(skipped["employers"][0]["status"], "skipped_no_recipients")
        self.assertEqual(WorkPermitMilestoneNotice.objects.count(), 0)
        self.recipient.groups.add(self.group)
        sent = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(sent["employers"][0]["status"], "sent")
        self.assertEqual(mock_send.call_count, 1)

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_task_is_the_scheduler_entry(self, mock_send):
        self._employee(
            "due", 30, phone="+15195557001", first_name="Dee", last_name="Permit"
        )
        self.assertEqual(work_permit_milestone_reminders.name, TASK_NAME)
        result = work_permit_milestone_reminders()
        self.assertEqual(result["task"], TASK_NAME)
        self.assertEqual(result["employers"][0]["status"], "sent")
        self.assertEqual(result["candidates"][0]["milestone"], 30)
        self.assertIn("Dee Permit", mock_send.call_args.kwargs["html_content"])
        mock_send.reset_mock()
        second = work_permit_milestone_reminders()
        self.assertEqual(second["candidates"], [])
        self.assertFalse(mock_send.called)


@override_settings(**SIN_SETTINGS)
class PermanentSinAndAuditTests(TestCase):
    """Paul's live-data examples: permanent SIN is not a permit reminder."""

    def setUp(self):
        self.today = timezone.localdate()
        self.employer = Employer.objects.create(name="Audit Fuels")

    def _person(self, username, phone, sin, permit_days, sin_days=None, **extra):
        user = CustomUser.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="pass12345",
            phone_number=phone,
            employer=self.employer,
            first_name=extra.pop("first_name", username.split(".")[0].title()),
            last_name=extra.pop("last_name", "Worker"),
            **extra,
        )
        if sin:
            set_user_sin(user, sin, validate_luhn=False, save=True)
        updates = []
        if permit_days is not None:
            user.work_permit_expiration_date = self.today + timedelta(days=permit_days)
            updates.append("work_permit_expiration_date")
        if sin_days is not None:
            user.sin_expiration_date = self.today + timedelta(days=sin_days)
            updates.append("sin_expiration_date")
        if updates:
            user.save(update_fields=updates)
        return user

    def _audit_row(self, last_name):
        audit = build_immigration_audit(self.employer)
        for row in audit["immigration_rows"]:
            if row["employee"].last_name == last_name:
                return row
        self.fail(f"No audit row for {last_name}")

    def test_permanent_sin_on_an_exact_day_is_not_mailed_and_stays_compliant(self):
        self._person(
            "pat.permanent",
            "+15195558030",
            PERMANENT_SIN,
            30,
            first_name="Pat",
            last_name="Permanent",
        )
        self._person(
            "old.date",
            "+15195558004",
            PERMANENT_SIN,
            -20,
            first_name="Omar",
            last_name="Leftover",
        )
        self._person(
            "no.sin",
            "+15195558031",
            None,
            30,
            first_name="Ned",
            last_name="Missing",
        )
        watched = self._person(
            "terry.watch",
            "+15195558082",
            TEMPORARY_SIN,
            82,
            sin_days=400,
            first_name="Terry",
            last_name="Watch",
        )
        self._person(
            "tina.thirty",
            "+15195558032",
            "923456789",
            30,
            sin_days=400,
            first_name="Tina",
            last_name="Thirty",
        )
        sin_watch = self._person(
            "sam.sinwatch",
            "+15195558083",
            "934567890",
            200,
            sin_days=82,
            first_name="Sam",
            last_name="Sinwatch",
        )

        result = send_work_permit_milestone_reminders(today=self.today, dry_run=True)
        listed = [row["name"] for row in result["candidates"]]
        self.assertEqual(listed, ["Tina Thirty"])
        self.assertEqual(result["candidates"][0]["days_left"], 30)
        self.assertEqual(result["candidates"][0]["milestone"], 30)

        permanent = self._audit_row("Permanent")
        self.assertEqual(permanent["permit_info"]["code"], "not_required")
        self.assertEqual(permanent["permit_info"]["label"], "Permit Not Required")
        self.assertEqual(permanent["overall_status"]["code"], "compliant")
        self.assertEqual(permanent["overall_status"]["label"], "Compliant")
        self.assertFalse(permanent["is_flagged"])
        self.assertEqual(permanent["permit_days"], 30)

        leftover = self._audit_row("Leftover")
        self.assertEqual(leftover["permit_info"]["code"], "not_required")
        self.assertEqual(leftover["overall_status"]["code"], "compliant")
        self.assertEqual(leftover["permit_days"], -20)
        self.assertFalse(leftover["is_flagged"])

        watch = self._audit_row("Watch")
        self.assertEqual(watch["sin_info"]["is_temporary"], True)
        self.assertEqual(watch["permit_info"]["code"], "expiring_soon")
        self.assertEqual(watch["permit_info"]["pill_class"], "warning")
        self.assertEqual(watch["overall_status"]["code"], "expiring_soon")
        self.assertEqual(watch["overall_status"]["pill_class"], "warning")
        self.assertEqual(watch["permit_days"], 82)
        self.assertTrue(watch["is_flagged"])
        self.assertEqual(
            watched.work_permit_expiration_date, self.today + timedelta(days=82)
        )

        sin_row = self._audit_row("Sinwatch")
        self.assertEqual(sin_row["sin_info"]["code"], "expiring_soon")
        self.assertEqual(
            sin_row["overall_status"]["code"], "compliant_sin_update_needed"
        )
        self.assertEqual(sin_row["overall_status"]["pill_class"], "warning")
        self.assertEqual(sin_watch.sin_expiration_date, self.today + timedelta(days=82))

        names = [
            row["employee"].last_name
            for row in build_immigration_audit(self.employer)["immigration_rows"]
        ]
        self.assertLess(names.index("Watch"), names.index("Permanent"))
        self.assertLess(names.index("Sinwatch"), names.index("Permanent"))
        self.assertLess(names.index("Watch"), names.index("Leftover"))

    def test_permanent_sin_extension_letter_is_the_only_override(self):
        self._person(
            "pat.extension",
            "+15195558130",
            PERMANENT_SIN,
            30,
            first_name="Pat",
            last_name="Extension",
            work_permit_extension_requested=True,
            work_permit_extension_date=self.today - timedelta(days=3),
        )
        row = self._audit_row("Extension")
        self.assertEqual(row["permit_info"]["code"], "extension_pending")
        self.assertEqual(row["overall_status"]["code"], "extension_pending")
        self.assertNotEqual(row["overall_status"]["code"], "expiring_soon")
        self.assertNotEqual(row["overall_status"]["code"], "urgent")
