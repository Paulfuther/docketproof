IMMIGRATION_STATUS_TYPES = {
    "work_permit_extension": {
        "label": "Work Permit Extension",
        "pill_class": "warning",
        "category": "temporary",
        "overrides_permit": True,
    },
    "maintained_status": {
        "label": "Maintained Status",
        "pill_class": "primary",
        "category": "temporary",
        "overrides_permit": True,
    },
    "pgwp_application": {
        "label": "PGWP Application",
        "pill_class": "primary",
        "category": "temporary",
        "overrides_permit": True,
    },
    "pr_application": {
        "label": "PR Application",
        "pill_class": "info",
        "category": "long_term",
        "overrides_permit": False,
    },
    "new_work_permit": {
        "label": "New Work Permit",
        "pill_class": "success",
        "category": "final",
        "overrides_permit": True,
    },
    "study_completed": {
        "label": "Studies Completed",
        "pill_class": "secondary",
        "category": "context",
        "overrides_permit": False,
    },
    "review_note": {
        "label": "HR Review Note",
        "pill_class": "dark",
        "category": "internal",
        "overrides_permit": False,
    },
    "work_authorization_letter_applied": {
        "label": "Work Authorization Letter Applied",
        "description": "Employee has applied for a work authorization letter allowing them to work while awaiting decision",
        "pill_class": "primary",
        "category": "Work Permit",
        "overrides_permit": True,
    },
    "other": {
        "label": "Other",
        "pill_class": "secondary",
        "category": "unknown",
        "overrides_permit": False,
    },
}

IMMIGRATION_STATUS_CHOICES = [
    (code, data["label"])
    for code, data in IMMIGRATION_STATUS_TYPES.items()
]

# Statuses that can change work-permit ranking when the event has a proof trail.
OVERRIDE_PERMIT_STATUS_TYPES = tuple(
    code
    for code, data in IMMIGRATION_STATUS_TYPES.items()
    if data.get("overrides_permit")
)


def immigration_status_overrides_permit(status_type):
    meta = IMMIGRATION_STATUS_TYPES.get(status_type) or {}
    return bool(meta.get("overrides_permit"))