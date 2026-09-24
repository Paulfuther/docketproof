import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from arl.quiz.forms import (
    ChecklistTemplateItemAdminForm,
    build_create_action_on,
    split_create_action_on,
)
from arl.quiz.models import (
    Checklist,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateItem,
)
from arl.user.models import Employer

User = get_user_model()


class CreateActionOnMappingTests(TestCase):
    def test_split_and_build_cover_the_four_lists(self):
        cases = (
            ([], False, False),
            (["N"], False, True),
            (["Y"], True, False),
            (["Y", "N"], True, True),
            (["N", "Y"], True, True),
        )
        for stored, yes, no in cases:
            with self.subTest(stored=stored):
                self.assertEqual(split_create_action_on(stored), (yes, no, []))
                expected = {
                    (False, False): [],
                    (True, False): ["Y"],
                    (False, True): ["N"],
                    (True, True): ["Y", "N"],
                }[(yes, no)]
                self.assertEqual(build_create_action_on(yes, no), expected)

    def test_extra_codes_stay_after_y_and_n(self):
        yes, no, extra = split_create_action_on(["N", "CUSTOM"])
        self.assertEqual((yes, no, extra), (False, True, ["CUSTOM"]))
        self.assertEqual(
            build_create_action_on(True, False, extra),
            ["Y", "CUSTOM"],
        )

    def test_non_list_is_opaque(self):
        self.assertEqual(split_create_action_on({"bad": True}), (False, False, None))


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class ChecklistTemplateItemAdminFormTests(TestCase):
    def setUp(self):
        self.template = ChecklistTemplate.objects.create(
            name="Workplace Inspection Checklist (BC & ON)",
            document_id="ENMCDS840-6.3",
        )
        self.item = ChecklistTemplateItem.objects.create(
            template=self.template,
            item_code="WI-005",
            section="Worker Training and Safety",
            text="Have young or new workers been provided with orientation?",
            response_type=ChecklistTemplateItem.RESPONSE_YES_NO_NA,
            responsibility_assignable=True,
            create_action_on=["N"],
            order=1,
        )

    def _data(self, *, yes=False, no=False, responsibility=False, raw=None, text=None):
        data = {
            "template": str(self.template.pk),
            "item_code": "WI-005",
            "section": "Worker Training and Safety",
            "text": text or self.item.text,
            "response_type": ChecklistTemplateItem.RESPONSE_YES_NO_NA,
            "order": "1",
            "action_plan_form": "",
        }
        if yes:
            data["follow_up_on_yes"] = "on"
        if no:
            data["follow_up_on_no"] = "on"
        if responsibility:
            data["responsibility_assignable"] = "on"
        if raw is not None:
            data["create_action_on_raw"] = raw
        return data

    def test_existing_n_checks_follow_up_when_no_only(self):
        form = ChecklistTemplateItemAdminForm(instance=self.item)
        self.assertTrue(form["follow_up_on_no"].value())
        self.assertFalse(form["follow_up_on_yes"].value())
        self.assertTrue(form["responsibility_assignable"].value())
        self.assertFalse(form.shows_advanced_create_action_on)
        rendered = str(form)
        self.assertEqual(form.fields["follow_up_on_no"].label, "Follow-up when No")
        self.assertEqual(form.fields["follow_up_on_yes"].label, "Follow-up when Yes")
        self.assertEqual(
            form.fields["responsibility_assignable"].label,
            "Require L or S when that follow-up applies",
        )
        self.assertIn("checked", str(form["follow_up_on_no"]))
        self.assertNotIn("checked", str(form["follow_up_on_yes"]))
        self.assertIn("Follow-up when No", rendered)
        self.assertNotIn('name="create_action_on"', rendered)

    def test_empty_and_both_answers_load_the_matching_boxes(self):
        self.item.create_action_on = []
        neither = ChecklistTemplateItemAdminForm(instance=self.item)
        self.assertFalse(neither["follow_up_on_yes"].value())
        self.assertFalse(neither["follow_up_on_no"].value())

        self.item.create_action_on = ["Y", "N"]
        both = ChecklistTemplateItemAdminForm(instance=self.item)
        self.assertTrue(both["follow_up_on_yes"].value())
        self.assertTrue(both["follow_up_on_no"].value())

        self.item.create_action_on = ["Y"]
        yes_only = ChecklistTemplateItemAdminForm(instance=self.item)
        self.assertTrue(yes_only["follow_up_on_yes"].value())
        self.assertFalse(yes_only["follow_up_on_no"].value())

    def test_new_item_defaults_to_follow_up_when_no(self):
        form = ChecklistTemplateItemAdminForm(
            instance=ChecklistTemplateItem(template=self.template, text="New check")
        )
        self.assertTrue(form["follow_up_on_no"].value())
        self.assertFalse(form["follow_up_on_yes"].value())
        self.assertTrue(form["responsibility_assignable"].value())

    def test_save_writes_each_create_action_on_list(self):
        expectations = (
            (False, False, []),
            (True, False, ["Y"]),
            (False, True, ["N"]),
            (True, True, ["Y", "N"]),
        )
        for yes, no, expected in expectations:
            with self.subTest(expected=expected):
                form = ChecklistTemplateItemAdminForm(
                    data=self._data(yes=yes, no=no, responsibility=True),
                    instance=self.item,
                )
                self.assertTrue(form.is_valid(), form.errors)
                saved = form.save()
                self.assertEqual(saved.create_action_on, expected)
                self.assertTrue(saved.responsibility_assignable)

    def test_require_l_can_be_off_while_yes_still_opens_a_follow_up(self):
        form = ChecklistTemplateItemAdminForm(
            data=self._data(yes=True, no=False, responsibility=False),
            instance=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.create_action_on, ["Y"])
        self.assertFalse(saved.responsibility_assignable)

        checklist = Checklist.objects.create(title="Store 12 inspection")
        answer = ChecklistItem.objects.create(
            checklist=checklist,
            template_item=saved,
            text=saved.text,
            result=ChecklistItem.RESULT_YES,
        )
        self.assertTrue(answer.creates_action())
        self.assertFalse(answer.needs_responsibility())
        self.assertFalse(any("L or S" in message for message in answer.submit_errors()))

        answer.result = ChecklistItem.RESULT_NO
        self.assertFalse(answer.creates_action())
        self.assertFalse(answer.needs_responsibility())

    def test_follow_up_on_no_with_require_l_still_skips_l_for_yes(self):
        form = ChecklistTemplateItemAdminForm(
            data=self._data(yes=False, no=True, responsibility=True),
            instance=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        checklist = Checklist.objects.create(title="Site security")
        answer = ChecklistItem.objects.create(
            checklist=checklist,
            template_item=saved,
            text=saved.text,
            result=ChecklistItem.RESULT_YES,
        )
        self.assertFalse(answer.needs_responsibility())
        self.assertEqual(answer.submit_errors(), [])
        answer.result = ChecklistItem.RESULT_NO
        self.assertTrue(answer.needs_responsibility())
        self.assertTrue(any("L or S" in message for message in answer.submit_errors()))

    def test_extra_code_is_preserved_when_the_inline_does_not_post_raw_json(self):
        self.item.create_action_on = ["N", "CUSTOM"]
        self.item.save(update_fields=["create_action_on"])
        form = ChecklistTemplateItemAdminForm(
            data=self._data(no=True, responsibility=True),
            instance=self.item,
        )
        self.assertTrue(form.shows_advanced_create_action_on)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.create_action_on, ["N", "CUSTOM"])

    def test_advanced_json_updates_extra_codes_and_checkboxes_set_y_n(self):
        self.item.create_action_on = ["N", "CUSTOM"]
        self.item.save(update_fields=["create_action_on"])
        form = ChecklistTemplateItemAdminForm(
            data=self._data(yes=True, responsibility=False, raw='["OTHER"]'),
            instance=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.create_action_on, ["Y", "OTHER"])
        self.assertFalse(saved.responsibility_assignable)

    def test_bad_advanced_json_is_rejected(self):
        self.item.create_action_on = ["N", "CUSTOM"]
        self.item.save(update_fields=["create_action_on"])
        form = ChecklistTemplateItemAdminForm(
            data=self._data(no=True, raw="not-json"),
            instance=self.item,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("create_action_on_raw", form.errors)

    def test_opaque_json_is_left_alone_unless_the_advanced_field_is_posted(self):
        self.item.create_action_on = {"unexpected": True}
        self.item.save(update_fields=["create_action_on"])
        form = ChecklistTemplateItemAdminForm(
            data=self._data(yes=True, no=True),
            instance=self.item,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.create_action_on, {"unexpected": True})


def _checkbox_checked(html, name):
    match = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', html)
    if match is None:
        return None
    return "checked" in match.group(0)


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class ChecklistTemplateItemAdminViewTests(TestCase):
    def setUp(self):
        employer = Employer.objects.create(name="Petro Test")
        self.user = User.objects.create_superuser(
            username="checklist-admin",
            email="checklist-admin@example.com",
            password="pass12345",
            phone_number="+15195550199",
            employer=employer,
        )
        self.client.force_login(self.user)
        self.template = ChecklistTemplate.objects.create(
            name="Workplace Inspection Checklist (BC & ON)",
            document_id="ENMCDS840-6.3",
        )
        self.on_no = ChecklistTemplateItem.objects.create(
            template=self.template,
            item_code="WI-005",
            section="Worker Training and Safety",
            text="Have young or new workers been provided with orientation?",
            responsibility_assignable=True,
            create_action_on=["N"],
            order=2,
        )
        self.neither = ChecklistTemplateItem.objects.create(
            template=self.template,
            item_code="SS-001",
            section="Site header",
            text="Site Number & Location",
            response_type=ChecklistTemplateItem.RESPONSE_TEXT,
            responsibility_assignable=False,
            create_action_on=[],
            order=1,
        )

    def test_template_change_page_shows_checkboxes_for_existing_n_items(self):
        url = reverse("admin:quiz_checklisttemplate_change", args=[self.template.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Follow-up when Yes", html)
        self.assertIn("Follow-up when No", html)
        self.assertIn("Require L or S when that follow-up applies", html)
        self.assertNotIn('name="items-0-create_action_on"', html)
        self.assertNotIn('name="items-1-create_action_on"', html)

        # Meta ordering is order, id. Site header is first, the ["N"] item second.
        self.assertIs(False, _checkbox_checked(html, "items-0-follow_up_on_yes"))
        self.assertIs(False, _checkbox_checked(html, "items-0-follow_up_on_no"))
        self.assertIs(
            False, _checkbox_checked(html, "items-0-responsibility_assignable")
        )
        self.assertIs(False, _checkbox_checked(html, "items-1-follow_up_on_yes"))
        self.assertIs(True, _checkbox_checked(html, "items-1-follow_up_on_no"))
        self.assertIs(
            True, _checkbox_checked(html, "items-1-responsibility_assignable")
        )

    def test_template_admin_save_writes_follow_up_checkboxes(self):
        url = reverse("admin:quiz_checklisttemplate_change", args=[self.template.pk])
        response = self.client.post(
            url,
            {
                "name": self.template.name,
                "description": "",
                "document_id": self.template.document_id,
                "parent_sop": "",
                "purpose": "",
                "instructions": "",
                "created_by": str(self.user.pk),
                "is_active": "on",
                "items-TOTAL_FORMS": "2",
                "items-INITIAL_FORMS": "2",
                "items-MIN_NUM_FORMS": "0",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-id": str(self.neither.pk),
                "items-0-order": "1",
                "items-0-item_code": "SS-001",
                "items-0-section": "Site header",
                "items-0-text": self.neither.text,
                "items-0-response_type": ChecklistTemplateItem.RESPONSE_TEXT,
                "items-0-required": "on",
                "items-0-allow_photo": "on",
                "items-1-id": str(self.on_no.pk),
                "items-1-order": "2",
                "items-1-item_code": "WI-005",
                "items-1-section": "Worker Training and Safety",
                "items-1-text": self.on_no.text,
                "items-1-response_type": ChecklistTemplateItem.RESPONSE_YES_NO_NA,
                "items-1-required": "on",
                "items-1-follow_up_on_yes": "on",
                "items-1-responsibility_assignable": "on",
                "items-1-requires_photo": "",
                "items-1-allow_photo": "on",
                "_save": "Save",
            },
        )
        if response.status_code != 302:
            html = response.content.decode()
            errors = re.findall(r'<ul class="errorlist[^"]*">.*?</ul>', html, re.S)
            self.fail("Admin save did not redirect. Errors:\n" + "\n".join(errors[:12]))
        self.on_no.refresh_from_db()
        self.neither.refresh_from_db()
        self.assertEqual(self.on_no.create_action_on, ["Y"])
        self.assertTrue(self.on_no.responsibility_assignable)
        self.assertEqual(self.neither.create_action_on, [])
        self.assertFalse(self.neither.responsibility_assignable)

    def test_item_admin_shows_advanced_json_for_codes_other_than_y_or_n(self):
        self.on_no.create_action_on = ["N", "CUSTOM"]
        self.on_no.save(update_fields=["create_action_on"])
        url = reverse("admin:quiz_checklisttemplateitem_change", args=[self.on_no.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create action on (advanced)")
        self.assertContains(response, "CUSTOM")
        self.assertIs(
            True, _checkbox_checked(response.content.decode(), "follow_up_on_no")
        )

    def test_item_admin_hides_raw_json_for_a_normal_n_item(self):
        url = reverse("admin:quiz_checklisttemplateitem_change", args=[self.on_no.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Follow-up when No", html)
        self.assertNotIn("Create action on (advanced)", html)
        self.assertIs(True, _checkbox_checked(html, "follow_up_on_no"))
        self.assertIs(False, _checkbox_checked(html, "follow_up_on_yes"))
