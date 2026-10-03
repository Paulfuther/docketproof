from datetime import date

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.test import TestCase

from arl.documentflow.models import ImmigrationStatusEvent
from arl.documentflow.services_immigration import (
    build_immigration_audit,
    update_immigration_tracker,
)
from arl.dsign.models import SignedDocumentFile
from arl.user.models import Employer
from arl.user.views import handle_new_hire_registration

User = get_user_model()


class ImmigrationTrackerTests(TestCase):
    def setUp(self):
        post_save.disconnect(handle_new_hire_registration, sender=User)
        self.employer = Employer.objects.create(name="Acme Retail", is_active=True)
        self.employee = User.objects.create_user(
            username="ada",
            password="Dock3t-Proof-Employee",
            email="ada@example.com",
            first_name="Ada",
            last_name="Lovelace",
            phone_number="+14161234567",
            employer=self.employer,
        )

    def test_tracker_update_stores_status_dates_and_file(self):
        document = SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="imm-file",
            file_name="permit.pdf",
            file_path="DOCUMENTS/acme/permit.pdf",
            document_title="Work permit extension",
        )

        event = update_immigration_tracker(
            employee=self.employee,
            employer=self.employer,
            created_by=self.employee,
            document_file=document,
            status_type="maintained_status",
            effective_date="2026-04-02",
            expiry_date="2026-10-02",
            reference_number="MS-7",
            notes="Waiting on IRCC",
        )

        self.assertIsInstance(event, ImmigrationStatusEvent)
        self.assertEqual(event.document_file_id, document.id)
        self.assertEqual(event.effective_date, date(2026, 4, 2))
        self.assertEqual(event.expiry_date, date(2026, 10, 2))
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.work_permit_extension_requested)
        self.assertEqual(self.employee.work_permit_extension_date, date(2026, 4, 2))
        self.assertEqual(self.employee.work_permit_expiration_date, date(2026, 10, 2))

        audit = build_immigration_audit(self.employer)
        self.assertEqual(audit["immigration_rows"][0]["latest_immigration_event"].id, event.id)

    def test_unknown_status_does_not_write_a_tracker_row(self):
        document = SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="imm-file",
            file_name="note.pdf",
            file_path="DOCUMENTS/acme/note.pdf",
            document_title="Note",
        )
        with self.assertRaises(ValueError):
            update_immigration_tracker(
                employee=self.employee,
                employer=self.employer,
                created_by=self.employee,
                document_file=document,
                status_type="made-up",
            )
        self.assertFalse(ImmigrationStatusEvent.objects.exists())
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.work_permit_extension_requested)
