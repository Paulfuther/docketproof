from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone

from arl.msg.models import EmailLog
from arl.user.models import CustomUser, Employer, Store
from arl.user.tasks import work_permit_expiry_digest
from arl.user.work_permit_reminders import (
    DIGEST_TEMPLATE_NAME,
    IMMIGRATION_EMAIL_GROUP,
    bucket_employees,
    ensure_work_permit_digest_schedule,
    permit_bucket,
    send_work_permit_expiry_digest,
)


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class WorkPermitWindowTests(TestCase):
    def test_buckets_are_mutually_exclusive_windows(self):
        self.assertIsNone(permit_bucket(None))
        self.assertIsNone(permit_bucket(-1))
        self.assertIsNone(permit_bucket(91))
        self.assertEqual(permit_bucket(0), 30)
        self.assertEqual(permit_bucket(30), 30)
        self.assertEqual(permit_bucket(31), 60)
        self.assertEqual(permit_bucket(60), 60)
        self.assertEqual(permit_bucket(61), 90)
        self.assertEqual(permit_bucket(90), 90)


@override_settings(
    SECRET_KEY="ci-test-secret-key-not-for-production",
    MAIL_DEFAULT_SENDER="digest@example.com",
)
class WorkPermitDigestTests(TestCase):
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

    def test_included_at_90_60_30_and_edges(self):
        in_90 = self._employee("ninety", 90, phone="+15195552090")
        in_61 = self._employee("sixtyone", 61, phone="+15195552061")
        in_60 = self._employee("sixty", 60, phone="+15195552060")
        in_31 = self._employee("thirtyone", 31, phone="+15195552031")
        in_30 = self._employee("thirty", 30, phone="+15195552030")
        in_0 = self._employee("today", 0, phone="+15195552000")
        self._employee("too-far", 91, phone="+15195552091")
        self._employee("expired", -1, phone="+15195552099")
        self._employee(
            "inactive",
            10,
            phone="+15195552010",
            is_active=False,
        )
        self._user(
            "no-date",
            "nodate@example.com",
            "+15195552011",
            employer=self.employer,
        )

        grouped = bucket_employees(self.today)
        buckets = grouped[self.employer.id]["buckets"]
        self.assertEqual(
            [row["id"] for row in buckets[90]],
            [in_61.id, in_90.id],
        )
        self.assertEqual(
            [row["id"] for row in buckets[60]],
            [in_31.id, in_60.id],
        )
        self.assertEqual(
            [row["id"] for row in buckets[30]],
            [in_0.id, in_30.id],
        )
        self.assertNotIn(self.other.id, grouped)

    def test_extension_letter_is_excluded_even_inside_the_window(self):
        self._employee(
            "bypassed",
            12,
            phone="+15195553001",
            work_permit_extension_requested=True,
            work_permit_extension_date=self.today - timedelta(days=30),
        )
        self._employee(
            "bypassed-expired",
            -20,
            phone="+15195553002",
            work_permit_extension_requested=True,
            work_permit_extension_date=self.today - timedelta(days=40),
        )
        kept = self._employee("kept", 12, phone="+15195553003")

        grouped = bucket_employees(self.today)
        ids = [
            row["id"]
            for rows in grouped[self.employer.id]["buckets"].values()
            for row in rows
        ]
        self.assertEqual(ids, [kept.id])

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_emails_only_immigration_email_role_for_that_employer(self, mock_send):
        self._employee(
            "due", 15, phone="+15195554001", first_name="Ada", last_name="Due"
        )
        self._employee(
            "other-due",
            45,
            phone="+15195554002",
            employer=self.other,
            store=None,
            first_name="Bea",
            last_name="Other",
        )
        inactive = self._user(
            "inactive.lead",
            "inactive-lead@example.com",
            "+15195554003",
            employer=self.employer,
        )
        inactive.groups.add(self.group)
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        result = send_work_permit_expiry_digest(today=self.today)
        sent_to = sorted(call.kwargs["to_email"] for call in mock_send.call_args_list)
        self.assertEqual(sent_to, ["lead@example.com", "other-lead@example.com"])
        self.assertNotIn("hr@example.com", sent_to)
        self.assertNotIn("inactive-lead@example.com", sent_to)

        bodies = {
            call.kwargs["to_email"]: call.kwargs["html_content"]
            for call in mock_send.call_args_list
        }
        self.assertIn("Ada Due", bodies["lead@example.com"])
        self.assertIn("Store 12", bodies["lead@example.com"])
        self.assertNotIn("Bea Other", bodies["lead@example.com"])
        self.assertIn("Bea Other", bodies["other-lead@example.com"])
        self.assertNotIn("Ada Due", bodies["other-lead@example.com"])
        self.assertEqual(
            {row["status"] for row in result["employers"]},
            {"sent"},
        )
        self.assertEqual(
            EmailLog.objects.filter(template_name=DIGEST_TEMPLATE_NAME).count(),
            2,
        )

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_second_run_same_day_does_not_send_again(self, mock_send):
        self._employee("due", 20, phone="+15195555001")
        send_work_permit_expiry_digest(today=self.today)
        self.assertEqual(mock_send.call_count, 1)
        again = send_work_permit_expiry_digest(today=self.today)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(again["employers"][0]["status"], "skipped_already_sent")

        forced = send_work_permit_expiry_digest(today=self.today, force=True)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(forced["employers"][0]["status"], "sent")

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_dry_run_and_empty_and_no_role_do_not_send(self, mock_send):
        self._employee(
            "due", 40, phone="+15195556001", first_name="Cara", last_name="Soon"
        )
        dry = send_work_permit_expiry_digest(today=self.today, dry_run=True)
        self.assertFalse(mock_send.called)
        self.assertEqual(dry["employers"][0]["status"], "dry_run")
        self.assertEqual(dry["employers"][0]["employees"][0]["name"], "Cara Soon")
        self.assertEqual(EmailLog.objects.count(), 0)

        self.recipient.groups.clear()
        skipped = send_work_permit_expiry_digest(today=self.today)
        self.assertFalse(mock_send.called)
        self.assertEqual(skipped["employers"][0]["status"], "skipped_no_recipients")

        CustomUser.objects.filter(work_permit_expiration_date__isnull=False).update(
            work_permit_extension_requested=True
        )
        empty = send_work_permit_expiry_digest(today=self.today)
        self.assertEqual(empty["employers"], [])
        self.assertFalse(mock_send.called)

    @patch("arl.msg.helpers.create_master_email", return_value=True)
    def test_task_smoke(self, mock_send):
        self._employee(
            "due", 30, phone="+15195557001", first_name="Dee", last_name="Permit"
        )
        self.assertEqual(work_permit_expiry_digest.name, "work_permit_expiry_digest")
        result = work_permit_expiry_digest(dry_run=False)
        self.assertEqual(result["employers"][0]["status"], "sent")
        self.assertIn("Dee Permit", mock_send.call_args.kwargs["html_content"])
        self.assertIn("30 days", mock_send.call_args.kwargs["html_content"])
        mock_send.reset_mock()
        preview = work_permit_expiry_digest(dry_run=True)
        self.assertEqual(preview["dry_run"], True)
        self.assertEqual(preview["employers"][0]["status"], "skipped_already_sent")
        self.assertFalse(mock_send.called)

    def test_schedule_registers_nightly_beat_task(self):
        info = ensure_work_permit_digest_schedule()
        self.assertEqual(info["task"], "work_permit_expiry_digest")
        self.assertTrue(info["enabled"])
        self.assertIn("America/New_York", info["crontab"])
        again = ensure_work_permit_digest_schedule()
        self.assertFalse(again["created"])
