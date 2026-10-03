from .models import DocumentFlow, DocumentFlowStep


def normalize_template_name(value):
    return (value or "").strip().lower()


def get_default_document_flow(employer):
    return (
        DocumentFlow.objects.filter(employer=employer, is_active=True, is_default=True)
        .prefetch_related("steps__template")
        .first()
    )


def _template_match_keys(template):
    if template is None:
        return None, None, None
    if isinstance(template, str):
        stripped = template.strip()
        return None, stripped, normalize_template_name(stripped)
    template_pk = getattr(template, "id", None) or getattr(template, "pk", None)
    template_docusign_id = (getattr(template, "template_id", None) or "").strip()
    template_name = normalize_template_name(getattr(template, "template_name", None))
    return template_pk, template_docusign_id, template_name


def envelope_template_identity(envelope):
    if envelope.template_id:
        return f"tpl-{envelope.template_id}"
    if envelope.template and envelope.template.template_id:
        return f"docusign-{envelope.template.template_id}"
    name = normalize_template_name(envelope.template_name)
    if name:
        return f"name-{name}"
    return f"envelope-{envelope.id}"


def envelope_display_name(envelope):
    if envelope.template_id and envelope.template:
        return envelope.template.template_name or envelope.template_name
    return envelope.template_name or "Document"


def envelope_matches_step_template_exact(envelope, step):
    step_template = step.template
    if not step_template:
        return False
    if envelope.template_id and envelope.template_id == step_template.id:
        return True
    envelope_docusign_id = ""
    if envelope.template_id and envelope.template:
        envelope_docusign_id = (envelope.template.template_id or "").strip()
    if (
        envelope_docusign_id
        and step_template.template_id
        and envelope_docusign_id == step_template.template_id
    ):
        return True
    return False


def envelope_matches_step_template_by_name(envelope, step):
    if envelope.template_id:
        return False
    step_template = step.template
    if not step_template:
        return False
    envelope_name = normalize_template_name(envelope.template_name)
    step_name = normalize_template_name(step_template.template_name)
    return envelope_name and envelope_name == step_name


def _step_matches_template(step, template_pk, template_docusign_id, template_name):
    step_template = step.template
    if not step_template:
        return False
    if template_pk and step_template.id == template_pk:
        return True
    if template_docusign_id and step_template.template_id == template_docusign_id:
        return True
    if template_name and normalize_template_name(step_template.template_name) == template_name:
        return True
    return False


def get_flow_step_for_template(employer, template):
    """Return the active step in the employer default flow that uses this template."""
    if not employer or not template:
        return None, None

    flow = get_default_document_flow(employer)
    if not flow:
        return None, None

    template_pk, template_docusign_id, template_name = _template_match_keys(template)

    for step in flow.steps.filter(is_active=True).select_related("template"):
        if template_pk or template_docusign_id:
            if template_pk and step.template_id == template_pk:
                return flow, step
            step_template = step.template
            if (
                template_docusign_id
                and step_template
                and step_template.template_id == template_docusign_id
            ):
                return flow, step
            continue
        if template_name and step.template:
            if template_name == normalize_template_name(step.template.template_name):
                return flow, step

    return None, None


def resolve_envelope_column(envelope, flow_steps, flow_step_ids):
    """
    Place an envelope on a flow-step column when its template exactly matches that
    step, otherwise on a per-template supplemental column.
    """
    steps_by_id = {step.id: step for step in flow_steps}

    if envelope.flow_step_id and envelope.flow_step_id in flow_step_ids:
        step = steps_by_id.get(envelope.flow_step_id)
        if step and envelope_matches_step_template_exact(envelope, step):
            return f"step-{step.id}", step

    for step in flow_steps:
        if envelope_matches_step_template_exact(envelope, step):
            return f"step-{step.id}", step

    if not envelope.template_id:
        for step in flow_steps:
            if envelope_matches_step_template_by_name(envelope, step):
                return f"step-{step.id}", step

    return f"extra-{envelope_template_identity(envelope)}", None


def build_flow_step_column(step):
    template = step.template
    template_name = template.template_name if template else ""
    step_name = (
        getattr(step, "display_name", None)
        or getattr(step, "name", None)
        or template_name
        or f"Step {step.step_order}"
    )
    return {
        "column_key": f"step-{step.id}",
        "step_id": step.id,
        "step_order": step.step_order,
        "step_name": step_name,
        "template_name": template_name,
        "template_id": template.template_id if template else "",
        "is_flow_step": True,
    }


def build_extra_column(column_key, envelope):
    return {
        "column_key": column_key,
        "step_id": None,
        "step_order": 10_000,
        "step_name": envelope_display_name(envelope),
        "template_name": envelope_display_name(envelope),
        "template_id": (
            envelope.template.template_id
            if envelope.template_id and envelope.template
            else ""
        ),
        "is_flow_step": False,
    }


def get_first_flow_step(flow):
    if not flow:
        return None
    return flow.steps.filter(is_active=True).order_by("step_order").first()


def get_next_flow_step(flow, current_step_order):
    return (
        flow.steps.filter(is_active=True, step_order__gt=current_step_order)
        .order_by("step_order")
        .first()
    )
