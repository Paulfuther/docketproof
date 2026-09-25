from datetime import date, timedelta
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from arl.documentflow.models import ImmigrationStatusEvent
from arl.documentflow.services_immigration import (
    EXTENSION_SHORTCUT_DATE_ERROR,
    EXTENSION_SHORTCUT_EVENT_ERROR,
    HR_WATCH_DAYS,
    HR_WATCH_LABEL,
    PRIORITY_MAP,
    _overall_status,
    _permit_status,
    _sin_status,
    active_permit_override_event,
    build_immigration_audit,
    event_overrides_permit,
    extension_shortcut_error,
    overall_priority,
)
from arl.dsign.models import SignedDocumentFile
from arl.user.models import CustomUser, Employer


class _User:
    def __init__(self, **kwargs):
        self.sin_plain = kwargs.get("sin_plain")
        self.sin_expiration_date = kwargs.get("sin_expiration_date")
        self.work_permit_expiration_date = kwargs.get(
            "work_permit_expiration_date"
        )
        self.work_permit_extension_requested = kwargs.get(
            "work_permit_extension_requested", False
        )


class _Event:
    def __init__(
        self,
        status_type,
        reference_number="",
        document_file_id=None,
        is_active=True,
    ):
        self.status_type = status_type
        self.reference_number = reference_number
        self.document_file_id = document_file_id
        self.document_file = None
        self.is_active = is_active

    def get_status_type_display(self):
        from arl.documentflow.constants import IMMIGRATION_STATUS_TYPES

        return IMMIGRATION_STATUS_TYPES[self.status_type]["label"]


def _temporary_sin():
    return {"is_temporary": True, "code": "temporary"}


class ImmigrationPriorityTests(SimpleTestCase):
    def test_map_order_and_no_dead_needs_review(self):
        codes = [
            "urgent",
            "expiring_soon",
            "extension_pending",
            "compliant_sin_update_needed",
            "compliant",
        ]
        weights = [overall_priority(code) for code in codes]
        self.assertEqual(weights, sorted(weights))
        self.assertEqual(len(set(weights)), len(codes))
        self.assertNotIn("needs_review", PRIORITY_MAP)
        self.assertLess(overall_priority("unmapped_status"), overall_priority("compliant"))

    def test_emitted_overall_codes_are_mapped(self):
        temporary = {"is_temporary": True, "code": "temporary"}
        expired_sin = {"is_temporary": True, "code": "expired"}
        missing_sin = {"is_temporary": False, "code": "missing"}
        permanent = {"is_temporary": False, "code": "permanent"}
        scenarios = [
            _overall_status(expired_sin, {"code": "extension_pending", "label": "Maintained Status"}),
            _overall_status(expired_sin, {"code": "valid", "label": "Valid"}),
            _overall_status(temporary, {"code": "valid", "label": "Valid"}),
            _overall_status(missing_sin, {"code": "not_required", "label": "Permit Not Required"}),
            _overall_status(expired_sin, {"code": "expired", "label": "Expired"}),
            _overall_status(
                {"is_temporary": True, "code": "expiring_soon"},
                {"code": "expiring_soon", "label": HR_WATCH_LABEL},
            ),
            _overall_status(permanent, {"code": "not_required", "label": "Permit Not Required"}),
            _overall_status(temporary, {"code": "missing_expiry", "label": "Missing Permit Expiry"}),
        ]
        emitted = {row["code"] for row in scenarios}
        self.assertEqual(emitted, set(PRIORITY_MAP))
        for row in scenarios:
            self.assertIn(row["code"], PRIORITY_MAP)

    def test_watch_label_is_120_days_not_a_new_bucket(self):
        self.assertEqual(HR_WATCH_DAYS, 120)
        user = _User(
            sin_plain="923456789",
            sin_expiration_date=date.today() + timedelta(days=120),
            work_permit_expiration_date=date.today() + timedelta(days=120),
        )
        sin_info = _sin_status(user)
        permit_info = _permit_status(user, sin_info)
        self.assertEqual(sin_info["code"], "expiring_soon")
        self.assertEqual(sin_info["label"], HR_WATCH_LABEL)
        self.assertEqual(permit_info["code"], "expiring_soon")
        self.assertEqual(permit_info["label"], HR_WATCH_LABEL)
        overall = _overall_status(sin_info, permit_info)
        self.assertEqual(overall["code"], "expiring_soon")
        self.assertEqual(overall["label"], HR_WATCH_LABEL)

        user.work_permit_expiration_date = date.today() + timedelta(days=121)
        user.sin_expiration_date = date.today() + timedelta(days=121)
        sin_info = _sin_status(user)
        permit_info = _permit_status(user, sin_info)
        self.assertEqual(sin_info["code"], "temporary")
        self.assertEqual(permit_info["code"], "valid")
        self.assertNotEqual(sin_info["label"], HR_WATCH_LABEL)

    def test_proof_event_drives_status_ahead_of_checkbox(self):
        user = _User(
            work_permit_extension_requested=True,
            work_permit_expiration_date=date.today() - timedelta(days=3),
        )
        event = _Event("maintained_status", reference_number="W123456789")
        permit = _permit_status(user, _temporary_sin(), event)
        self.assertEqual(permit["code"], "extension_pending")
        self.assertEqual(permit["label"], "Maintained Status")
        self.assertEqual(permit["source"], "event")
        overall = _overall_status(
            {"is_temporary": True, "code": "expired"},
            permit,
        )
        self.assertEqual(overall["code"], "extension_pending")
        self.assertEqual(overall["label"], "Maintained Status")

    def test_checkbox_still_ranks_when_no_proof_event(self):
        user = _User(
            work_permit_extension_requested=True,
            work_permit_expiration_date=date.today() - timedelta(days=3),
        )
        empty = _Event("work_permit_extension", reference_number="  ")
        self.assertFalse(event_overrides_permit(empty))
        permit = _permit_status(user, _temporary_sin(), empty)
        self.assertEqual(permit["source"], "checkbox")
        self.assertEqual(permit["label"], "Extension Pending")

    def test_review_note_and_inactive_event_do_not_override(self):
        user = _User(work_permit_expiration_date=date.today() - timedelta(days=1))
        note = _Event("review_note", reference_number="NOTE-1")
        inactive = _Event(
            "work_permit_extension",
            reference_number="W1",
            is_active=False,
        )
        self.assertFalse(event_overrides_permit(note))
        self.assertFalse(event_overrides_permit(inactive))
        permit = _permit_status(user, _temporary_sin(), note)
        self.assertEqual(permit["code"], "expired")


class ImmigrationAuditTemplateTests(SimpleTestCase):
    def test_pills_wrap_and_name_the_watch_window(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "user"
            / "hr"
            / "partials"
            / "immigration_audit.html"
        )
        text = template.read_text()
        self.assertNotIn("width: 150px", text)
        self.assertNotIn("width: 130px", text)
        self.assertIn("white-space: normal", text)
        self.assertIn("Expiring within 120 days (HR watch)", text)
        self.assertIn("title=", text)


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class ImmigrationProofAndSortTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Audit Fuels")
        self.hr = self._user("hr-audit")

    def _user(self, username, **kwargs):
        serial = CustomUser.objects.count() + 1
        return CustomUser.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="pass12345",
            phone_number=f"+1519555{serial:04d}",
            employer=self.employer,
            **kwargs,
        )

    def _event(self, user, status_type="maintained_status", **kwargs):
        defaults = {
            "user": user,
            "employer": self.employer,
            "status_type": status_type,
            "effective_date": date.today(),
            "reference_number": "W123456789",
            "created_by": self.hr,
            "is_active": True,
        }
        defaults.update(kwargs)
        return ImmigrationStatusEvent.objects.create(**defaults)

    def test_override_event_requires_proof_who_and_when(self):
        employee = self._user("proof-employee")
        with self.assertRaises(ValidationError):
            ImmigrationStatusEvent.objects.create(
                user=employee,
                employer=self.employer,
                status_type="work_permit_extension",
                is_active=True,
                created_by=self.hr,
                effective_date=date.today(),
            )
        with self.assertRaises(ValidationError):
            self._event(employee, reference_number="   ", effective_date=date.today())
        with self.assertRaises(ValidationError):
            self._event(employee, effective_date=None)
        with self.assertRaises(ValidationError):
            self._event(employee, created_by=None)

        saved = self._event(employee)
        self.assertEqual(saved.reference_number, "W123456789")

        document = SignedDocumentFile.objects.create(
            user=employee,
            employer=self.employer,
            envelope_id="env-proof",
            file_name="wp-ext.pdf",
            file_path="DOCUMENTS/wp-ext.pdf",
        )
        with_document = self._event(
            employee,
            status_type="work_authorization_letter_applied",
            reference_number="",
            document_file=document,
        )
        self.assertEqual(with_document.document_file_id, document.pk)

    def test_non_override_and_inactive_rows_can_omit_proof(self):
        employee = self._user("note-employee")
        note = ImmigrationStatusEvent.objects.create(
            user=employee,
            employer=self.employer,
            status_type="review_note",
            is_active=True,
        )
        self.assertFalse(event_overrides_permit(note))
        retired = ImmigrationStatusEvent.objects.create(
            user=employee,
            employer=self.employer,
            status_type="pgwp_application",
            is_active=False,
        )
        self.assertIsNone(retired.reference_number or None)

    def test_extension_shortcut_blocks_new_flips_without_an_event(self):
        employee = self._user("shortcut-employee")
        self.assertEqual(
            extension_shortcut_error(
                employee,
                True,
                None,
                ["work_permit_extension_requested"],
            ),
            EXTENSION_SHORTCUT_DATE_ERROR,
        )
        self.assertEqual(
            extension_shortcut_error(
                employee,
                True,
                date.today(),
                ["work_permit_extension_date"],
            ),
            EXTENSION_SHORTCUT_EVENT_ERROR,
        )
        self.assertIsNone(
            extension_shortcut_error(
                employee,
                True,
                None,
                ["phone_number"],
            )
        )
        self.assertIsNone(
            extension_shortcut_error(
                employee,
                False,
                None,
                ["work_permit_extension_requested"],
            )
        )
        self._event(employee)
        self.assertIsNone(
            extension_shortcut_error(
                employee,
                True,
                date.today(),
                ["work_permit_extension_requested"],
            )
        )

    def test_audit_sort_and_event_override(self):
        urgent = self._user("urgent-row")
        urgent.sin_expiration_date = None
        urgent.work_permit_expiration_date = None
        urgent.save()

        watching = self._user("watching-row")
        watching.sin_expiration_date = date.today() + timedelta(days=30)
        watching.work_permit_expiration_date = date.today() + timedelta(days=30)
        watching.save()

        event_row = self._user("event-row")
        event_row.sin_expiration_date = date.today() - timedelta(days=1)
        event_row.work_permit_expiration_date = date.today() - timedelta(days=5)
        event_row.work_permit_extension_requested = False
        event_row.save()
        self._event(event_row, status_type="maintained_status", reference_number="W999")

        checkbox_row = self._user("checkbox-row")
        checkbox_row.sin_expiration_date = date.today() - timedelta(days=1)
        checkbox_row.work_permit_expiration_date = date.today() - timedelta(days=1)
        checkbox_row.work_permit_extension_requested = True
        checkbox_row.work_permit_extension_date = date.today()
        checkbox_row.save()

        sin_update = self._user("sin-update-row")
        sin_update.sin_expiration_date = date.today() - timedelta(days=2)
        sin_update.work_permit_expiration_date = date.today() + timedelta(days=200)
        sin_update.save()

        compliant = self._user("compliant-row")

        sins = {
            "urgent-row": "900000001",
            "watching-row": "900000002",
            "event-row": "900000003",
            "checkbox-row": "900000004",
            "sin-update-row": "900000005",
            "compliant-row": "100000006",
            "hr-audit": "100000007",
        }

        def fake_sin(user):
            return sins[user.username]

        from unittest.mock import patch

        with patch(
            "arl.documentflow.services_immigration._get_sin_value",
            side_effect=fake_sin,
        ):
            rows = build_immigration_audit(self.employer)["immigration_rows"]

        names = [row["employee"].username for row in rows if row["employee"].username != "hr-audit"]
        self.assertEqual(
            names,
            [
                "urgent-row",
                "watching-row",
                "event-row",
                "checkbox-row",
                "sin-update-row",
                "compliant-row",
            ],
        )
        by_name = {row["employee"].username: row for row in rows}
        self.assertEqual(by_name["sin-update-row"]["overall_status"]["code"], "compliant_sin_update_needed")
        self.assertLess(
            overall_priority("compliant_sin_update_needed"),
            overall_priority("compliant"),
        )
        self.assertEqual(by_name["event-row"]["overall_status"]["label"], "Maintained Status")
        self.assertEqual(by_name["event-row"]["permit_info"]["source"], "event")
        self.assertEqual(by_name["checkbox-row"]["permit_info"]["source"], "checkbox")
        self.assertEqual(by_name["watching-row"]["overall_status"]["label"], HR_WATCH_LABEL)
        self.assertIsNone(active_permit_override_event(checkbox_row))
        self.assertEqual(
            active_permit_override_event(event_row).status_type,
            "maintained_status",
        )
