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
        # An active event with proof overrides an expired permit date.
        # It does not use the extension-on-file label. Ranking is Compliant.
        "overrides_permit": True,
        "compliant_with_proof": True,
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