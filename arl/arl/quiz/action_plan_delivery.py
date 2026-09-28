"""Split 6.4 action-plan delivery from the checklist PDF.

Site Security, Workplace Security, and the related BC/ON workplace
inspection keep today's checklist email and Dropbox upload. The action
plan is a second PDF, a second upload beside that file, and a second
email to the action_plan_email role.
"""

import logging

from django.contrib.auth import get_user_model
from django.db.models import Q

from arl.quiz.dropbox_paths import (
    build_checklist_dropbox_path,
    resolve_checklist_employer,
)
from arl.quiz.models import ChecklistActionItem
from arl.quiz.store_address import format_store_address_line

logger = logging.getLogger(__name__)

CHECKLIST_EMAIL_GROUP = "quiz_email"
ACTION_PLAN_EMAIL_GROUP = "action_plan_email"
CHECKLIST_SENDGRID_TEMPLATE = "d-7e7eb87381b04ef59bc39abc16550ead"
MAX_ATTACH_BYTES = 15_500_000

# ENMCDS840-6.3 is the Workplace Inspection Checklist (BC & ON).
# Its items point at the 6.4 Site Security Action Plan Form.
SPLIT_DOCUMENT_IDS = {"ENMCDS840-6.3"}
SPLIT_FORM_IDS = {"ENMCDS840-6.4"}
SPLIT_NAME_MARKERS = (
    "site security",
    "workplace security",
    "workplace inspection",
)


def checklist_splits_action_plan(checklist) -> bool:
    """True when submit should deliver a separate 6.4 action-plan PDF."""
    template = getattr(checklist, "template", None)
    names = (
        (getattr(template, "name", None) or ""),
        (getattr(checklist, "title", None) or ""),
    )
    for name in names:
        folded = name.casefold()
        if any(marker in folded for marker in SPLIT_NAME_MARKERS):
            return True
    document_id = (getattr(template, "document_id", None) or "").strip().upper()
    if document_id in SPLIT_DOCUMENT_IDS:
        return True
    return _items_use_action_plan_form(checklist)


def checklist_pdf_shows_action_plan(checklist) -> bool:
    """Checklist PDF keeps an action summary only when delivery is not split."""
    return not checklist_splits_action_plan(checklist)


def _items_use_action_plan_form(checklist) -> bool:
    items = getattr(checklist, "items", None)
    if items is None:
        return False
    if hasattr(items, "select_related"):
        items = items.select_related("template_item")
    for item in items:
        template_item = getattr(item, "template_item", None)
        form_id = (getattr(template_item, "action_plan_form", None) or "").strip()
        form_id = form_id.upper()
        if form_id in SPLIT_FORM_IDS or form_id.endswith("-6.4"):
            return True
    return False


def build_action_plan_dropbox_path(checklist, store_segment, when=None):
    """Same folder as the checklist PDF, with an -action-plan filename."""
    checklist_path, folder = build_checklist_dropbox_path(
        checklist, store_segment, when=when
    )
    stem, dot, ext = checklist_path.rpartition(".")
    if not dot:
        return f"{checklist_path}-action-plan.pdf", folder
    return f"{stem}-action-plan.{ext}", folder


def _person_name(user) -> str:
    if user is None:
        return ""
    getter = getattr(user, "get_full_name", None)
    if callable(getter):
        full = (getter() or "").strip()
        if full:
            return full
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    combined = f"{first} {last}".strip()
    if combined:
        return combined
    return (getattr(user, "email", None) or "").strip()


def _header_items(checklist):
    items = getattr(checklist, "items", None)
    if items is None:
        return []
    if hasattr(items, "select_related"):
        items = items.select_related("template_item")
    headers = []
    for item in items:
        section = (getattr(item, "section", None) or "").strip().casefold()
        response = getattr(item, "response_type", "") or ""
        if section == "site header" or response in ("text", "date"):
            headers.append(item)
    return headers


def _header_text(headers, predicate) -> str:
    for item in headers:
        label = (getattr(item, "text", None) or "").strip().casefold()
        if not predicate(label):
            continue
        value = (getattr(item, "text_value", None) or "").strip()
        if value:
            return value
    return ""


def _site_location(checklist, header_value) -> str:
    if header_value:
        return header_value
    store = getattr(checklist, "store", None)
    if store is None:
        return ""
    number = getattr(store, "number", None)
    name = (getattr(store, "name", None) or "").strip()
    bits = []
    if number not in (None, ""):
        bits.append(str(number))
    if name:
        bits.append(name)
    location = " — ".join(bits)
    address = format_store_address_line(store)
    if address:
        return f"{location}, {address}" if location else address
    return location


def _completed_on(checklist, header_value) -> str:
    if header_value:
        return header_value
    submitted = getattr(checklist, "submitted_at", None)
    if submitted is None:
        return ""
    return submitted.strftime("%Y-%m-%d")


def _is_position_label(label: str) -> bool:
    return (
        label == "position"
        or label.startswith("position ")
        or label.startswith("position (")
    )


def action_plan_rows(checklist):
    actions = getattr(checklist, "action_items", None)
    if actions is None:
        return []
    if hasattr(actions, "select_related"):
        actions = actions.select_related("checklist_item").order_by(
            "checklist_item__order", "id"
        )
    rows = []
    for action in actions:
        rows.append(
            {
                "action_item": action.action_item,
                "action_required": action.action_required,
                "when": action.target_date,
                "who": action.who,
                "done": action.status == ChecklistActionItem.STATUS_DONE,
                "completion_date": action.completion_date,
            }
        )
    return rows


def action_plan_pdf_context(checklist) -> dict:
    """Header and rows for the 6.4 Site Security Action Plan Form."""
    headers = _header_items(checklist)
    site_from_form = _header_text(headers, lambda label: "site number" in label)
    completed_by = _header_text(headers, lambda label: "completed by" in label)
    if not completed_by:
        completed_by = _person_name(
            getattr(checklist, "submitted_by", None)
            or getattr(checklist, "created_by", None)
        )
    position = _header_text(headers, _is_position_label)
    completed_on = _completed_on(
        checklist,
        _header_text(headers, lambda label: "completed on" in label),
    )
    template = getattr(checklist, "template", None)
    return {
        "checklist": checklist,
        "form_title": "6.4 Site Security Action Plan Form",
        "form_subtitle": "Workplace Violence and Harassment Prevention",
        "site_location": _site_location(checklist, site_from_form),
        "completed_by": completed_by,
        "position": position,
        "completed_on": completed_on,
        "rows": action_plan_rows(checklist),
        "document_id": (getattr(template, "document_id", None) or "").strip(),
        "source_title": getattr(checklist, "title", "") or "",
    }


def email_pdf_to_employer_group(
    *,
    checklist,
    pdf_bytes,
    filename,
    group_name,
    subject,
    log_label,
):
    """Email one PDF to active users in one role for the checklist employer.

    Same SendGrid template and attachment rules as the existing checklist mail.
    Returns True when SendGrid accepts the send.
    """
    from django.conf import settings

    from arl.msg.helpers import create_master_email
    from arl.setup.models import TenantApiKeys

    logger.info("[%s] Using template ID: %s", log_label, CHECKLIST_SENDGRID_TEMPLATE)
    employer = resolve_checklist_employer(checklist)
    employer_id = getattr(employer, "id", None)
    if not employer_id:
        logger.warning(
            "[%s] No employer on checklist.created_by, submitted_by, or store; "
            "skipping email.",
            log_label,
        )
        return False

    User = get_user_model()
    to_emails = list(
        User.objects.filter(
            Q(is_active=True),
            Q(groups__name=group_name),
            Q(employer_id=employer_id),
        )
        .values_list("email", flat=True)
        .distinct()
    )
    if not to_emails:
        logger.warning(
            "[%s] No active users in group '%s' for employer_id=%s",
            log_label,
            group_name,
            employer_id,
        )
        return False

    tenant_api_key = TenantApiKeys.objects.filter(employer_id=employer_id).first()
    sender_email = (
        tenant_api_key.verified_sender_email
        if tenant_api_key
        else settings.MAIL_DEFAULT_SENDER
    )
    logger.info(
        "[%s] Preparing attachment: %s (%s bytes)",
        log_label,
        filename,
        len(pdf_bytes or b""),
    )

    attachments = None
    if pdf_bytes and len(pdf_bytes) <= MAX_ATTACH_BYTES:
        import base64

        attachments = [
            {
                "content": base64.b64encode(pdf_bytes).decode(),
                "filename": filename,
                "type": "application/pdf",
                "disposition": "attachment",
            }
        ]
    else:
        logger.info(
            "[%s] Attachment too large/missing; sending without file",
            log_label,
        )

    first_email = to_emails[0]
    try:
        recip = User.objects.get(email=first_email)
        full_name = recip.get_full_name() or recip.username
        company_name = (
            getattr(getattr(recip, "employer", None), "name", None) or "Company"
        )
    except User.DoesNotExist:
        full_name = "Team"
        company_name = "Company"

    template_data = {
        "subject": subject,
        "name": full_name,
        "company_name": company_name,
    }
    try:
        ok_email = create_master_email(
            to_email=to_emails,
            sendgrid_id=CHECKLIST_SENDGRID_TEMPLATE,
            template_data=template_data,
            attachments=attachments,
            verified_sender=sender_email,
        )
    except Exception as exc:
        logger.exception("[%s] Error sending email: %s", log_label, exc)
        return False
    if ok_email:
        logger.info(
            "[%s] Email sent to %d recipients in '%s'",
            log_label,
            len(to_emails),
            group_name,
        )
        return True
    logger.error("[%s] SendGrid send returned False", log_label)
    return False
