from django.db.models import Prefetch, Q
from arl.documentflow.helpers import (
    build_flow_step_lookup,
    resolve_envelope_flow_step_id,
)
from arl.documentflow.models import (
    DocumentFlow,
    SentDocuSignEnvelope,
    SentDocuSignRecipient,
)
from arl.user.models import CustomUser
import logging
logger = logging.getLogger(__name__)


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


def build_document_audit(employer, search_query="", incomplete_only=False):
    flow = (
        DocumentFlow.objects
        .filter(employer=employer, is_active=True, is_default=True)
        .prefetch_related("steps__template")
        .first()
    )

    employees = (
        CustomUser.objects
        .filter(employer=employer, is_active=True)
        .select_related("store")
    )

    search_query = (search_query or "").strip()
    if search_query:
        employees = employees.filter(
            Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
            | Q(email__icontains=search_query)
        )

    employees = employees.order_by("-date_joined")

    if not flow:
        return {
            "flow": None,
            "rows": [],
            "audit_search": search_query,
            "audit_incomplete_only": incomplete_only,
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
        ]
    )

    by_template_pk, by_docusign_template_id, by_template_name, flow_step_ids = (
        build_flow_step_lookup(flow_steps)
    )

    # Include every tracked envelope for these employees; match to flow steps at read time.
    sent_envelopes = (
        SentDocuSignEnvelope.objects
        .filter(employer=employer, user__in=employees)
        .select_related("flow_step", "template", "user", "employer")
        .prefetch_related(
            Prefetch(
                "recipients",
                queryset=SentDocuSignRecipient.objects.order_by("routing_order", "id"),
            )
        )
        .order_by("sent_at", "id")
    )

    # Keep the newest envelope per (user, flow_step), including legacy unlinked rows.
    sent_map = {}
    envelopes_to_link = []
    for env in sent_envelopes:
        step_id = resolve_envelope_flow_step_id(
            env,
            by_template_pk,
            by_docusign_template_id,
            by_template_name,
            flow_step_ids,
        )
        if not env.user_id or not step_id:
            continue
        if env.flow_step_id != step_id or env.flow_id != flow.id:
            env.flow = flow
            env.flow_step_id = step_id
            envelopes_to_link.append(env)
        key = (env.user_id, step_id)
        existing = sent_map.get(key)
        if not existing or env.sent_at >= existing.sent_at:
            sent_map[key] = env

    if envelopes_to_link:
        SentDocuSignEnvelope.objects.bulk_update(
            envelopes_to_link,
            ["flow", "flow_step"],
        )

    rows = []

    for employee in employees:
        step_results = []
        completed_count = 0
        total_required = len(flow_steps)

        for step in flow_steps:
            template_name = step.template.template_name if step.template else ""
            template_id = step.template.template_id if step.template else ""

            step_name = (
                getattr(step, "display_name", None)
                or getattr(step, "name", None)
                or template_name
                or f"Step {step.step_order}"
            )

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

                step_results.append(
                    {
                        "step_id": step.id,
                        "step_order": step.step_order,
                        "step_name": step_name,
                        "template_name": template_name,
                        "template_id": template_id,
                        "status": envelope_status,
                        "label": get_step_pill_label(envelope_status),
                        "pill_class": get_step_pill_class(envelope_status),
                        "recipient_pills": recipient_pills,
                        "sent_envelope_id": sent_envelope.id,
                        "envelope_id": sent_envelope.envelope_id,
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
                        "recipient_pills": [],
                        "sent_envelope_id": None,
                        "envelope_id": None,
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
                "step_results": step_results,   # keep old template name for compatibility
                "steps": step_results,          # also provide new name
                "completed_count": completed_count,
                "total_required": total_required,
                "overall_status": overall_status,
                "is_complete": is_complete,
            }
        )

    return {
        "flow": flow,
        "rows": rows,
        "audit_search": search_query,
        "audit_incomplete_only": incomplete_only,
    }