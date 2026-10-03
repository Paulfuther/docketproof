from datetime import date

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.test import TestCase

from arl.documentflow.models import (
    DocumentFlow,
    DocumentFlowStep,
    ImmigrationStatusEvent,
    SentDocuSignEnvelope,
    SentDocuSignRecipient,
)
from arl.documentflow.services import build_document_audit
from arl.documentflow.services_immigration import (
    build_immigration_audit,
    update_immigration_tracker,
)
from arl.dsign.models import DocuSignTemplate
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


class DocumentAuditTests(TestCase):
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
        self.handbook_template = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-handbook",
            template_name="Employee Handbook",
            is_ready_to_send=True,
        )
        self.offer_template = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-offer",
            template_name="Offer Letter",
            is_ready_to_send=True,
        )
        self.other_template = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-other",
            template_name="One-off Form",
            is_ready_to_send=True,
        )
        self.flow = DocumentFlow.objects.create(
            employer=self.employer,
            name="New Hire Document Flow",
            is_active=True,
            is_default=True,
        )
        self.handbook_step = DocumentFlowStep.objects.create(
            flow=self.flow,
            template=self.handbook_template,
            step_order=1,
            label="Handbook",
        )
        self.offer_step = DocumentFlowStep.objects.create(
            flow=self.flow,
            template=self.offer_template,
            step_order=2,
            label="Offer",
        )

    def _step_result(self, audit, step_id):
        row = audit["rows"][0]
        return next(step for step in row["step_results"] if step["step_id"] == step_id)

    def test_manual_gsa_send_without_flow_links_shows_in_audit(self):
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=self.handbook_template,
            template_name=self.handbook_template.template_name,
            envelope_id="env-manual-handbook",
            status="sent",
        )

        audit = build_document_audit(self.employer)
        handbook = self._step_result(audit, self.handbook_step.id)

        self.assertEqual(handbook["status"], "sent")
        self.assertEqual(handbook["label"], "Sent")
        self.assertEqual(handbook["pill_class"], "primary")

    def test_manual_send_not_in_default_flow_has_no_audit_column(self):
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=self.other_template,
            template_name=self.other_template.template_name,
            envelope_id="env-one-off",
            status="sent",
        )

        audit = build_document_audit(self.employer)
        row = audit["rows"][0]

        self.assertEqual(len(row["step_results"]), 2)
        self.assertTrue(
            all(step["status"] == "not_sent" for step in row["step_results"])
        )

    def test_recipient_pills_appear_for_legacy_manual_send(self):
        envelope = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=self.handbook_template,
            template_name=self.handbook_template.template_name,
            envelope_id="env-manual-delivered",
            status="delivered",
        )
        SentDocuSignRecipient.objects.create(
            sent_envelope=envelope,
            recipient_id="1",
            role_name="GSA",
            name="Ada Lovelace",
            email="ada@example.com",
            routing_order=1,
            status="delivered",
        )

        audit = build_document_audit(self.employer)
        handbook = self._step_result(audit, self.handbook_step.id)

        self.assertEqual(handbook["status"], "delivered")
        self.assertEqual(handbook["label"], "Opened")
        self.assertEqual(handbook["pill_class"], "warning")
        self.assertEqual(len(handbook["recipient_pills"]), 1)
        self.assertEqual(handbook["recipient_pills"][0]["label"], "Opened")
        self.assertEqual(handbook["recipient_pills"][0]["pill_class"], "warning")
