import logging

from django.db.models import CharField, Prefetch, Q
from django.db.models.functions import Cast

from arl.documentflow.models import (
    DocumentFlow,
    SentDocuSignEnvelope,
    SentDocuSignRecipient,
)
from arl.dsign.models import SignedDocumentFile
from arl.user.models import CustomUser

logger = logging.getLogger(__name__)

# Same statuses the DocuSign resend call accepts. Drafts, completed,
# declined, voided, and never-sent steps are not resent from this list.
RESENDABLE_STEP_STATUSES = frozenset({"sent", "delivered"})

_OVERALL_RANK = {
    "not_sent": 0,
    "in_progress": 1,
    "no_docs": 2,
    "completed": 3,
}

_STEP_SEVERITY = {
    "not_sent": 0,
    "declined": 1,
    "voided": 2,
    "created": 3,
    "sent": 4,
    "delivered": 5,
    "completed": 6,
}

_PILL_TONE = {
    "success": "ok",
    "warning": "watch",
    "primary": "auth",
    "danger": "urgent",
    "dark": "muted",
}

_OVERALL_CHIP = {
    "completed": ("Complete", "ok", "All required documents are complete"),
    "in_progress": ("In Progress", "watch", "Some required documents are still open"),
    "not_sent": ("Not Sent", "urgent", "No documents have been sent"),
}


def normalize_template_name(value):
    return (value or "").strip().lower()


def get_recipient_pill_class(status):
    status = (status or "").lower()

    if status == "completed":
        return "success"  # green
    if status == "delivered":
        return "warning"  # yellow
    if status in {"created", "sent"}:
        return "primary"  # blue
    if status in {"declined", "voided"}:
        return "dark"
    return "danger"  # red


def get_recipient_pill_label(status):
    status = (status or "").lower()

    if status == "completed":
        return "Complete"
    if status == "delivered":
        return "Opened"
    if status == "sent":
        return "Sent"
    if status == "created":
        return "Queued"
    if status == "declined":
        return "Declined"
    if status == "voided":
        return "Voided"
    return "Not Sent"


def get_step_pill_class(status):
    status = (status or "").lower()

    if status == "completed":
        return "success"
    if status == "delivered":
        return "warning"
    if status in {"created", "sent"}:
        return "primary"
    return "danger"


def get_step_pill_label(status):
    status = (status or "").lower()
    if status == "completed":
        return "Complete"
    if status == "delivered":
        return "Opened"
    if status == "sent":
        return "Sent"
    if status == "created":
        return "Queued"
    if status == "declined":
        return "Declined"
    if status == "voided":
        return "Voided"
    return "Not Sent"


def _status_tone(pill_class):
    return _PILL_TONE.get(pill_class, "muted")


def _employee_name_key(employee):
    return (
        (employee.last_name or "").casefold(),
        (employee.first_name or "").casefold(),
        (employee.username or "").casefold(),
        employee.pk or 0,
    )


def _search_audit_employees(employees, search_query):
    """Each word must match name, email, username, or store number."""
    search_query = (search_query or "").strip()
    if not search_query:
        return employees
    employees = employees.annotate(
        _store_number_text=Cast("store__number", CharField())
    )
    for token in search_query.split():
        employees = employees.filter(
            Q(first_name__icontains=token)
            | Q(last_name__icontains=token)
            | Q(email__icontains=token)
            | Q(username__icontains=token)
            | Q(_store_number_text__icontains=token)
        )
    return employees


def _normalize_doc_sort(sort, step_ids):
    sort = (sort or "").strip()
    if sort == "name":
        return sort
    if sort.startswith("step-") and sort[5:].isdigit() and int(sort[5:]) in step_ids:
        return sort
    return ""


def _overall_chip(overall_status):
    label, tone, title = _OVERALL_CHIP.get(
        overall_status,
        ("No Docs", "muted", "No required documents in this flow"),
    )
    return {"label": label, "tone": tone, "title": title}


def _step_display_name(step):
    template_name = step.template.template_name if step.template else ""
    return (
        getattr(step, "display_name", None)
        or template_name
        or f"Step {step.step_order}"
    )


def build_document_audit(employer, search_query="", incomplete_only=False, sort=""):
    flow = (
        DocumentFlow.objects.filter(employer=employer, is_active=True, is_default=True)
        .prefetch_related("steps__template")
        .first()
    )

    employees = CustomUser.objects.filter(
        employer=employer, is_active=True
    ).select_related("store")

    search_query = (search_query or "").strip()
    employees = _search_audit_employees(employees, search_query)
    employees = employees.order_by("-date_joined")

    if not flow:
        return {
            "flow": None,
            "rows": [],
            "audit_columns": [],
            "audit_search": search_query,
            "audit_incomplete_only": incomplete_only,
            "audit_sort": "",
            "hrdoc_chip_count": 1,
        }

    # Always start from the actual configured flow steps
    flow_steps = list(
        flow.steps.filter(is_active=True)
        .select_related("template")
        .order_by("step_order", "id")
    )

    logger.info(
        "AUDIT FLOW STEPS: %s",
        [
            (
                s.id,
                s.step_order,
                getattr(s, "display_name", None),
                getattr(s.template, "template_name", None) if s.template else None,
            )
            for s in flow_steps
        ],
    )

    # Get all envelopes for these employees in this flow
    sent_envelopes = (
        SentDocuSignEnvelope.objects.filter(
            user__in=employees,
            flow=flow,
        )
        .select_related("flow_step", "template", "user", "employer")
        .prefetch_related(
            Prefetch(
                "recipients",
                queryset=SentDocuSignRecipient.objects.order_by("routing_order", "id"),
            )
        )
        .order_by("completed_at", "id")
    )

    # Keep the newest envelope per (user, flow_step)
    sent_map = {}
    for env in sent_envelopes:
        if env.user_id and env.flow_step_id:
            sent_map[(env.user_id, env.flow_step_id)] = env

    employee_list = list(employees)
    signed_by_key = {}
    if employee_list:
        signed_files = SignedDocumentFile.objects.filter(
            employer=employer,
            user_id__in=[employee.id for employee in employee_list],
            is_company_document=False,
        ).only("id", "user_id", "envelope_id")
        for signed in signed_files:
            if signed.envelope_id:
                signed_by_key[(signed.user_id, signed.envelope_id)] = signed.id

    rows = []

    for employee in employees:
        step_results = []
        completed_count = 0
        total_required = len(flow_steps)

        for step in flow_steps:
            template_name = step.template.template_name if step.template else ""
            template_id = step.template.template_id if step.template else ""

            step_name = _step_display_name(step)

            sent_envelope = sent_map.get((employee.id, step.id))

            if sent_envelope:
                envelope_status = (sent_envelope.status or "").lower()

                recipient_pills = []
                for recipient in sent_envelope.recipients.all():
                    recipient_pills.append(
                        {
                            "name": recipient.name or recipient.email,
                            "email": recipient.email,
                            "role_name": recipient.role_name,
                            "routing_order": recipient.routing_order,
                            "status": recipient.status,
                            "label": get_recipient_pill_label(recipient.status),
                            "pill_class": get_recipient_pill_class(recipient.status),
                            "sent_at": recipient.sent_at,
                            "delivered_at": recipient.delivered_at,
                            "completed_at": recipient.completed_at,
                        }
                    )

                logger.info(
                    "AUDIT RECIPIENTS | employee=%s | step=%s | recipients=%s",
                    employee.email,
                    step_name,
                    [
                        (r.name, r.role_name, r.status)
                        for r in sent_envelope.recipients.all()
                    ],
                )

                is_complete = envelope_status == "completed"
                if is_complete:
                    completed_count += 1

                pill_class = get_step_pill_class(envelope_status)
                label = get_step_pill_label(envelope_status)
                step_results.append(
                    {
                        "step_id": step.id,
                        "step_order": step.step_order,
                        "step_name": step_name,
                        "template_name": template_name,
                        "template_id": template_id,
                        "status": envelope_status,
                        "label": label,
                        "pill_class": pill_class,
                        "tone": _status_tone(pill_class),
                        "title": f"{step_name} — {label}",
                        "recipient_pills": recipient_pills,
                        "sent_envelope_id": sent_envelope.id,
                        "envelope_id": sent_envelope.envelope_id,
                        "sent_at": sent_envelope.sent_at,
                        "signed_document_id": signed_by_key.get(
                            (employee.id, sent_envelope.envelope_id)
                        ),
                        "can_resend": envelope_status in RESENDABLE_STEP_STATUSES,
                        "is_complete": is_complete,
                    }
                )
            else:
                step_results.append(
                    {
                        "step_id": step.id,
                        "step_order": step.step_order,
                        "step_name": step_name,
                        "template_name": template_name,
                        "template_id": template_id,
                        "status": "not_sent",
                        "label": "Not Sent",
                        "pill_class": "danger",
                        "tone": "urgent",
                        "title": f"{step_name} — Not Sent",
                        "recipient_pills": [],
                        "sent_envelope_id": None,
                        "envelope_id": None,
                        "sent_at": None,
                        "signed_document_id": None,
                        "can_resend": False,
                        "is_complete": False,
                    }
                )

        if completed_count == total_required and total_required > 0:
            overall_status = "completed"
        elif completed_count == 0 and all(
            step["status"] == "not_sent" for step in step_results
        ):
            overall_status = "not_sent"
        else:
            overall_status = "in_progress"

        is_complete = completed_count == total_required and total_required > 0

        if incomplete_only and is_complete:
            continue

        rows.append(
            {
                "employee": employee,
                "step_results": step_results,  # keep old template name for compatibility
                "steps": step_results,  # also provide new name
                "completed_count": completed_count,
                "total_required": total_required,
                "overall_status": overall_status,
                "overall_chip": _overall_chip(overall_status),
                "is_complete": is_complete,
                "is_flagged": not is_complete,
                "can_resend": any(step["can_resend"] for step in step_results),
                "missing_count": total_required - completed_count,
            }
        )

    step_ids = {step.id for step in flow_steps}
    sort = _normalize_doc_sort(sort, step_ids)

    if sort == "name":
        rows.sort(key=lambda row: _employee_name_key(row["employee"]))
    elif sort.startswith("step-"):
        step_id = int(sort[5:])

        def _step_rank(row):
            for step in row["steps"]:
                if step["step_id"] == step_id:
                    return _STEP_SEVERITY.get(step["status"], 99)
            return 99

        rows.sort(
            key=lambda row: (
                _step_rank(row),
                _employee_name_key(row["employee"]),
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                _OVERALL_RANK.get(row["overall_status"], 9),
                -row["missing_count"],
                _employee_name_key(row["employee"]),
            )
        )

    audit_columns = [
        {
            "id": step.id,
            "name": _step_display_name(step),
            "sort": f"step-{step.id}",
        }
        for step in flow_steps
    ]

    return {
        "flow": flow,
        "rows": rows,
        "audit_columns": audit_columns,
        "audit_search": search_query,
        "audit_incomplete_only": incomplete_only,
        "audit_sort": sort,
        "hrdoc_chip_count": len(flow_steps) + 1,
    }


def resend_employee_flow_documents(employer, employee, step_id=None):
    """Resend the envelopes this scan list is showing.

    Only sent and delivered envelopes are included. A step that was
    never sent has no DocuSign envelope to resend.
    """
    from arl.dsign.helpers import resend_docusign_envelope

    audit = build_document_audit(employer)
    steps = []
    for row in audit["rows"]:
        if row["employee"].pk == employee.pk:
            steps = row["steps"]
            break

    if step_id not in (None, ""):
        steps = [step for step in steps if str(step["step_id"]) == str(step_id)]

    resent = []
    errors = []
    for step in steps:
        if not step.get("can_resend") or not step.get("sent_envelope_id"):
            continue
        envelope = SentDocuSignEnvelope.objects.filter(
            pk=step["sent_envelope_id"],
            employer=employer,
            user=employee,
        ).first()
        if envelope is None:
            continue
        try:
            resend_docusign_envelope(envelope)
        except Exception as exc:
            logger.exception(
                "HR document resend failed for envelope %s",
                envelope.envelope_id,
            )
            errors.append(f"{step['step_name']}: {exc}")
            continue
        resent.append(step["step_name"])

    return {"resent": resent, "errors": errors}
