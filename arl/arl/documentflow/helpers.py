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
        if _step_matches_template(step, template_pk, template_docusign_id, template_name):
            return flow, step

    return None, None


def build_flow_step_lookup(flow_steps):
    """Map template PK, DocuSign template_id, and template name to flow step ids."""
    by_template_pk = {}
    by_docusign_template_id = {}
    by_template_name = {}
    flow_step_ids = set()
    for step in flow_steps:
        flow_step_ids.add(step.id)
        if not step.template_id:
            continue
        by_template_pk[step.template_id] = step.id
        docusign_id = step.template.template_id if step.template else ""
        if docusign_id:
            by_docusign_template_id[docusign_id] = step.id
        template_name = normalize_template_name(
            step.template.template_name if step.template else ""
        )
        if template_name:
            by_template_name[template_name] = step.id
    return by_template_pk, by_docusign_template_id, by_template_name, flow_step_ids


def resolve_envelope_flow_step_id(
    envelope,
    by_template_pk,
    by_docusign_template_id,
    by_template_name,
    flow_step_ids,
):
    if envelope.flow_step_id and envelope.flow_step_id in flow_step_ids:
        return envelope.flow_step_id
    if envelope.template_id and envelope.template_id in by_template_pk:
        return by_template_pk[envelope.template_id]
    docusign_id = ""
    if envelope.template_id and envelope.template:
        docusign_id = envelope.template.template_id or ""
    if docusign_id:
        step_id = by_docusign_template_id.get(docusign_id)
        if step_id:
            return step_id
    for name in (
        normalize_template_name(envelope.template_name),
        normalize_template_name(
            envelope.template.template_name if envelope.template else ""
        ),
    ):
        if name and name in by_template_name:
            return by_template_name[name]
    return None


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
