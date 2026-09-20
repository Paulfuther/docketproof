"""Dropbox PDF path for submitted checklists (matches main / PR #422)."""

from datetime import datetime

from django.utils.text import slugify


def build_checklist_dropbox_path(checklist, store_segment, when=None):
    """
    /CHECKLISTS/{company_name}/{checklist_folder}/{store_segment}/{year}/{month}/{filename}.pdf

    checklist_folder prefers slugify(template.name), then title, slug, or checklist-{id}.
    Filename stays {store_segment}_{slug}-{id}.pdf.
    """
    company_name = slugify(
        getattr(
            getattr(checklist.created_by, "employer", None), "name", "no-company"
        )
    )
    when = when or datetime.now()
    year = when.strftime("%Y")
    month = when.strftime("%m-%B")
    slug = checklist.slug or slugify(checklist.title) or f"checklist-{checklist.id}"
    filename = f"{store_segment}_{slug}-{checklist.id}.pdf"
    template_name = getattr(getattr(checklist, "template", None), "name", None)
    checklist_folder = (
        slugify(template_name or "")
        or slugify(checklist.title or "")
        or slugify(checklist.slug or "")
        or f"checklist-{checklist.id}"
    )
    folder_path = (
        f"/CHECKLISTS/{company_name}/{checklist_folder}/{store_segment}"
        f"/{year}/{month}"
    )
    return f"{folder_path}/{filename}", checklist_folder
