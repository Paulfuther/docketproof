from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from arl.user.models import CustomUser, Employer, Store, WorkPermitMilestoneNotice
from arl.user.tasks import work_permit_milestone_reminders
from arl.user.work_permit_reminders import (
    IMMIGRATION_EMAIL_GROUP,
    TASK_NAME,
    due_milestone,
    format_milestone_report,
    send_work_permit_milestone_reminders,
)


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class DueMilestoneTests(TestCase):
    def test_bands_send_once_each_and_ignore_outside(self):
        self.assertIsNone(due_milestone(None))
        self.assertIsNone(due_milestone(91))
        self.assertIsNone(due_milestone(-1))
        self.assertEqual(due_milestone(90), 90)
        self.assertEqual(due_milestone(61), 90)
        self.assertEqual(due_milestone(60), 60)
        self.assertEqual(due_milestone(31), 60)
        self.assertEqual(due_milestone(30), 30)
        self.assertEqual(due_milestone(0), 30)


@override_settings(
    SECRET_KEY="ci-test-secret-key-not-for-production",
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

    def _employee(self, username, days, **extra):
        return self._user(
            username,
            f"{username}@example.com",
            extra.pop("phone"),
            employer=extra.pop("employer", self.employer),
            work_permit_expiration_date=self.today + timedelta(days=days),
            store=extra.pop("store", self.store),
            **extra,
        )

    def _milestones(self, today=None):
        result = send_work_permit_milestone_reminders(
            today=today or self.today, dry_run=True
        )
        return [(row["name"], row["milestone"]) for row in result["candidates"]]

    def test_exact_and_late_inside_the_band_only(self):
        self._employee(
            "ninety", 90, phone="+15195552090", first_name="Nan", last_name="Ninety"
        )
        self._employee(
            "late-ninety", 87, phone="+15195552087", first_name="Lana", last_name="Late"
        )
        self._employee(
            "sixty", 60, phone="+15195552060", first_name="Sam", last_name="Sixty"
        )
        self._employee(
            "thirty", 30, phone="+15195552030", first_name="Tara", last_name="Thirty"
        )
        self._employee(
            "today", 0, phone="+15195552000", first_name="Tim", last_name="Today"
        )
        self._employee("too-far", 91, phone="+15195552091")
        self._employee("expired", -1, phone="+15195552099")

        self.assertEqual(
            self._milestones(),
            [
                ("Lana Late", 90),
                ("Nan Ninety", 90),
                ("Sam Sixty", 60),
                ("Tara Thirty", 30),
                ("Tim Today", 30),
            ],
        )

    def test_missed_90_band_sends_60_only(self):
        self._employee(
            "first-seen", 45, phone="+15195553045", first_name="Fay", last_name="First"
        )
        self.assertEqual(self._milestones(), [("Fay First", 60)])

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
            "kept", 12, phone="+15195553003", first_name="Kay", last_name="Kept"
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
    def test_new_permit_date_starts_the_sequence_over(self, mock_send):
        person = self._employee(
            "ada", 90, phone="+15195554011", first_name="Ada", last_name="Due"
        )
        send_work_permit_milestone_reminders(today=self.today)
        person.work_permit_expiration_date = self.today + timedelta(days=88)
        person.save(update_fields=["work_permit_expiration_date"])
        again = send_work_permit_milestone_reminders(today=self.today)
        self.assertEqual(again["candidates"][0]["milestone"], 90)
        self.assertEqual(again["candidates"][0]["days_left"], 88)
        self.assertEqual(mock_send.call_count, 2)

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
