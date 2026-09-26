IMMIGRATION_STATUS_TYPES = {
    "work_permit_extension": {
        "label": "Work Permit Extension",
        "scan_label": "Extension on file",
        "pill_class": "warning",
        "category": "temporary",
        "overrides_permit": True,
    },
    "maintained_status": {
        "label": "Maintained Status",
        "scan_label": "Maintained status",
        "pill_class": "primary",
        "category": "temporary",
        "overrides_permit": True,
    },
    "pgwp_application": {
        "label": "PGWP Application",
        "scan_label": "PGWP application",
        "pill_class": "primary",
        "category": "temporary",
        "overrides_permit": True,
    },
    "pr_application": {
        "label": "PR Application",
        "scan_label": "PR application",
        "pill_class": "info",
        "category": "long_term",
        "overrides_permit": False,
    },
    "new_work_permit": {
        "label": "New Work Permit",
        "scan_label": "New work permit",
        "pill_class": "success",
        "category": "final",
        "overrides_permit": True,
    },
    "study_completed": {
        "label": "Studies Completed",
        "scan_label": "Studies Completed",
        "pill_class": "secondary",
        "category": "context",
        "overrides_permit": False,
    },
    "review_note": {
        "label": "HR Review Note",
        "scan_label": "Review note",
        "pill_class": "dark",
        "category": "internal",
        "overrides_permit": False,
    },
    "work_authorization_letter_applied": {
        "label": "Work Authorization Letter Applied",
        "scan_label": "Authorization letter",
        "description": "Employee has applied for a work authorization letter allowing them to work while awaiting decision",
        "pill_class": "primary",
        "category": "Work Permit",
        "overrides_permit": True,
    },
    "other": {
        "label": "Other",
        "scan_label": "Other",
        "pill_class": "secondary",
        "category": "unknown",
        "overrides_permit": False,
    },
}

IMMIGRATION_STATUS_CHOICES = [
    (code, data["label"])
    for code, data in IMMIGRATION_STATUS_TYPES.items()
]