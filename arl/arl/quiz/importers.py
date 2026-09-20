import json
from pathlib import Path

from .models import ChecklistTemplate, ChecklistTemplateItem

DEFAULT_WORKPLACE_INSPECTION_JSON = (
    Path(__file__).resolve().parent / "data" / "enmcds840_6_3_workplace_inspection.json"
)


def load_checklist_payload(path=None):
    source = Path(path) if path else DEFAULT_WORKPLACE_INSPECTION_JSON
    with source.open(encoding="utf-8") as handle:
        return json.load(handle)


def import_checklist_template(payload, created_by=None):
    """Create or update a ChecklistTemplate from ENMCDS840-style JSON."""
    document_id = (payload.get("document_id") or "").strip()
    name = payload.get("document") or document_id or "Imported checklist"
    defaults = {
        "name": name,
        "description": payload.get("purpose") or "",
        "parent_sop": payload.get("parent_sop") or "",
        "purpose": payload.get("purpose") or "",
        "instructions": payload.get("instructions") or "",
        "is_active": True,
    }
    if created_by is not None:
        defaults["created_by"] = created_by

    lookup = {"document_id": document_id} if document_id else {"name": name}
    if document_id:
        defaults["document_id"] = document_id
    template, _created = ChecklistTemplate.objects.update_or_create(
        **lookup,
        defaults=defaults,
    )

    keep_codes = []
    for index, raw in enumerate(payload.get("items") or [], start=1):
        item_code = (raw.get("id") or "").strip()
        wording = (raw.get("wording") or raw.get("title") or raw.get("description") or "")[
            :500
        ]
        response_type = raw.get("response_type") or ChecklistTemplateItem.RESPONSE_YES_NO_NA
        if response_type not in {
            ChecklistTemplateItem.RESPONSE_YES_NO_NA,
            ChecklistTemplateItem.RESPONSE_TEXT,
            ChecklistTemplateItem.RESPONSE_DATE,
        }:
            response_type = ChecklistTemplateItem.RESPONSE_YES_NO_NA

        create_action_on = raw.get("create_action_on")
        if create_action_on is None:
            create_action_on = (
                ["N"] if response_type == ChecklistTemplateItem.RESPONSE_YES_NO_NA else []
            )

        item_defaults = {
            "section": raw.get("section") or "",
            "text": wording,
            "response_type": response_type,
            "required": bool(raw.get("required", True)),
            "requires_photo": bool(raw.get("photo_required", False)),
            "allow_photo": bool(raw.get("allow_photo", True)),
            "responsibility_assignable": bool(raw.get("responsibility_assignable", False)),
            "create_action_on": list(create_action_on),
            "action_plan_form": raw.get("action_plan_form") or "",
            "order": index,
        }
        if item_code:
            item, _ = ChecklistTemplateItem.objects.update_or_create(
                template=template,
                item_code=item_code,
                defaults=item_defaults,
            )
            keep_codes.append(item.item_code)
        else:
            item = ChecklistTemplateItem.objects.create(
                template=template,
                item_code="",
                **item_defaults,
            )
            keep_codes.append(item.item_code)

    if keep_codes:
        template.items.exclude(item_code__in=[code for code in keep_codes if code]).delete()
    return template
