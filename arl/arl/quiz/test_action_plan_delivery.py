"""Checklist PDF stays with quiz_email. 6.4 action plan is a second file and email."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase as SimpleTestCase
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import timezone

from arl.quiz.action_plan_delivery import (
    ACTION_PLAN_EMAIL_GROUP,
    CHECKLIST_EMAIL_GROUP,
    action_plan_pdf_context,
    build_action_plan_dropbox_path,
    checklist_pdf_shows_action_plan,
    checklist_splits_action_plan,
    email_pdf_to_employer_group,
)
from arl.quiz.models import (
    Checklist,
    ChecklistActionItem,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateItem,
)
from arl.quiz.tasks import deliver_action_plan_pdf, generate_checklist_pdf_task
from arl.user.models import CustomUser, Employer, Store


class ActionPlanSplitRulesTests(SimpleTestCase):
    def _checklist(self, name, title="Walk", document_id="", items=None):
        return SimpleNamespace(
            title=title,
            template=SimpleNamespace(name=name, document_id=document_id),
            items=items or [],
        )

    def test_site_security_workplace_security_and_bc_on_inspection_split(self):
        names = (
            "Site Security",
            "Workplace Security Checklist",
            "Workplace Inspection Checklist (BC & ON)",
        )
        for name in names:
            checklist = self._checklist(name)
            self.assertTrue(checklist_splits_action_plan(checklist), name)
            self.assertFalse(checklist_pdf_shows_action_plan(checklist), name)

    def test_document_id_and_6_4_form_id_split_even_with_a_generic_name(self):
        by_document = self._checklist("Custom walk", document_id="enmcds840-6.3")
        self.assertTrue(checklist_splits_action_plan(by_document))

        item = SimpleNamespace(
            template_item=SimpleNamespace(action_plan_form="ENMCDS840-6.4")
        )
        by_form = self._checklist("Pump check", items=[item])
        self.assertTrue(checklist_splits_action_plan(by_form))

    def test_other_checklists_keep_a_combined_pdf(self):
        checklist = self._checklist("Fall Winter Exterior Checklist")
        self.assertFalse(checklist_splits_action_plan(checklist))
        self.assertTrue(checklist_pdf_shows_action_plan(checklist))


class ActionPlanPathTests(SimpleTestCase):
    def test_action_plan_file_sits_beside_the_checklist_pdf(self):
        when = datetime(2026, 9, 20)
        checklist = SimpleNamespace(
            id=42,
            slug="store-12-inspection-ab12cd34",
            title="Store 12 inspection",
            template=SimpleNamespace(name="Workplace Inspection Checklist (BC & ON)"),
            created_by=SimpleNamespace(employer=SimpleNamespace(name="Petro Canada")),
        )
        path, folder = build_action_plan_dropbox_path(checklist, "12", when=when)
        self.assertEqual(folder, "workplace-inspection-checklist-bc-on")
        self.assertEqual(
            path,
            "/CHECKLISTS/petro-canada/workplace-inspection-checklist-bc-on/"
            "12/2026/09-September/"
            "12_store-12-inspection-ab12cd34-42-action-plan.pdf",
        )


class ActionPlanFormTemplateTests(SimpleTestCase):
    def test_pdf_html_matches_6_4_columns_and_header(self):
        html = render_to_string(
            "quiz/action_plan_pdf.html",
            {
                "form_title": "6.4 Site Security Action Plan Form",
                "form_subtitle": "Workplace Violence and Harassment Prevention",
                "source_title": "Store 12 inspection",
                "document_id": "ENMCDS840-6.3",
                "site_location": "12, 123 Main St, London, ON",
                "completed_by": "Ada Lovelace",
                "position": "Manager",
                "completed_on": "2026-09-28",
                "rows": [
                    {
                        "action_item": "Door lock",
                        "action_required": "Replace the lock",
                        "when": date(2026, 10, 1),
                        "who": "Sam",
                        "done": False,
                        "completion_date": None,
                    }
                ],
            },
        )
        self.assertIn("6.4 Site Security Action Plan Form", html)
        self.assertIn("Workplace Violence and Harassment Prevention", html)
        self.assertIn("Site Number", html)
        self.assertIn("Completed By", html)
        self.assertIn("Position", html)
        self.assertIn("Completed on", html)
        self.assertIn("ACTION ITEM", html)
        self.assertIn("ACTION REQUIRED", html)
        self.assertIn("WHEN", html)
        self.assertIn("WHO", html)
        self.assertIn("COMPLETION DATE", html)
        self.assertIn("Door lock", html)
        self.assertIn("Replace the lock", html)
        self.assertIn("2026-10-01", html)
        self.assertIn("Sam", html)
        self.assertNotIn("Question", html)

    def test_empty_plan_says_there_are_no_action_items(self):
        html = render_to_string(
            "quiz/action_plan_pdf.html",
            {
                "form_title": "6.4 Site Security Action Plan Form",
                "form_subtitle": "Workplace Violence and Harassment Prevention",
                "source_title": "",
                "document_id": "",
                "site_location": "",
                "completed_by": "",
                "position": "",
                "completed_on": "",
                "rows": [],
            },
        )
        self.assertIn("No action items.", html)

    def _checklist_pdf(self, include_action_plan):
        context = {
            "checklist": SimpleNamespace(
                title="Workplace Inspection",
                created_by="Pat",
                submitted_by=None,
                notes="",
                submitted_at=None,
            ),
            "items": [
                {
                    "text": "Are locks working?",
                    "result": "no",
                    "answer": "N",
                    "responsibility": "L",
                    "text_value": "",
                    "comment": "",
                    "photo_url": None,
                    "action": SimpleNamespace(
                        action_required="Replace the lock",
                        action_item="Are locks working?",
                        who="Sam",
                        target_date=date(2026, 10, 1),
                    ),
                }
            ],
            "action_items": [
                SimpleNamespace(
                    action_item="Are locks working?",
                    action_required="Replace the lock",
                    who="Sam",
                    target_date=date(2026, 10, 1),
                )
            ],
            "store_number": "12",
            "store_name": "",
            "store_address_line": "",
        }
        if include_action_plan is not None:
            context["include_action_plan"] = include_action_plan
        return render_to_string("quiz/checklist_pdf.html", context)

    def test_split_checklist_pdf_drops_the_action_plan(self):
        html = self._checklist_pdf(False)
        self.assertIn("Are locks working?", html)
        self.assertNotIn("Action plan summary", html)
        self.assertNotIn("Replace the lock", html)

    def test_other_checklist_pdf_still_includes_the_action_plan(self):
        html = self._checklist_pdf(True)
        self.assertIn("Action plan summary", html)
        self.assertIn("Replace the lock", html)
        omitted_flag = self._checklist_pdf(None)
        self.assertIn("Action plan summary", omitted_flag)


class ActionPlanHeaderTests(SimpleTestCase):
    def test_header_uses_site_header_answers(self):
        checklist = SimpleNamespace(
            title="Workplace Inspection Checklist (BC & ON)",
            template=SimpleNamespace(name="Workplace Inspection", document_id=""),
            store=None,
            submitted_by=None,
            created_by=None,
            submitted_at=None,
            items=[
                SimpleNamespace(
                    section="Site header",
                    response_type="text",
                    text="Site Number & Location",
                    text_value="4411 Main",
                ),
                SimpleNamespace(
                    section="Site header",
                    response_type="text",
                    text="Completed By (Name)",
                    text_value="Ada Lovelace",
                ),
                SimpleNamespace(
                    section="Site header",
                    response_type="text",
                    text="Position",
                    text_value="Manager",
                ),
                SimpleNamespace(
                    section="Site header",
                    response_type="date",
                    text="Completed on (Date)",
                    text_value="2026-09-28",
                ),
                SimpleNamespace(
                    section="Workers Working Alone",
                    response_type="text",
                    text="What does this supervision involve?",
                    text_value="Radio check",
                ),
            ],
            action_items=[
                SimpleNamespace(
                    action_item="Door lock",
                    action_required="Replace the lock",
                    target_date=date(2026, 10, 1),
                    who="Sam",
                    status="open",
                    completion_date=None,
                )
            ],
        )
        context = action_plan_pdf_context(checklist)
        self.assertEqual(context["site_location"], "4411 Main")
        self.assertEqual(context["completed_by"], "Ada Lovelace")
        self.assertEqual(context["position"], "Manager")
        self.assertEqual(context["completed_on"], "2026-09-28")
        self.assertEqual(context["rows"][0]["action_required"], "Replace the lock")
        self.assertFalse(context["rows"][0]["done"])


def _user(employer, username, email, phone):
    return CustomUser.objects.create_user(
        username=username,
        email=email,
        password="pass12345",
        phone_number=phone,
        employer=employer,
        first_name=username.title(),
        last_name="Lee",
    )


@override_settings(
    SECRET_KEY="ci-test-secret-key-not-for-production",
    MAIL_DEFAULT_SENDER="noreply@example.com",
)
class SplitDeliveryTaskTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Canada")
        other = Employer.objects.create(name="Other Co")
        self.inspector = _user(
            self.employer, "inspector", "inspector@example.com", "+15195550101"
        )
        self.checklist_recipient = _user(
            self.employer, "quiz", "quiz@example.com", "+15195550102"
        )
        self.plan_recipient = _user(
            self.employer, "plans", "plans@example.com", "+15195550103"
        )
        outsider = _user(other, "outsider", "outsider@example.com", "+15195550104")
        quiz_group = Group.objects.create(name=CHECKLIST_EMAIL_GROUP)
        plan_group, _created = Group.objects.get_or_create(name=ACTION_PLAN_EMAIL_GROUP)
        self.checklist_recipient.groups.add(quiz_group)
        self.plan_recipient.groups.add(plan_group)
        outsider.groups.add(quiz_group, plan_group)

        self.store = Store.objects.create(
            number=12,
            employer=self.employer,
            address="123 Main St",
            city="London",
            province="ON",
        )
        self.template = ChecklistTemplate.objects.create(
            name="Workplace Inspection Checklist (BC & ON)",
            document_id="ENMCDS840-6.3",
        )
        self.checklist = self._checklist(self.template, "Store 12 inspection")
        self._header_and_action(self.checklist)

    def _checklist(self, template, title):
        return Checklist.objects.create(
            title=title,
            template=template,
            created_by=self.inspector,
            submitted_by=self.inspector,
            submitted_at=timezone.now(),
            store=self.store,
            status="submitted",
        )

    def _header_and_action(self, checklist, action_plan_form="ENMCDS840-6.4"):
        template = checklist.template
        header = ChecklistTemplateItem.objects.create(
            template=template,
            text="Site Number & Location",
            section="Site header",
            response_type=ChecklistTemplateItem.RESPONSE_TEXT,
            responsibility_assignable=False,
            create_action_on=[],
            order=0,
        )
        ChecklistItem.objects.create(
            checklist=checklist,
            template_item=header,
            text="Site Number & Location",
            section="Site header",
            text_value="12 — 123 Main St",
            order=0,
        )
        question = ChecklistTemplateItem.objects.create(
            template=template,
            text="Are locks working?",
            response_type=ChecklistTemplateItem.RESPONSE_YES_NO_NA,
            responsibility_assignable=True,
            create_action_on=["N"],
            action_plan_form=action_plan_form,
            order=1,
        )
        item = ChecklistItem.objects.create(
            checklist=checklist,
            template_item=question,
            text="Are locks working?",
            result=ChecklistItem.RESULT_NO,
            responsibility="L",
            order=1,
        )
        ChecklistActionItem.objects.create(
            checklist=checklist,
            checklist_item=item,
            action_item="Are locks working?",
            action_required="Replace the lock",
            who="Sam",
            target_date=date(2026, 10, 1),
        )

    def _patches(self):
        response = MagicMock()
        response.content = b"%PDF-checklist"
        response.raise_for_status = MagicMock()
        return (
            patch(
                "arl.quiz.tasks.generate_fresh_checklist_pdf",
                return_value="TEMP/fresh.pdf",
            ),
            patch(
                "arl.quiz.tasks.get_signed_url_for_key",
                return_value="https://example.test/fresh.pdf",
            ),
            patch("arl.quiz.tasks.requests.get", return_value=response),
            patch(
                "arl.quiz.tasks.master_upload_file_to_dropbox",
                return_value=(True, "ok"),
            ),
            patch(
                "arl.quiz.tasks.pdfkit.from_string",
                return_value=b"%PDF-action-plan",
            ),
            patch(
                "arl.msg.helpers.create_master_email",
                return_value=True,
            ),
        )

    def test_submit_uploads_and_emails_checklist_and_action_plan_separately(self):
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3] as upload, patches[
            4
        ] as pdfkit, patches[5] as email:
            result = generate_checklist_pdf_task.run(self.checklist.id)

        self.assertEqual(result["status"], "success")
        paths = [call.args[1] for call in upload.call_args_list]
        self.assertEqual(len(paths), 2)
        checklist_path = paths[0]
        plan_path = paths[1]
        self.assertNotIn("action-plan", checklist_path)
        self.assertTrue(plan_path.endswith("-action-plan.pdf"))
        self.assertEqual(
            plan_path,
            checklist_path.replace(".pdf", "-action-plan.pdf"),
        )
        self.assertIn(
            "/CHECKLISTS/petro-canada/workplace-inspection-checklist-bc-on/12/",
            checklist_path,
        )
        plan_html = pdfkit.call_args.args[0]
        self.assertIn("6.4 Site Security Action Plan Form", plan_html)
        self.assertIn("Replace the lock", plan_html)
        self.assertIn("12 — 123 Main St", plan_html)
        self.assertNotIn("Are locks working?</strong>", plan_html)

        self.assertEqual(email.call_count, 2)
        checklist_mail, plan_mail = email.call_args_list
        self.assertEqual(
            checklist_mail.kwargs["template_data"]["subject"],
            f"Checklist #{self.checklist.id} PDF",
        )
        self.assertEqual(
            plan_mail.kwargs["template_data"]["subject"],
            f"Action plan #{self.checklist.id} PDF",
        )
        self.assertEqual(
            checklist_mail.kwargs["to_email"],
            [self.checklist_recipient.email],
        )
        self.assertEqual(plan_mail.kwargs["to_email"], [self.plan_recipient.email])
        self.assertTrue(
            checklist_mail.kwargs["attachments"][0]["filename"].endswith(".pdf")
        )
        self.assertNotIn(
            "action-plan", checklist_mail.kwargs["attachments"][0]["filename"]
        )
        self.assertTrue(
            plan_mail.kwargs["attachments"][0]["filename"].endswith("-action-plan.pdf")
        )
        self.assertNotEqual(
            checklist_mail.kwargs["attachments"][0]["content"],
            plan_mail.kwargs["attachments"][0]["content"],
        )

    def test_other_checklist_still_sends_one_combined_pdf(self):
        other = ChecklistTemplate.objects.create(name="Fall Winter Exterior Checklist")
        checklist = self._checklist(other, "Fall Winter Exterior Checklist")
        self._header_and_action(checklist, action_plan_form="")
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3] as upload, patches[
            4
        ] as pdfkit, patches[5] as email:
            result = generate_checklist_pdf_task.run(checklist.id)

        self.assertEqual(result["status"], "success")
        self.assertEqual(upload.call_count, 1)
        self.assertNotIn("action-plan", upload.call_args.args[1])
        pdfkit.assert_not_called()
        self.assertEqual(email.call_count, 1)
        self.assertEqual(
            email.call_args.kwargs["to_email"],
            [self.checklist_recipient.email],
        )

    def test_fresh_checklist_pdf_for_these_forms_omits_the_action_plan(self):
        captured = {}

        def fake_pdf(html, *_args, **_kwargs):
            captured["html"] = html
            return b"%PDF-checklist"

        with patch("arl.quiz.tasks.pdfkit.from_string", side_effect=fake_pdf), patch(
            "arl.quiz.tasks.upload_to_linode_object_storage"
        ):
            from arl.quiz.tasks import generate_fresh_checklist_pdf

            key = generate_fresh_checklist_pdf(self.checklist.id)

        self.assertTrue(key)
        self.assertIn("Are locks working?", captured["html"])
        self.assertNotIn("Action plan summary", captured["html"])
        self.assertNotIn("Replace the lock", captured["html"])

    def test_action_plan_group_exists_for_role_assignment(self):
        self.assertTrue(Group.objects.filter(name=ACTION_PLAN_EMAIL_GROUP).exists())

    def test_direct_delivery_uses_the_action_plan_role_only(self):
        with patch(
            "arl.quiz.tasks.pdfkit.from_string", return_value=b"%PDF-action-plan"
        ), patch(
            "arl.quiz.tasks.master_upload_file_to_dropbox", return_value=(True, "ok")
        ) as upload, patch(
            "arl.msg.helpers.create_master_email", return_value=True
        ) as email:
            path = deliver_action_plan_pdf(
                self.checklist, "12", when=datetime(2026, 9, 20)
            )

        self.assertTrue(path.endswith("-action-plan.pdf"))
        upload.assert_called_once()
        self.assertEqual(
            email.call_args.kwargs["to_email"], [self.plan_recipient.email]
        )
        self.assertNotIn(
            self.checklist_recipient.email, email.call_args.kwargs["to_email"]
        )

    def test_store_employer_is_used_when_creator_and_submitter_have_none(self):
        self.inspector.employer = None
        self.inspector.save(update_fields=["employer"])
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3] as upload, patches[
            4
        ], patches[5] as email:
            result = generate_checklist_pdf_task.run(self.checklist.id)

        self.assertEqual(result["status"], "success")
        paths = [call.args[1] for call in upload.call_args_list]
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertIn("/CHECKLISTS/petro-canada/", path)
            self.assertNotIn("/no-company/", path)
        self.assertEqual(email.call_count, 2)
        self.assertEqual(
            email.call_args_list[0].kwargs["to_email"],
            [self.checklist_recipient.email],
        )
        self.assertEqual(
            email.call_args_list[1].kwargs["to_email"],
            [self.plan_recipient.email],
        )


class ChecklistEmployerEmailTests(SimpleTestCase):
    def test_skips_email_only_when_creator_submitter_and_store_have_no_employer(self):
        checklist = SimpleNamespace(
            created_by=None,
            submitted_by=SimpleNamespace(employer=None),
            store=None,
        )
        with patch("arl.msg.helpers.create_master_email") as send:
            sent = email_pdf_to_employer_group(
                checklist=checklist,
                pdf_bytes=b"%PDF",
                filename="checklist.pdf",
                group_name=CHECKLIST_EMAIL_GROUP,
                subject="Checklist",
                log_label="Checklist Email Task",
            )
        self.assertFalse(sent)
        send.assert_not_called()
