from django.db.models import Prefetch, Q
from arl.documentflow.helpers import (
    build_extra_column,
    build_flow_step_column,
    resolve_envelope_column,
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

    flow_step_ids = {step.id for step in flow_steps}
    audit_columns = [build_flow_step_column(step) for step in flow_steps]

    # Include every tracked envelope for these employees.
    sent_envelopes = list(
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

    extra_column_envelopes = {}
    sent_map = {}
    for env in sent_envelopes:
        if not env.user_id:
            continue
        column_key, _matched_step = resolve_envelope_column(
            env, flow_steps, flow_step_ids
        )
        if column_key.startswith("extra-") and column_key not in extra_column_envelopes:
            extra_column_envelopes[column_key] = env
        map_key = (env.user_id, column_key)
        existing = sent_map.get(map_key)
        if not existing or env.sent_at >= existing.sent_at:
            sent_map[map_key] = env

    for column_key in sorted(
        extra_column_envelopes,
        key=lambda key: (
            extra_column_envelopes[key].sent_at,
            extra_column_envelopes[key].id,
        ),
    ):
        audit_columns.append(
            build_extra_column(column_key, extra_column_envelopes[column_key])
        )

    rows = []

    for employee in employees:
        step_results = []
        completed_count = 0
        total_required = len(flow_steps)

        for column in audit_columns:
            template_name = column["template_name"]
            template_id = column["template_id"]
            step_name = column["step_name"]

            sent_envelope = sent_map.get((employee.id, column["column_key"]))

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
                if column["is_flow_step"] and is_complete:
                    completed_count += 1

                step_results.append(
                    {
                        "column_key": column["column_key"],
                        "step_id": column["step_id"],
                        "step_order": column["step_order"],
                        "step_name": step_name,
                        "template_name": template_name,
                        "template_id": template_id,
                        "is_flow_step": column["is_flow_step"],
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
                if not column["is_flow_step"]:
                    continue
                step_results.append(
                    {
                        "column_key": column["column_key"],
                        "step_id": column["step_id"],
                        "step_order": column["step_order"],
                        "step_name": step_name,
                        "template_name": template_name,
                        "template_id": template_id,
                        "is_flow_step": column["is_flow_step"],
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
        "audit_columns": audit_columns,
        "rows": rows,
        "audit_search": search_query,
        "audit_incomplete_only": incomplete_only,
    }