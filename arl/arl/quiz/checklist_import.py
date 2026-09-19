"""Create ChecklistTemplate rows from parsed checklist JSON."""

from django import forms
from django.db import transaction

from .checklist_json import (
    ChecklistImportError,
    MAX_IMPORT_BYTES,
    ParsedChecklistTemplate,
    parse_checklist_template_bytes,
)
from .models import ChecklistTemplate, ChecklistTemplateItem


@transaction.atomic
def create_checklist_template_from_parsed(
    parsed: ParsedChecklistTemplate, *, created_by=None
) -> ChecklistTemplate:
    template = ChecklistTemplate.objects.create(
        name=parsed.name,
        description=parsed.description,
        created_by=created_by,
        is_active=True,
    )
    ChecklistTemplateItem.objects.bulk_create(
        [
            ChecklistTemplateItem(
                template=template,
                text=item.text,
                requires_photo=item.requires_photo,
                order=item.order,
            )
            for item in parsed.items
        ]
    )
    return template


class ChecklistTemplateImportForm(forms.Form):
    json_file = forms.FileField(
        label="JSON file",
        help_text=(
            "Upload a checklist export. Template name comes from name, title, "
            "or document. Items use text or title. Photos stay optional unless "
            "requires_photo or photo_required is true."
        ),
    )

    def clean_json_file(self):
        uploaded = self.cleaned_data["json_file"]
        if uploaded.size and uploaded.size > MAX_IMPORT_BYTES:
            raise forms.ValidationError("File is too large (max 5 MB).")
        self.parsed = parse_checklist_template_bytes(uploaded.read())
        return uploaded

    def save(self, *, created_by=None) -> ChecklistTemplate:
        parsed = getattr(self, "parsed", None)
        if parsed is None:
            raise ChecklistImportError("No parsed checklist data to save.")
        return create_checklist_template_from_parsed(
            parsed, created_by=created_by
        )
