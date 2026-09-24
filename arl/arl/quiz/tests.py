from unittest.mock import patch
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase as SimpleTestCase

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from arl.quiz.forms import ChecklistItemForm, ChecklistItemFormSet
from arl.user.models import CustomUser, Employer, Store
from arl.quiz.importers import import_checklist_template, load_checklist_payload
from arl.quiz.models import (
    Checklist,
    ChecklistActionItem,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateItem,
)


class ChecklistModelTests(TestCase):
    def setUp(self):
        self.template = ChecklistTemplate.objects.create(
            name="Workplace Inspection",
            document_id="ENMCDS840-6.3",
        )
        self.yes_no = ChecklistTemplateItem.objects.create(
            template=self.template,
            item_code="WI-005",
            section="Worker Training and Safety",
            text="Have young or new workers been provided with orientation?",
            response_type=ChecklistTemplateItem.RESPONSE_YES_NO_NA,
            responsibility_assignable=True,
            create_action_on=["N"],
            order=1,
        )
        self.header = ChecklistTemplateItem.objects.create(
            template=self.template,
            item_code="WI-001",
            section="Site header",
            text="Site Number & Location",
            response_type=ChecklistTemplateItem.RESPONSE_TEXT,
            required=True,
            responsibility_assignable=False,
            create_action_on=[],
            order=0,
        )
        self.checklist = Checklist.objects.create(title="Store 12 inspection")
        self.item = ChecklistItem.objects.create(
            checklist=self.checklist,
            template_item=self.yes_no,
            text=self.yes_no.text,
            section=self.yes_no.section,
            order=1,
        )
        self.header_item = ChecklistItem.objects.create(
            checklist=self.checklist,
            template_item=self.header,
            text=self.header.text,
            section=self.header.section,
            order=0,
        )

    def test_answer_codes_map_to_y_n_na(self):
        self.item.result = ChecklistItem.RESULT_YES
        self.assertEqual(self.item.answer, "Y")
        self.item.result = ChecklistItem.RESULT_NO
        self.assertEqual(self.item.answer, "N")
        self.item.result = ChecklistItem.RESULT_NA
        self.assertEqual(self.item.answer, "N/A")

    def test_n_creates_action_and_requires_l_or_s(self):
        self.item.result = ChecklistItem.RESULT_NO
        self.assertTrue(self.item.creates_action())
        self.assertTrue(self.item.needs_responsibility())
        errors = self.item.submit_errors()
        self.assertTrue(any("L or S" in message for message in errors))
        self.assertTrue(any("action plan" in message for message in errors))

    def test_na_does_not_require_responsibility_or_action(self):
        self.item.result = ChecklistItem.RESULT_NA
        self.assertFalse(self.item.creates_action())
        self.assertFalse(self.item.needs_responsibility())
        self.assertEqual(self.item.submit_errors(), [])

    def test_yes_does_not_require_responsibility_or_action(self):
        self.item.result = ChecklistItem.RESULT_YES
        self.item.responsibility = ""
        self.assertFalse(self.item.creates_action())
        self.assertFalse(self.item.needs_responsibility())
        self.assertEqual(self.item.submit_errors(), [])

    def test_action_on_yes_requires_follow_up_but_no_does_not(self):
        template_item = self.item.template_item
        template_item.create_action_on = ["Y"]
        template_item.save(update_fields=["create_action_on"])
        self.item.refresh_from_db()

        self.item.result = ChecklistItem.RESULT_YES
        self.assertTrue(self.item.creates_action())
        self.assertTrue(self.item.needs_responsibility())
        yes_errors = self.item.submit_errors()
        self.assertTrue(any("L or S" in message for message in yes_errors))
        self.assertTrue(any("action plan" in message for message in yes_errors))

        self.item.result = ChecklistItem.RESULT_NO
        self.item.responsibility = ""
        self.assertFalse(self.item.creates_action())
        self.assertFalse(self.item.needs_responsibility())
        self.assertEqual(self.item.submit_errors(), [])

    def test_complete_action_item_allows_submit(self):
        self.item.result = ChecklistItem.RESULT_NO
        self.item.responsibility = ChecklistItem.RESPONSIBILITY_S
        self.item.save()
        action = ChecklistActionItem.objects.create(
            checklist=self.checklist,
            checklist_item=self.item,
            action_item=self.item.text,
            action_required="Install orientation records binder",
            who="Store manager",
            target_date=date(2026, 3, 1),
        )
        self.assertTrue(action.is_ready_for_submit())
        self.assertEqual(self.item.submit_errors(), [])
        self.header_item.text_value = "Store 12"
        self.header_item.save(update_fields=["text_value"])
        self.assertTrue(self.checklist.can_submit())

    def test_new_item_result_defaults_blank_not_no(self):
        fresh = ChecklistItem(checklist=self.checklist, text="Fresh")
        self.assertEqual(fresh.result, "")


class ChecklistFormTests(TestCase):
    def setUp(self):
        template = ChecklistTemplate.objects.create(name="Inspection")
        self.template_item = ChecklistTemplateItem.objects.create(
            template=template,
            text="Are panic buttons working?",
            create_action_on=["N"],
            responsibility_assignable=True,
        )
        self.checklist = Checklist.objects.create(title="Draft inspection")
        self.item = ChecklistItem.objects.create(
            checklist=self.checklist,
            template_item=self.template_item,
            text=self.template_item.text,
            order=1,
        )

    def _form(self, data, validate_submit=True):
        return ChecklistItemForm(
            data,
            instance=self.item,
            validate_submit=validate_submit,
        )

    def test_draft_can_save_n_without_action_fields(self):
        form = self._form(
            {
                "result": "no",
                "responsibility": "L",
                "comment": "",
                "order": 1,
            },
            validate_submit=False,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_submit_requires_action_fields_for_n(self):
        form = self._form(
            {
                "result": "no",
                "responsibility": "L",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("action_required", form.errors)
        self.assertIn("who", form.errors)
        self.assertIn("target_date", form.errors)
        self.assertTrue(
            any("action plan" in str(message) for message in form.errors["action_required"])
        )
        self.assertIn("is-invalid", form["action_required"].as_widget())
        self.assertIn("is-invalid", form["who"].as_widget())

    def test_yes_submit_allows_blank_responsibility(self):
        form = self._form(
            {
                "result": "yes",
                "responsibility": "",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_n_submit_requires_responsibility_when_action_on_n(self):
        form = self._form(
            {
                "result": "no",
                "responsibility": "",
                "comment": "",
                "order": 1,
                "action_required": "Replace dead panic button",
                "who": "Facilities",
                "target_date": "2026-04-15",
            },
            validate_submit=True,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("responsibility", form.errors)
        self.assertTrue(
            any("L or S" in str(message) for message in form.errors["responsibility"])
        )
        self.assertNotIn("action_required", form.errors)

    def test_n_without_action_is_valid_when_create_action_on_excludes_n(self):
        self.template_item.create_action_on = []
        self.template_item.save(update_fields=["create_action_on"])
        self.item.refresh_from_db()
        form = self._form(
            {
                "result": "no",
                "responsibility": "",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_create_action_on_yes_requires_action_for_y_not_n(self):
        self.template_item.create_action_on = ["Y"]
        self.template_item.save(update_fields=["create_action_on"])
        self.item.refresh_from_db()
        yes_form = self._form(
            {
                "result": "yes",
                "responsibility": "L",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertFalse(yes_form.is_valid())
        self.assertIn("action_required", yes_form.errors)
        self.assertIn("who", yes_form.errors)
        self.assertIn("target_date", yes_form.errors)

        no_form = self._form(
            {
                "result": "no",
                "responsibility": "",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertTrue(no_form.is_valid(), no_form.errors)

    def test_na_submit_does_not_require_ls_or_action(self):
        form = self._form(
            {
                "result": "na",
                "responsibility": "",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_error_summary_lists_failed_items(self):
        other = ChecklistItem.objects.create(
            checklist=self.checklist,
            template_item=self.template_item,
            text="Fire extinguisher tagged?",
            order=2,
        )
        data = {
            "items-TOTAL_FORMS": "2",
            "items-INITIAL_FORMS": "2",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(self.item.pk),
            "items-0-order": "1",
            "items-0-result": "no",
            "items-0-responsibility": "",
            "items-0-comment": "",
            "items-1-id": str(other.pk),
            "items-1-order": "2",
            "items-1-result": "yes",
            "items-1-responsibility": "",
            "items-1-comment": "",
        }
        formset = ChecklistItemFormSet(
            data,
            instance=self.checklist,
            form_kwargs={"validate_submit": True},
        )
        self.assertFalse(formset.is_valid())
        summary = formset.item_error_summaries()
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["need_ls"], 1)
        self.assertEqual(summary["need_action"], 1)
        self.assertEqual(summary["rows"][0]["pk"], self.item.pk)
        self.assertIn("responsibility", summary["rows"][0]["kinds"])
        self.assertIn("action", summary["rows"][0]["kinds"])

    def test_unbound_formset_has_empty_error_summary(self):
        formset = ChecklistItemFormSet(instance=self.checklist)
        summary = formset.item_error_summaries()
        self.assertEqual(summary["count"], 0)
        self.assertEqual(summary["rows"], [])

    def test_submit_saves_action_item_from_n(self):
        form = self._form(
            {
                "result": "no",
                "responsibility": "S",
                "comment": "",
                "order": 1,
                "action_item": "Are panic buttons working?",
                "action_required": "Replace dead panic button",
                "who": "Facilities",
                "target_date": "2026-04-15",
                "action_note": "Photo on item",
            },
            validate_submit=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        action = form.save_action_item()
        self.assertIsNotNone(action)
        self.assertEqual(action.who, "Facilities")
        self.assertEqual(action.action_required, "Replace dead panic button")
        self.assertEqual(self.checklist.action_items.count(), 1)

    def test_changing_n_to_y_deletes_action_item(self):
        ChecklistActionItem.objects.create(
            checklist=self.checklist,
            checklist_item=self.item,
            action_item=self.item.text,
            action_required="Old plan",
            who="Someone",
            target_date=date(2026, 4, 1),
        )
        form = self._form(
            {
                "result": "yes",
                "responsibility": "L",
                "comment": "",
                "order": 1,
            },
            validate_submit=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form.save_action_item()
        self.assertEqual(self.checklist.action_items.count(), 0)

    def test_formset_accepts_validate_submit_kwarg(self):
        formset = ChecklistItemFormSet(
            instance=self.checklist,
            form_kwargs={"validate_submit": True},
        )
        self.assertTrue(formset.forms[0].validate_submit)

    def test_form_renders_answer_pills_and_action_fields(self):
        form = ChecklistItemForm(instance=self.item)
        html = form.as_p()
        self.assertIn('value="yes"', html)
        self.assertIn('value="no"', html)
        self.assertIn('value="na"', html)
        self.assertIn("action_required", html)
        self.assertIn('name="who"', html)
        self.assertIn("target_date", html)
        labels = [choice[1] for choice in form.fields["result"].choices]
        self.assertEqual(labels, ["Y", "N", "N/A"])


class ChecklistImportTests(TestCase):
    def test_import_bundled_workplace_inspection_json(self):
        payload = load_checklist_payload()
        template = import_checklist_template(payload)
        self.assertEqual(template.document_id, "ENMCDS840-6.3")
        self.assertEqual(template.items.count(), payload["item_count"])
        yes_no = template.items.filter(
            response_type=ChecklistTemplateItem.RESPONSE_YES_NO_NA
        )
        self.assertTrue(yes_no.exists())
        first_yes_no = yes_no.order_by("order").first()
        self.assertEqual(first_yes_no.create_action_on, ["N"])
        self.assertTrue(first_yes_no.responsibility_assignable)
        header = template.items.get(item_code="WI-001")
        self.assertEqual(header.response_type, ChecklistTemplateItem.RESPONSE_TEXT)
        self.assertEqual(header.create_action_on, [])
        self.assertFalse(header.responsibility_assignable)

    def test_management_command_imports_default_json(self):
        call_command("import_checklist_template")
        self.assertTrue(
            ChecklistTemplate.objects.filter(document_id="ENMCDS840-6.3").exists()
        )


class StoreReportContextTests(SimpleTestCase):
    def test_joins_nonempty_address_parts(self):
        from .store_address import store_report_context

        store = SimpleNamespace(
            number=123,
            name=None,
            address="123 Main St",
            address_two="",
            city="London",
            province="ON",
        )
        ctx = store_report_context(store)
        self.assertEqual(ctx["store_number"], 123)
        self.assertEqual(ctx["store_address_line"], "123 Main St, London, ON")

    def test_empty_when_store_missing(self):
        from .store_address import store_report_context

        ctx = store_report_context(None)
        self.assertEqual(ctx["store_address_line"], "")
        self.assertIsNone(ctx["store_number"])

    def test_omits_blank_address_two(self):
        from .store_address import format_store_address_line

        store = SimpleNamespace(
            address="123 Main St",
            address_two="  ",
            city="London",
            province="ON",
        )
        self.assertEqual(
            format_store_address_line(store), "123 Main St, London, ON"
        )


class DropboxChecklistPathTests(SimpleTestCase):
    def test_path_is_company_then_checklist_then_store_then_date(self):
        from .dropbox_paths import build_checklist_dropbox_path

        when = datetime(2026, 9, 20)
        checklist = SimpleNamespace(
            id=42,
            slug="store-12-inspection-ab12cd34",
            title="Store 12 inspection",
            template=SimpleNamespace(name="Workplace Inspection Checklist (BC & ON)"),
            created_by=SimpleNamespace(
                employer=SimpleNamespace(name="Petro Canada")
            ),
        )
        path, folder = build_checklist_dropbox_path(checklist, "12", when=when)
        pdf_filename = path.rsplit("/", 1)[-1]
        self.assertEqual(folder, "workplace-inspection-checklist-bc-on")
        self.assertEqual(pdf_filename, "12_store-12-inspection-ab12cd34-42.pdf")
        self.assertEqual(
            path,
            "/CHECKLISTS/petro-canada/workplace-inspection-checklist-bc-on/"
            f"12/2026/09-September/{pdf_filename}",
        )

    def test_falls_back_to_title_when_template_missing(self):
        from .dropbox_paths import build_checklist_dropbox_path

        checklist = SimpleNamespace(
            id=7,
            slug="",
            title="Fall Winter Exterior Checklist",
            template=None,
            created_by=SimpleNamespace(employer=None),
        )
        path, folder = build_checklist_dropbox_path(
            checklist, "no-store", when=datetime(2026, 2, 1)
        )
        self.assertEqual(folder, "fall-winter-exterior-checklist")
        self.assertTrue(
            path.startswith(
                "/CHECKLISTS/no-company/fall-winter-exterior-checklist/no-store/2026/02-February/"
            )
        )


class ChecklistEditTemplateTests(SimpleTestCase):
    def test_edit_template_has_per_item_error_hooks(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "quiz"
            / "checklist_edit.html"
        )
        text = template.read_text()
        self.assertIn("id=\"checklist-error-summary\"", text)
        self.assertIn("has-item-errors", text)
        self.assertIn("data-item-error", text)
        self.assertIn("focusFirstChecklistError", text)
        self.assertIn("L/S", text)
        self.assertIn("item_error_summary.rows", text)
        self.assertIn("alert-success.alert-dismissible", text)
        self.assertIn("function applyLiveItemErrors", text)
        self.assertIn("function refreshErrorSummary", text)
        self.assertIn("data-error-kind", text)
        self.assertIn("id=\"checklist-edit-form\"", text)
        self.assertNotIn('enctype="multipart/form-data"', text)
        self.assertIn("function checklistFormData", text)
        self.assertIn("data-action=\"submit\"", text)
        self.assertIn("id=\"checklist-form-action\"", text)
        self.assertIn("fd.delete('action')", text)
        self.assertIn("data.ok", text)
        self.assertIn("creates.includes(answerCode)", text)
        self.assertNotIn("answer === YES || answer === NO", text)
        self.assertNotIn("(required for Y/N)", text)
        self.assertIn("function clearAction", text)


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class ChecklistMobileSubmitTests(TestCase):
    """80-item submit must not 400; Django TooManyFieldsSent is the mobile failure mode."""

    ITEM_COUNT = 80

    def setUp(self):
        employer = Employer.objects.create(name="Petro Test")
        self.user = CustomUser.objects.create_user(
            username="inspector",
            email="inspector@example.com",
            password="pass12345",
            phone_number="+15196707469",
            employer=employer,
        )
        self.store = Store.objects.create(
            number=12,
            employer=employer,
            address="123 Main St",
            city="London",
            province="ON",
        )
        template = ChecklistTemplate.objects.create(name="Workplace Inspection")
        self.checklist = Checklist.objects.create(
            title="Store 12 inspection",
            created_by=self.user,
            store=self.store,
            status="draft",
        )
        for i in range(self.ITEM_COUNT):
            ti = ChecklistTemplateItem.objects.create(
                template=template,
                text=f"Item {i + 1}",
                response_type=ChecklistTemplateItem.RESPONSE_YES_NO_NA,
                responsibility_assignable=True,
                create_action_on=["N"],
                order=i,
            )
            ChecklistItem.objects.create(
                checklist=self.checklist,
                template_item=ti,
                text=ti.text,
                order=i,
            )
        self.client.force_login(self.user)
        self.url = reverse("checklist_edit", kwargs={"slug": self.checklist.slug})
        self.pdf_patch = patch(
            "arl.quiz.views.generate_checklist_pdf_task.delay"
        )
        self.pdf_patch.start()
        self.addCleanup(self.pdf_patch.stop)

    def _post_data(self, include_action_fields=True, responsibility="L", result="yes"):
        items = list(self.checklist.items.order_by("id"))
        data = {
            "title": self.checklist.title,
            "notes": "",
            "store": str(self.store.pk),
            "action": "submit",
            "items-TOTAL_FORMS": str(len(items)),
            "items-INITIAL_FORMS": str(len(items)),
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
        }
        for i, item in enumerate(items):
            data[f"items-{i}-id"] = str(item.pk)
            data[f"items-{i}-order"] = str(item.order)
            data[f"items-{i}-result"] = result
            data[f"items-{i}-responsibility"] = responsibility
            data[f"items-{i}-comment"] = ""
            if include_action_fields:
                data[f"items-{i}-action_item"] = ""
                data[f"items-{i}-action_required"] = ""
                data[f"items-{i}-who"] = ""
                data[f"items-{i}-target_date"] = ""
                data[f"items-{i}-action_note"] = ""
        return data

    def test_eighty_item_payload_exceeds_django_default_field_limit(self):
        """Evidence: full L/S + action-plan POST is > Django's default 1000 keys
        once radios/file parts are counted the way mobile multipart can."""
        data = self._post_data()
        # Always-posted keys for 80 items (id, order, result, L/S, comment,
        # 5 action fields) plus management + checklist + action ≈ 809.
        self.assertGreater(len(data), 800)
        # Simulated extra radio parts (3 Y/N/N/A + 2 L/S per item) push over 1000.
        simulated_mobile_parts = len(data) + (self.ITEM_COUNT * 3)
        self.assertGreater(simulated_mobile_parts, 1000)

    def test_eighty_item_submit_is_not_400(self):
        resp = self.client.post(self.url, self._post_data())
        self.assertNotEqual(resp.status_code, 400)
        self.assertEqual(resp.status_code, 302)
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "submitted")

    def test_submit_omitting_action_fields_for_yes_is_not_400(self):
        resp = self.client.post(
            self.url, self._post_data(include_action_fields=False)
        )
        self.assertNotEqual(resp.status_code, 400)
        self.assertEqual(resp.status_code, 302)

    def test_all_yes_blank_responsibility_submits(self):
        resp = self.client.post(self.url, self._post_data(responsibility=""))
        self.assertNotEqual(resp.status_code, 400)
        self.assertEqual(resp.status_code, 302)
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "submitted")

    def test_no_without_responsibility_is_validation_200_not_400(self):
        resp = self.client.post(
            self.url, self._post_data(responsibility="", result="no")
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Please fix the errors below")
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "draft")

    @override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=50)
    def test_too_many_fields_is_http_400_suspicious_operation(self):
        resp = self.client.post(self.url, self._post_data())
        self.assertEqual(resp.status_code, 400)
        self.assertNotEqual(resp.status_code, 500)


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class ChecklistAutosaveTests(TestCase):
    def setUp(self):
        employer = Employer.objects.create(name="Petro Test")
        self.user = CustomUser.objects.create_user(
            username="autosave-inspector",
            email="autosave@example.com",
            password="pass12345",
            phone_number="+15196707470",
            employer=employer,
        )
        self.store = Store.objects.create(
            number=14,
            employer=employer,
            address="14 Main St",
            city="London",
            province="ON",
        )
        template = ChecklistTemplate.objects.create(name="Workplace Inspection")
        self.template_item = ChecklistTemplateItem.objects.create(
            template=template,
            text="Are panic buttons working?",
            response_type=ChecklistTemplateItem.RESPONSE_YES_NO_NA,
            responsibility_assignable=True,
            create_action_on=["N"],
            order=1,
        )
        self.checklist = Checklist.objects.create(
            title="Store 14 inspection",
            created_by=self.user,
            store=self.store,
            status="draft",
            notes="",
        )
        self.item = ChecklistItem.objects.create(
            checklist=self.checklist,
            template_item=self.template_item,
            text=self.template_item.text,
            order=1,
        )
        self.client.force_login(self.user)
        self.url = reverse("checklist_edit", kwargs={"slug": self.checklist.slug})

    def _payload(self, **overrides):
        data = {
            "title": self.checklist.title,
            "notes": "Night shift note: closer is slow.",
            "store": str(self.store.pk),
            "action": "submit",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-id": str(self.item.pk),
            "items-0-order": "1",
            "items-0-result": "no",
            "items-0-responsibility": "S",
            "items-0-comment": "Hinge photo later",
            "items-0-action_item": "Are panic buttons working?",
            "items-0-action_required": "Replace the dead panic button",
            "items-0-who": "Facilities",
            "items-0-target_date": "2026-05-01",
            "items-0-action_note": "Waiting on parts",
        }
        data.update(overrides)
        return data

    def test_autosave_persists_notes_responsibility_and_action_fields(self):
        resp = self.client.post(
            self.url + "?autosave=1",
            self._payload(),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["ok"])
        self.assertIn("saved_at", payload)

        self.checklist.refresh_from_db()
        item = ChecklistItem.objects.get(pk=self.item.pk)
        self.assertEqual(self.checklist.status, "draft")
        self.assertEqual(self.checklist.notes, "Night shift note: closer is slow.")
        self.assertEqual(item.result, ChecklistItem.RESULT_NO)
        self.assertEqual(item.responsibility, ChecklistItem.RESPONSIBILITY_S)
        self.assertEqual(item.comment, "Hinge photo later")
        action = ChecklistActionItem.objects.get(checklist_item=item)
        self.assertIsNotNone(action)
        self.assertEqual(action.action_required, "Replace the dead panic button")
        self.assertEqual(action.who, "Facilities")
        self.assertEqual(action.target_date, date(2026, 5, 1))
        self.assertEqual(action.note, "Waiting on parts")

    def test_autosave_keeps_partial_action_plan(self):
        resp = self.client.post(
            self.url + "?autosave=1",
            self._payload(
                **{
                    "items-0-action_required": "Order a replacement",
                    "items-0-who": "",
                    "items-0-target_date": "",
                    "items-0-action_note": "",
                }
            ),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.checklist.refresh_from_db()
        self.assertEqual(self.checklist.status, "draft")
        self.assertEqual(self.checklist.notes, "Night shift note: closer is slow.")
        action = ChecklistActionItem.objects.get(checklist_item_id=self.item.pk)
        self.assertIsNotNone(action)
        self.assertEqual(action.action_required, "Order a replacement")
        self.assertEqual(action.who, "")
        self.assertIsNone(action.target_date)

    def test_autosave_clears_action_when_answer_is_not_a_trigger(self):
        ChecklistActionItem.objects.create(
            checklist=self.checklist,
            checklist_item=self.item,
            action_item=self.item.text,
            action_required="Old plan",
            who="Someone",
            target_date=date(2026, 4, 1),
        )
        resp = self.client.post(
            self.url + "?autosave=1",
            self._payload(
                **{
                    "items-0-result": "yes",
                    "items-0-responsibility": "",
                    "items-0-action_required": "",
                    "items-0-who": "",
                    "items-0-target_date": "",
                    "items-0-action_note": "",
                }
            ),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        item = ChecklistItem.objects.get(pk=self.item.pk)
        self.assertEqual(item.result, ChecklistItem.RESULT_YES)
        self.assertEqual(item.responsibility, "")
        self.assertFalse(
            ChecklistActionItem.objects.filter(checklist_item=item).exists()
        )


