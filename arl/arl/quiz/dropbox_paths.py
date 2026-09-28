"""Dropbox PDF path for submitted checklists (matches main / PR #422)."""

from datetime import datetime

from django.utils.text import slugify


def resolve_checklist_employer(checklist):
    """Employer for the checklist email and the Dropbox company folder.

    created_by.employer, then submitted_by.employer, then store.employer.
    A checklist creator with no employer still uses the submitter or the store.
    """
    for owner in (
        getattr(checklist, "created_by", None),
        getattr(checklist, "submitted_by", None),
        getattr(checklist, "store", None),
    ):
        if owner is None:
            continue
        employer = getattr(owner, "employer", None)
        if employer is not None:
            return employer
    return None


def build_checklist_dropbox_path(checklist, store_segment, when=None):
    """
    /CHECKLISTS/{company_name}/{checklist_folder}/{store_segment}/{year}/{month}/{filename}.pdf

    checklist_folder prefers slugify(template.name), then title, slug, or checklist-{id}.
    Filename stays {store_segment}_{slug}-{id}.pdf.
    Company comes from resolve_checklist_employer (creator, submitter, then store).
    """
    employer = resolve_checklist_employer(checklist)
    company_name = slugify(getattr(employer, "name", None) or "") or "no-company"
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


def build_salt_log_dropbox_path(salt_log, store_segment, when=None):
    """
    /SALTLOGS/{company}/salt-log/{store}/{year}/{month}/{filename}.pdf

    Same shape as checklists, under SALTLOGS. Does not move older PDFs
    that were stored as /SALTLOGS/{company}/{year}/{month}/{store}/….
    """
    company_name = (
        slugify(getattr(getattr(salt_log, "user_employer", None), "name", "") or "")
        or "no-company"
    )
    when = when or datetime.now()
    year = when.strftime("%Y")
    month = when.strftime("%m-%B")
    folder = "salt-log"
    filename = f"{store_segment}_salt-log-{salt_log.pk}.pdf"
    return (
        f"/SALTLOGS/{company_name}/{folder}/{store_segment}/{year}/{month}/{filename}"
    )
