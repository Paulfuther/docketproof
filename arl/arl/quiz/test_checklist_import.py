import unittest

from .checklist_json import (
    ChecklistImportError,
    parse_checklist_template_bytes,
    parse_checklist_template_payload,
)


class ParseChecklistTemplatePayloadTests(unittest.TestCase):
    def test_merch_export_maps_title_notes_and_optional_photos(self):
        parsed = parse_checklist_template_payload(
            {
                "title": "Fall/Winter Exterior Checklist",
                "notes": "From merchandising guidelines.",
                "items": [
                    {
                        "id": "fw26-01",
                        "section": "Implementation Timing",
                        "title": "National implementation by October 31",
                        "description": "Ignored extra detail.",
                        "image_path": "pages/page-02.png",
                        "required": True,
                        "page": 2,
                    },
                    {
                        "title": "No product in parking areas",
                    },
                ],
            }
        )

        self.assertEqual(parsed.name, "Fall/Winter Exterior Checklist")
        self.assertEqual(parsed.description, "From merchandising guidelines.")
        self.assertEqual(len(parsed.items), 2)
        self.assertEqual(parsed.items[0].text, "National implementation by October 31")
        self.assertFalse(parsed.items[0].requires_photo)
        self.assertEqual(parsed.items[0].order, 0)
        self.assertEqual(parsed.items[1].order, 1)

    def test_security_export_ignores_allow_photo(self):
        parsed = parse_checklist_template_payload(
            {
                "document": "Security Assessment",
                "purpose": "Walk the site.",
                "pages": 7,
                "items": [
                    {
                        "id": "SA-001",
                        "section": "Site header",
                        "title": "DATE",
                        "response_type": "date",
                        "allow_photo": True,
                        "photo_required": False,
                    },
                    {
                        "title": "SITE NAME",
                        "allow_photo": True,
                        "photo_required": False,
                    },
                ],
            }
        )

        self.assertEqual(parsed.name, "Security Assessment")
        self.assertEqual(parsed.description, "Walk the site.")
        self.assertEqual(parsed.items[0].text, "DATE")
        self.assertFalse(parsed.items[0].requires_photo)
        self.assertFalse(parsed.items[1].requires_photo)

    def test_name_beats_title_and_document(self):
        parsed = parse_checklist_template_payload(
            {
                "name": "Opening",
                "title": "Ignored title",
                "document": "Ignored document",
                "items": [{"text": "Lights on"}],
            }
        )
        self.assertEqual(parsed.name, "Opening")

    def test_item_text_beats_title(self):
        parsed = parse_checklist_template_payload(
            {
                "name": "Closing",
                "items": [{"text": "Lock the safe", "title": "Ignored"}],
            }
        )
        self.assertEqual(parsed.items[0].text, "Lock the safe")

    def test_requires_photo_true_from_requires_photo_or_photo_required(self):
        parsed = parse_checklist_template_payload(
            {
                "name": "Photos",
                "items": [
                    {"text": "Required photo field", "requires_photo": True},
                    {"text": "Required photo alias", "photo_required": True},
                    {"text": "Optional because allow_photo only", "allow_photo": True},
                ],
            }
        )
        self.assertTrue(parsed.items[0].requires_photo)
        self.assertTrue(parsed.items[1].requires_photo)
        self.assertFalse(parsed.items[2].requires_photo)

    def test_explicit_order_is_used(self):
        parsed = parse_checklist_template_payload(
            {
                "name": "Ordered",
                "items": [
                    {"text": "Second", "order": 2},
                    {"text": "First", "order": 1},
                ],
            }
        )
        self.assertEqual([item.order for item in parsed.items], [2, 1])

    def test_skips_blank_items_and_non_objects(self):
        parsed = parse_checklist_template_payload(
            {
                "name": "Sparse",
                "items": [
                    "not a dict",
                    {"title": "   "},
                    {"section": "only extra fields"},
                    {"title": "Keep me"},
                ],
            }
        )
        self.assertEqual([item.text for item in parsed.items], ["Keep me"])
        self.assertEqual(parsed.items[0].order, 3)

    def test_missing_name_raises(self):
        with self.assertRaises(ChecklistImportError):
            parse_checklist_template_payload({"items": [{"text": "A"}]})

    def test_missing_items_raises(self):
        with self.assertRaises(ChecklistImportError):
            parse_checklist_template_payload({"name": "Empty"})

    def test_no_usable_items_raises(self):
        with self.assertRaises(ChecklistImportError):
            parse_checklist_template_payload(
                {"name": "Empty", "items": [{"section": "x"}]}
            )

    def test_utf8_bytes_with_bom(self):
        raw = b'\xef\xbb\xbf{"name": "BOM", "items": [{"text": "One"}]}'
        parsed = parse_checklist_template_bytes(raw)
        self.assertEqual(parsed.name, "BOM")
        self.assertEqual(parsed.items[0].text, "One")

    def test_invalid_json_bytes_raise(self):
        with self.assertRaises(ChecklistImportError):
            parse_checklist_template_bytes(b"{not json")
