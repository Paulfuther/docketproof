from datetime import date

from django.core.management import call_command
from django.test import TestCase

from arl.quiz.forms import ChecklistItemForm, ChecklistItemFormSet
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

    def test_yes_requires_responsibility_but_not_action(self):
        self.item.result = ChecklistItem.RESULT_YES
        self.item.responsibility = ChecklistItem.RESPONSIBILITY_L
        self.assertFalse(self.item.creates_action())
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
        self.assertTrue(form.non_field_errors())

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

