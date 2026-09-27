from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from arl.documentflow.models import (
    DocumentFlow,
    DocumentFlowStep,
    SentDocuSignEnvelope,
    SentDocuSignRecipient,
)
from arl.documentflow.services import (
    _employee_name_key,
    _hired_sort_key,
    build_document_audit,
)
from arl.dsign.models import DocuSignTemplate
from arl.user.models import CustomUser, Employer, Store


class HRDocumentsScanTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Scan Fuels")
        self.other = Employer.objects.create(name="Other Fuels")
        self.store = Store.objects.create(
            number=405,
            employer=self.employer,
            address="1 Main",
            city="London",
            province="ON",
        )
        self.hr = self._person(
            "hr.scan",
            "+15195550100",
            first_name="Hannah",
            last_name="Scan",
        )
        manager, _ = Group.objects.get_or_create(name="Manager")
        self.hr.groups.add(manager)

        self.offer = self._template("offer-template", "Offer Letter")
        self.policy = self._template("policy-template", "Policy Handbook")
        self.flow = DocumentFlow.objects.create(
            employer=self.employer,
            name="New Hire Document Flow",
            is_active=True,
            is_default=True,
        )
        self.offer_step = DocumentFlowStep.objects.create(
            flow=self.flow,
            template=self.offer,
            step_order=1,
            label="Offer Letter",
        )
        self.policy_step = DocumentFlowStep.objects.create(
            flow=self.flow,
            template=self.policy,
            step_order=2,
            label="Policy Handbook",
        )

    def _person(self, username, phone, employer=None, **extra):
        store = extra.pop("store", None)
        user = CustomUser.objects.create_user(
            username=username,
            email=extra.pop("email", f"{username}@example.com"),
            password="pass12345",
            phone_number=phone,
            employer=employer or self.employer,
            first_name=extra.pop("first_name", "First"),
            last_name=extra.pop("last_name", "Last"),
            is_active=extra.pop("is_active", True),
            **extra,
        )
        if store is not None:
            user.store = store
            user.save(update_fields=["store"])
        return user

    def _template(self, template_id, name, employer=None):
        return DocuSignTemplate.objects.create(
            employer=employer or self.employer,
            template_id=template_id,
            template_name=name,
        )

    def _envelope(self, user, step, status, envelope_id):
        envelope = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=user,
            template=step.template,
            flow=self.flow,
            flow_step=step,
            template_name=step.template.template_name,
            envelope_id=envelope_id,
            status=status,
        )
        SentDocuSignRecipient.objects.create(
            sent_envelope=envelope,
            recipient_id=f"{envelope_id}-1",
            email=user.email,
            name=user.get_full_name(),
            role_name="GSA",
            status=status if status != "delivered" else "delivered",
        )
        return envelope

    def _rows(self, **kwargs):
        return build_document_audit(self.employer, **kwargs)["rows"]

    def test_issues_rank_missing_before_complete(self):
        self._person(
            "mia.missing",
            "+15195550101",
            first_name="Mia",
            last_name="Missing",
            store=self.store,
        )
        partial = self._person(
            "pat.partial",
            "+15195550102",
            first_name="Pat",
            last_name="Partial",
            store=self.store,
        )
        done = self._person(
            "bea.done",
            "+15195550103",
            first_name="Bea",
            last_name="Done",
            store=self.store,
        )
        self._envelope(partial, self.offer_step, "completed", "env-partial-offer")
        self._envelope(done, self.offer_step, "completed", "env-done-offer")
        self._envelope(done, self.policy_step, "completed", "env-done-policy")

        names = [row["employee"].get_full_name() for row in self._rows()]
        self.assertLess(names.index("Mia Missing"), names.index("Pat Partial"))
        self.assertLess(names.index("Pat Partial"), names.index("Bea Done"))
        by_name = {row["employee"].get_full_name(): row for row in self._rows()}
        self.assertEqual(by_name["Mia Missing"]["overall_status"], "not_sent")
        self.assertEqual(by_name["Mia Missing"]["overall_chip"]["label"], "Not Sent")
        self.assertEqual(by_name["Mia Missing"]["overall_chip"]["tone"], "urgent")
        self.assertFalse(by_name["Mia Missing"]["can_resend"])
        self.assertEqual(by_name["Pat Partial"]["overall_chip"]["label"], "In Progress")
        self.assertTrue(by_name["Bea Done"]["is_complete"])
        self.assertFalse(by_name["Bea Done"]["can_resend"])

        offer = by_name["Mia Missing"]["steps"][0]
        self.assertEqual(offer["step_name"], "Offer Letter")
        self.assertEqual(offer["label"], "Not Sent")
        self.assertEqual(offer["tone"], "urgent")

    def test_issues_only_hides_complete_rows(self):
        self._person(
            "bea.done",
            "+15195550110",
            first_name="Bea",
            last_name="Done",
        )
        open_user = self._person(
            "sam.sent",
            "+15195550111",
            first_name="Sam",
            last_name="Sent",
        )
        done = CustomUser.objects.get(username="bea.done")
        self._envelope(done, self.offer_step, "completed", "env-hide-offer")
        self._envelope(done, self.policy_step, "completed", "env-hide-policy")
        self._envelope(open_user, self.offer_step, "sent", "env-open-offer")

        flagged = self._rows(incomplete_only=True)
        flagged_ids = [row["employee"].pk for row in flagged]
        self.assertIn(open_user.pk, flagged_ids)
        self.assertNotIn(done.pk, flagged_ids)
        open_row = next(row for row in flagged if row["employee"].pk == open_user.pk)
        self.assertTrue(open_row["can_resend"])
        self.assertEqual(open_row["steps"][0]["label"], "Sent")
        self.assertEqual(open_row["steps"][0]["tone"], "auth")

    def test_resend_only_for_sent_and_delivered(self):
        user = self._person(
            "dee.livered",
            "+15195550120",
            first_name="Dee",
            last_name="Livered",
        )
        self._envelope(user, self.offer_step, "delivered", "env-delivered")
        self._envelope(user, self.policy_step, "declined", "env-declined")
        row = next(item for item in self._rows() if item["employee"].pk == user.pk)
        offer, policy = row["steps"]
        self.assertTrue(offer["can_resend"])
        self.assertEqual(offer["label"], "Opened")
        self.assertEqual(offer["tone"], "watch")
        self.assertFalse(policy["can_resend"])
        self.assertEqual(policy["label"], "Declined")
        self.assertTrue(row["can_resend"])

        queued = self._person(
            "que.ued",
            "+15195550121",
            first_name="Que",
            last_name="Ued",
        )
        self._envelope(queued, self.offer_step, "created", "env-created")
        self._envelope(queued, self.policy_step, "voided", "env-voided")
        queued_row = next(
            row for row in self._rows() if row["employee"].pk == queued.pk
        )
        self.assertFalse(queued_row["can_resend"])
        self.assertEqual(queued_row["steps"][0]["label"], "Queued")
        self.assertEqual(queued_row["steps"][1]["label"], "Voided")

    def test_search_matches_name_email_and_store_and_hides_other_employer(self):
        self._person(
            "aria.store",
            "+15195550130",
            first_name="Aria",
            last_name="Store",
            email="aria.store@example.com",
            store=self.store,
        )
        self._person(
            "other.person",
            "+15195550131",
            employer=self.other,
            first_name="Aria",
            last_name="Other",
            store=Store.objects.create(
                number=405,
                employer=self.other,
                address="2 Main",
                city="London",
                province="ON",
            ),
        )
        by_store = self._rows(search_query="405")
        self.assertEqual(
            [row["employee"].username for row in by_store],
            ["aria.store"],
        )
        by_email = self._rows(search_query="aria.store@example.com")
        self.assertEqual(len(by_email), 1)
        self.assertEqual(by_email[0]["employee"].first_name, "Aria")

    def test_name_sort(self):
        self._person("zoe.user", "+15195550140", first_name="Zoe", last_name="Young")
        self._person("amy.user", "+15195550141", first_name="Amy", last_name="Young")
        ordered = [
            row["employee"].first_name
            for row in self._rows(sort="name")
            if row["employee"].username in {"amy.user", "zoe.user"}
        ]
        self.assertEqual(ordered, ["Amy", "Zoe"])

    def test_date_hired_sort_is_newest_first(self):
        now = timezone.now()
        older = self._person(
            "zoe.hired",
            "+15195550142",
            first_name="Zoe",
            last_name="Older",
        )
        newer = self._person(
            "amy.hired",
            "+15195550143",
            first_name="Amy",
            last_name="Newer",
        )
        blank = self._person(
            "blank.hired",
            "+15195550144",
            first_name="",
            last_name="",
        )
        older.date_joined = now - timedelta(days=40)
        newer.date_joined = now - timedelta(days=2)
        blank.date_joined = now - timedelta(days=9)
        for user in (older, newer, blank):
            user.save(update_fields=["date_joined"])

        hired = [
            row["employee"].username
            for row in self._rows(sort="hired")
            if row["employee"].username in {"zoe.hired", "amy.hired", "blank.hired"}
        ]
        self.assertEqual(hired, ["amy.hired", "blank.hired", "zoe.hired"])

        complete = self._person(
            "done.hired",
            "+15195550145",
            first_name="Done",
            last_name="File",
        )
        self._envelope(complete, self.offer_step, "completed", "env-hired-offer")
        self._envelope(complete, self.policy_step, "completed", "env-hired-policy")
        complete.date_joined = now
        complete.save(update_fields=["date_joined"])

        filtered = [
            row["employee"].username
            for row in self._rows(
                sort="Hired",
                search_query="Newer",
                incomplete_only=True,
            )
        ]
        self.assertEqual(filtered, ["amy.hired"])

        step_sort = f"step-{self.offer_step.id}"
        self.assertEqual(
            build_document_audit(self.employer, sort=step_sort)["audit_sort"],
            step_sort,
        )
        self.assertEqual(
            build_document_audit(self.employer, sort="nope")["audit_sort"],
            "",
        )

        missing = SimpleNamespace(
            date_joined=None,
            last_name="Older",
            first_name="Zoe",
            username="missing.date",
            pk=older.pk + 1,
        )
        present = SimpleNamespace(
            date_joined=now,
            last_name="Older",
            first_name="Zoe",
            username="has.date",
            pk=older.pk + 2,
        )
        self.assertLess(
            _hired_sort_key(present),
            _hired_sort_key(missing),
        )
        missing_amy = SimpleNamespace(
            date_joined=None,
            last_name="Amy",
            first_name="A",
            username="a",
            pk=1,
        )
        missing_zoe = SimpleNamespace(
            date_joined=None,
            last_name="Zoe",
            first_name="A",
            username="z",
            pk=2,
        )
        self.assertLess(
            _hired_sort_key(missing_amy),
            _hired_sort_key(missing_zoe),
        )
        self.assertEqual(
            _hired_sort_key(missing_amy)[1],
            _employee_name_key(missing_amy),
        )

        self.client.force_login(self.hr)
        response = self.client.get(
            reverse("document_audit_log_partial"),
            {"audit_sort": "hired", "audit_q": "Newer", "audit_incomplete": "1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(">Name (A-Z)<", body)
        self.assertIn(">Date hired (newest)<", body)
        self.assertIn('value="hired" selected', body)
        self.assertIn('value="Newer"', body)
        self.assertIn("checked", body)
        self.assertIn("Amy Newer", body)
        self.assertNotIn("Zoe Older", body)
        self.assertNotIn("Done File", body)
        self.assertIn("Offer Letter", body)

    def test_partial_uses_live_search_and_real_step_names(self):
        self._person(
            "mia.missing",
            "+15195550150",
            first_name="Mia",
            last_name="Missing",
            store=self.store,
        )
        sent_user = self._person(
            "sam.sent",
            "+15195550151",
            first_name="Sam",
            last_name="Sent",
            store=self.store,
        )
        self._envelope(sent_user, self.offer_step, "sent", "env-ui-offer")
        self.client.force_login(self.hr)
        response = self.client.get(reverse("document_audit_log_partial"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("HR Documents", body)
        self.assertIn("Issues only", body)
        self.assertIn("Offer Letter", body)
        self.assertIn("Policy Handbook", body)
        self.assertIn('name="audit_q"', body)
        self.assertIn("delay:300ms", body)
        self.assertIn("font-size: max(16px, 1rem)", body)
        self.assertIn("max-width: 767.98px", body)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)", body)
        self.assertIn("justify-self: stretch", body)
        self.assertNotIn("Resend outstanding documents", body)
        self.assertIn(reverse("resend_hr_documents", args=[sent_user.pk]), body)
        self.assertIn(f'"step_id": "{self.offer_step.id}"', body)
        self.assertNotIn(
            reverse(
                "resend_hr_documents",
                args=[CustomUser.objects.get(username="mia.missing").pk],
            ),
            body,
        )
        self.assertNotIn("SIN docs", body)
        self.assertLess(body.index("Mia Missing"), body.index("Sam Sent"))

        flagged = self.client.get(
            reverse("document_audit_log_partial"),
            {"audit_incomplete": "1", "audit_q": "sam"},
        )
        flagged_body = flagged.content.decode()
        self.assertIn("Sam Sent", flagged_body)
        self.assertNotIn("Mia Missing", flagged_body)

    def test_partial_forbidden_without_hr_group(self):
        outsider = self._person(
            "no.hr",
            "+15195550160",
            first_name="No",
            last_name="Access",
        )
        self.client.force_login(outsider)
        response = self.client.get(reverse("document_audit_log_partial"))
        self.assertEqual(response.status_code, 403)

    @patch("arl.dsign.helpers.resend_docusign_envelope")
    def test_resend_posts_only_outstanding_envelopes(self, mock_resend):
        user = self._person(
            "sam.sent",
            "+15195550170",
            first_name="Sam",
            last_name="Sent",
        )
        sent = self._envelope(user, self.offer_step, "sent", "env-resend-offer")
        self._envelope(user, self.policy_step, "completed", "env-resend-policy")
        self.client.force_login(self.hr)
        bulk = self.client.post(
            reverse("resend_hr_documents", args=[user.pk]),
            {"audit_q": "", "audit_sort": ""},
        )
        self.assertEqual(bulk.status_code, 200)
        mock_resend.assert_not_called()
        self.assertContains(bulk, "Nothing to resend")

        response = self.client.post(
            reverse("resend_hr_documents", args=[user.pk]),
            {"audit_q": "", "audit_sort": "", "step_id": self.offer_step.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_resend.call_count, 1)
        self.assertEqual(mock_resend.call_args.args[0].pk, sent.pk)
        self.assertContains(response, "Resent Offer Letter for Sam Sent.")

        mock_resend.reset_mock()
        one_step = self.client.post(
            reverse("resend_hr_documents", args=[user.pk]),
            {"step_id": self.policy_step.id},
        )
        self.assertEqual(one_step.status_code, 200)
        mock_resend.assert_not_called()
        self.assertContains(one_step, "Nothing to resend")

    @patch("arl.dsign.helpers.resend_docusign_envelope")
    def test_resend_refuses_other_employer(self, mock_resend):
        outsider = self._person(
            "other.hr",
            "+15195550180",
            employer=self.other,
            first_name="Other",
            last_name="Hr",
        )
        outsider.groups.add(Group.objects.get_or_create(name="Manager")[0])
        target = self._person(
            "sam.sent",
            "+15195550181",
            first_name="Sam",
            last_name="Sent",
        )
        self._envelope(target, self.offer_step, "sent", "env-other-offer")
        self.client.force_login(outsider)
        response = self.client.post(reverse("resend_hr_documents", args=[target.pk]))
        self.assertEqual(response.status_code, 404)
        mock_resend.assert_not_called()
