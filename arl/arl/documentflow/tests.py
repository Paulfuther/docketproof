from datetime import date

from django.contrib.auth import get_user_model
from django.utils import timezone
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

    def _column_result(self, audit, column_key):
        row = audit["rows"][0]
        return next(
            step for step in row["step_results"] if step["column_key"] == column_key
        )

    def test_completed_audit_step_includes_completed_at(self):
        completed_at = timezone.now()
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=self.handbook_template,
            template_name=self.handbook_template.template_name,
            envelope_id="env-completed-handbook",
            status="completed",
            completed_at=completed_at,
        )

        audit = build_document_audit(self.employer)
        handbook = self._step_result(audit, self.handbook_step.id)

        self.assertEqual(handbook["status"], "completed")
        self.assertEqual(handbook["completed_at"], completed_at)

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

    def test_manual_send_not_in_default_flow_gets_its_own_column(self):
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
        extra = self._column_result(
            audit, f"extra-tpl-{self.other_template.id}"
        )

        self.assertEqual(len(row["step_results"]), 3)
        self.assertEqual(extra["status"], "sent")
        self.assertEqual(extra["label"], "Sent")
        self.assertFalse(extra["is_flow_step"])
        handbook = self._step_result(audit, self.handbook_step.id)
        offer = self._step_result(audit, self.offer_step.id)
        self.assertEqual(handbook["status"], "not_sent")
        self.assertEqual(offer["status"], "not_sent")

    def test_envelope_on_non_default_flow_still_matches_step_template(self):
        other_flow = DocumentFlow.objects.create(
            employer=self.employer,
            name="Old flow",
            is_active=True,
            is_default=False,
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=self.handbook_template,
            template_name=self.handbook_template.template_name,
            flow=other_flow,
            envelope_id="env-wrong-flow",
            status="sent",
        )

        audit = build_document_audit(self.employer)
        handbook = self._step_result(audit, self.handbook_step.id)

        self.assertEqual(handbook["status"], "sent")
        self.assertEqual(handbook["label"], "Sent")

    def test_template_name_only_legacy_send_matches_flow_step(self):
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name=self.handbook_template.template_name,
            envelope_id="env-name-only",
            status="sent",
        )

        audit = build_document_audit(self.employer)
        handbook = self._step_result(audit, self.handbook_step.id)

        self.assertEqual(handbook["status"], "sent")
        self.assertEqual(handbook["label"], "Sent")

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

    def test_second_quiz_keeps_its_own_column(self):
        quiz_one = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-quiz-one",
            template_name="Safety Quiz",
            is_ready_to_send=True,
        )
        quiz_two = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-quiz-two",
            template_name="Onboarding Quiz",
            is_ready_to_send=True,
        )
        quiz_step = DocumentFlowStep.objects.create(
            flow=self.flow,
            template=quiz_one,
            step_order=3,
            label="Safety Quiz",
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=quiz_one,
            template_name=quiz_one.template_name,
            flow=self.flow,
            flow_step=quiz_step,
            envelope_id="env-quiz-one",
            status="completed",
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=quiz_two,
            template_name=quiz_two.template_name,
            flow=self.flow,
            flow_step=quiz_step,
            envelope_id="env-quiz-two",
            status="sent",
        )

        audit = build_document_audit(self.employer)
        quiz_one_col = self._column_result(audit, f"step-{quiz_step.id}")
        quiz_two_col = self._column_result(
            audit, f"extra-tpl-{quiz_two.id}"
        )

        self.assertEqual(quiz_one_col["status"], "completed")
        self.assertEqual(quiz_one_col["label"], "Complete")
        self.assertEqual(quiz_two_col["status"], "sent")
        self.assertEqual(quiz_two_col["label"], "Sent")

    def test_employee_address_update_shows_as_sent_column(self):
        address_template = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-address",
            template_name="employee address update",
            is_ready_to_send=True,
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=address_template,
            template_name=address_template.template_name,
            envelope_id="env-address",
            status="delivered",
        )

        audit = build_document_audit(self.employer)
        address_col = self._column_result(
            audit, f"extra-tpl-{address_template.id}"
        )

        self.assertEqual(address_col["step_name"], "employee address update")
        self.assertEqual(address_col["status"], "delivered")
        self.assertEqual(address_col["label"], "Opened")

    def test_pill_rows_wrap_after_three_documents(self):
        extra_templates = []
        for index in range(3):
            template = DocuSignTemplate.objects.create(
                employer=self.employer,
                template_id=f"tpl-extra-{index}",
                template_name=f"Extra Form {index}",
                is_ready_to_send=True,
            )
            extra_templates.append(template)
            SentDocuSignEnvelope.objects.create(
                employer=self.employer,
                user=self.employee,
                template=template,
                template_name=template.template_name,
                envelope_id=f"env-extra-{index}",
                status="sent",
            )

        audit = build_document_audit(self.employer)
        row = audit["rows"][0]

        self.assertEqual(len(row["steps"]), 5)
        self.assertEqual(len(row["pill_rows"]), 2)
        self.assertEqual(len(row["pill_rows"][0]["steps"]), 3)
        self.assertEqual(len(row["pill_rows"][0]["empty_slots"]), 0)
        self.assertTrue(row["pill_rows"][0]["show_overall"])
        self.assertEqual(len(row["pill_rows"][1]["steps"]), 2)
        self.assertEqual(len(row["pill_rows"][1]["empty_slots"]), 2)
        self.assertFalse(row["pill_rows"][1]["show_overall"])
        self.assertEqual(len(audit["document_header_slots"]), 3)
        self.assertEqual(
            [slot["name"] for slot in audit["document_header_slots"]],
            ["Employee Handbook", "Offer Letter", ""],
        )

    def test_document_header_slots_ignore_extra_documents(self):
        extra_template = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-extra-header",
            template_name="Employee Address Update",
            is_ready_to_send=True,
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=extra_template,
            template_name=extra_template.template_name,
            envelope_id="env-extra-header",
            status="sent",
        )

        audit = build_document_audit(self.employer)
        header_names = [slot["name"] for slot in audit["document_header_slots"]]

        self.assertEqual(header_names, ["Employee Handbook", "Offer Letter", ""])
        self.assertEqual(len(audit["rows"][0]["steps"]), 3)

    def test_pill_rows_use_four_slots_on_continuation_rows(self):
        extra_templates = []
        for index in range(7):
            template = DocuSignTemplate.objects.create(
                employer=self.employer,
                template_id=f"tpl-wrap-{index}",
                template_name=f"Extra Form {index}",
                is_ready_to_send=True,
            )
            extra_templates.append(template)
            SentDocuSignEnvelope.objects.create(
                employer=self.employer,
                user=self.employee,
                template=template,
                template_name=template.template_name,
                envelope_id=f"env-wrap-{index}",
                status="sent",
            )

        audit = build_document_audit(self.employer)
        pill_rows = audit["rows"][0]["pill_rows"]

        self.assertEqual(len(audit["rows"][0]["steps"]), 9)
        self.assertEqual(len(pill_rows), 3)
        self.assertEqual(len(pill_rows[0]["steps"]), 3)
        self.assertTrue(pill_rows[0]["show_overall"])
        self.assertEqual(len(pill_rows[1]["steps"]), 4)
        self.assertEqual(pill_rows[1]["empty_slots"], [])
        self.assertEqual(len(pill_rows[2]["steps"]), 2)
        self.assertEqual(len(pill_rows[2]["empty_slots"]), 2)
