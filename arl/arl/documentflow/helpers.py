from .models import DocumentFlow, DocumentFlowStep


def get_default_document_flow(employer):
    return (
        DocumentFlow.objects.filter(employer=employer, is_active=True, is_default=True)
        .prefetch_related("steps__template")
        .first()
    )


def get_flow_step_for_template(employer, template):
    """Return the active step in the employer default flow that uses this template."""
    if not employer or not template:
        return None, None

    flow = get_default_document_flow(employer)
    if not flow:
        return None, None

    template_pk = getattr(template, "id", None) or getattr(template, "pk", None)
    template_docusign_id = getattr(template, "template_id", None)
    if isinstance(template, str):
        template_docusign_id = template

    for step in flow.steps.filter(is_active=True).select_related("template"):
        step_template = step.template
        if not step_template:
            continue
        if template_pk and step_template.id == template_pk:
            return flow, step
        if template_docusign_id and step_template.template_id == template_docusign_id:
            return flow, step

    return flow, None


def build_flow_step_lookup(flow_steps):
    """Map template PK and DocuSign template_id strings to flow step ids."""
    by_template_pk = {}
    by_docusign_template_id = {}
    for step in flow_steps:
        if not step.template_id:
            continue
        by_template_pk[step.template_id] = step.id
        docusign_id = step.template.template_id if step.template else ""
        if docusign_id:
            by_docusign_template_id[docusign_id] = step.id
    return by_template_pk, by_docusign_template_id


def resolve_envelope_flow_step_id(envelope, by_template_pk, by_docusign_template_id):
    if envelope.flow_step_id:
        return envelope.flow_step_id
    if envelope.template_id and envelope.template_id in by_template_pk:
        return by_template_pk[envelope.template_id]
    docusign_id = ""
    if envelope.template_id and envelope.template:
        docusign_id = envelope.template.template_id or ""
    return by_docusign_template_id.get(docusign_id)


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