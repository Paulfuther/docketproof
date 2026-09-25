from datetime import date

from django.db.models import F, Q

from arl.documentflow.constants import (
    IMMIGRATION_STATUS_TYPES,
    OVERRIDE_PERMIT_STATUS_TYPES,
    immigration_status_overrides_permit,
)

# On-screen watch window. Not an email milestone. The 90/60/30 mailer
# lives in a separate change and is not applied here.
HR_WATCH_DAYS = 120
HR_WATCH_LABEL = "Expiring within 120 days (HR watch)"

# Lower sorts first. Unknown codes use overall_priority() and stay ahead of compliant.
PRIORITY_MAP = {
    "urgent": 0,
    "expiring_soon": 1,
    "extension_pending": 2,
    "compliant_sin_update_needed": 3,
    "compliant": 4,
}

EXTENSION_SHORTCUT_DATE_ERROR = (
    "Work permit extension date is required when extension requested is checked."
)
EXTENSION_SHORTCUT_EVENT_ERROR = (
    "Checking an extension, or changing its filing date, needs an active "
    "immigration status event that overrides the permit and has a document "
    "or a reference number. Add that event first. The checkbox alone is not "
    "a proof trail."
)


def overall_priority(code):
    """Sort weight for an overall status code. Missing codes stay before compliant."""
    if code in PRIORITY_MAP:
        return PRIORITY_MAP[code]
    return PRIORITY_MAP["compliant"] - 1


def _get_sin_value(user):
    """
    Returns digits only from decrypted SIN.
    Legacy raw `sin` field is ignored.
    """
    raw = user.sin_plain or ""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def _days_until(target_date):
    if not target_date:
        return None
    return (target_date - date.today()).days


def event_has_permit_proof(event):
    """Document or reference number. Whitespace-only references do not count."""
    if event is None:
        return False
    if getattr(event, "document_file_id", None):
        return True
    document = getattr(event, "document_file", None)
    if getattr(document, "pk", None):
        return True
    return bool((getattr(event, "reference_number", None) or "").strip())


def event_overrides_permit(event):
    """Active override-type event with a document or reference number."""
    if event is None or not getattr(event, "is_active", False):
        return False
    if not immigration_status_overrides_permit(getattr(event, "status_type", None)):
        return False
    return event_has_permit_proof(event)


def _override_label(event):
    getter = getattr(event, "get_status_type_display", None)
    if callable(getter):
        label = getter()
        if label:
            return label
    meta = IMMIGRATION_STATUS_TYPES.get(getattr(event, "status_type", None)) or {}
    return meta.get("label") or "Extension Pending"


def active_permit_override_event(user):
    """Latest active override event that has a document or reference number."""
    events = (
        user.immigration_status_events.filter(
            is_active=True,
            status_type__in=OVERRIDE_PERMIT_STATUS_TYPES,
        )
        .select_related("document_file")
        .order_by(F("effective_date").desc(nulls_last=True), "-created_at")
    )
    for event in events:
        if event_overrides_permit(event):
            return event
    return None


def extension_shortcut_error(user, requested, extension_date, changed_fields):
    """
    Admin shortcut guard.

    Existing checked rows can still be saved when the extension fields are
    not part of this edit. Turning the shortcut on, or changing its date,
    requires a filing date and a proof event.
    """
    changed = set(changed_fields or [])
    if not (
        {"work_permit_extension_requested", "work_permit_extension_date"} & changed
    ):
        return None
    if not requested:
        return None
    if not extension_date:
        return EXTENSION_SHORTCUT_DATE_ERROR
    if not getattr(user, "pk", None) or not active_permit_override_event(user):
        return EXTENSION_SHORTCUT_EVENT_ERROR
    return None


def _sin_status(user):
    sin_value = _get_sin_value(user)

    if not sin_value:
        return {
            "code": "missing",
            "label": "Missing SIN",
            "pill_class": "danger",
            "is_temporary": False,
            "expiry": None,
            "days_left": None,
        }

    # TEMPORARY SIN (starts with 9)
    if sin_value.startswith("9"):
        sin_expiry = getattr(user, "sin_expiration_date", None)
        days_left = _days_until(sin_expiry)

        if not sin_expiry:
            return {
                "code": "missing_expiry",
                "label": "Temporary SIN",
                "pill_class": "warning",
                "is_temporary": True,
                "expiry": sin_expiry,
                "days_left": days_left,
            }

        if days_left is not None and days_left < 0:
            return {
                "code": "expired",
                "label": "Temporary SIN Expired",
                "pill_class": "danger",
                "is_temporary": True,
                "expiry": sin_expiry,
                "days_left": days_left,
            }

        if days_left is not None and days_left <= HR_WATCH_DAYS:
            return {
                "code": "expiring_soon",
                "label": HR_WATCH_LABEL,
                "pill_class": "warning",
                "is_temporary": True,
                "expiry": sin_expiry,
                "days_left": days_left,
            }

        return {
            "code": "temporary",
            "label": "Temporary SIN",
            "pill_class": "warning",
            "is_temporary": True,
            "expiry": sin_expiry,
            "days_left": days_left,
        }

    # PERMANENT SIN (everything else)
    return {
        "code": "permanent",
        "label": "Permanent SIN",
        "pill_class": "success",
        "is_temporary": False,
        "expiry": None,
        "days_left": None,
    }


def _permit_status(user, sin_info, override_event=None):
    expiry = getattr(user, "work_permit_expiration_date", None)
    days_left = _days_until(expiry)

    # Proof event drives maintained-status style ranking. The checkbox below
    # remains a fallback for rows filed before events were required.
    if event_overrides_permit(override_event):
        return {
            "code": "extension_pending",
            "label": _override_label(override_event),
            "pill_class": "primary",
            "expiry": expiry,
            "days_left": days_left,
            "source": "event",
        }

    if user.work_permit_extension_requested:
        return {
            "code": "extension_pending",
            "label": "Extension Pending",
            "pill_class": "primary",
            "expiry": expiry,
            "days_left": days_left,
            "source": "checkbox",
        }

    if not sin_info["is_temporary"]:
        return {
            "code": "not_required",
            "label": "Permit Not Required",
            "pill_class": "dark",
            "expiry": expiry,
            "days_left": days_left,
            "source": None,
        }

    if not expiry:
        return {
            "code": "missing_expiry",
            "label": "Missing Permit Expiry",
            "pill_class": "danger",
            "expiry": expiry,
            "days_left": days_left,
            "source": None,
        }

    if days_left is not None and days_left < 0:
        return {
            "code": "expired",
            "label": "Expired",
            "pill_class": "danger",
            "expiry": expiry,
            "days_left": days_left,
            "source": None,
        }

    if days_left is not None and days_left <= HR_WATCH_DAYS:
        return {
            "code": "expiring_soon",
            "label": HR_WATCH_LABEL,
            "pill_class": "warning",
            "expiry": expiry,
            "days_left": days_left,
            "source": None,
        }

    return {
        "code": "valid",
        "label": "Valid",
        "pill_class": "success",
        "expiry": expiry,
        "days_left": days_left,
        "source": None,
    }


def _overall_status(sin_info, permit_info):
    # Extension / maintained-status path overrides SIN expiry.
    # A temporary SIN may expire while the employee remains work-authorized.
    if permit_info["code"] == "extension_pending":
        return {
            "code": "extension_pending",
            "label": permit_info.get("label") or "Extension Pending",
            "pill_class": "primary",
        }

    # Valid permit should also prevent expired SIN from becoming "urgent".
    if permit_info["code"] == "valid" and sin_info["is_temporary"]:
        if sin_info["code"] in {"expired", "expiring_soon", "missing_expiry"}:
            return {
                "code": "compliant_sin_update_needed",
                "label": "Compliant - SIN Update Needed",
                "pill_class": "warning",
            }

        return {
            "code": "compliant",
            "label": "Compliant",
            "pill_class": "success",
        }

    if sin_info["code"] in {"missing", "expired", "missing_expiry", "expiring_soon"}:
        if sin_info["code"] in {"expired", "missing_expiry"}:
            return {
                "code": "urgent",
                "label": "Urgent",
                "pill_class": "danger",
            }
        return {
            "code": "expiring_soon",
            "label": HR_WATCH_LABEL,
            "pill_class": "warning",
        }

    if permit_info["code"] in {"missing_expiry", "expired"}:
        return {
            "code": "urgent",
            "label": "Urgent",
            "pill_class": "danger",
        }

    if permit_info["code"] == "expiring_soon":
        return {
            "code": "expiring_soon",
            "label": HR_WATCH_LABEL,
            "pill_class": "warning",
        }

    return {
        "code": "compliant",
        "label": "Compliant",
        "pill_class": "success",
    }


def build_immigration_audit(employer, search_query="", flagged_only=False):
    employees = (
        employer.customuser_set.filter(is_active=True)
        .select_related("store")
        .order_by("-date_joined")
    )

    search_query = (search_query or "").strip()
    if search_query:
        employees = employees.filter(
            Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
            | Q(email__icontains=search_query)
        )

    rows = []

    for employee in employees:
        latest_event = (
            employee.immigration_status_events
            .filter(is_active=True)
            .order_by("-effective_date", "-created_at")
            .first()
        )

        permit_event = active_permit_override_event(employee)

        immigration_events = (
            employee.immigration_status_events
            .filter(is_active=True)
            .select_related("document_file", "created_by")
            .order_by("-effective_date", "-created_at")
        )
        sin_expiry = employee.sin_expiration_date
        sin_days = _days_until(sin_expiry)
        sin_info = _sin_status(employee)

        permit_expiry = employee.work_permit_expiration_date
        permit_days = _days_until(permit_expiry)

        permit_info = _permit_status(employee, sin_info, permit_event)
        overall = _overall_status(sin_info, permit_info)

        row = {
            "employee": employee,
            "sin_masked": employee.masked_sin(),
            "sin_expiry": sin_expiry,
            "sin_days": sin_days,
            "sin_info": sin_info,
            "permit_expiry": permit_expiry,
            "permit_days": permit_days,
            "permit_info": permit_info,
            "extension_requested": employee.work_permit_extension_requested,
            "extension_date": employee.work_permit_extension_date,
            "overall_status": overall,
            "is_flagged": overall.get("code") != "compliant",
            "latest_immigration_event": latest_event,
            "permit_event": permit_event,
            "immigration_events": immigration_events,
        }

        if flagged_only and not row["is_flagged"]:
            continue

        rows.append(row)

    rows.sort(
        key=lambda r: (
            overall_priority(r["overall_status"]["code"]),
            r["permit_days"] if r["permit_days"] is not None else 9999,
            -(r["employee"].date_joined.timestamp() if r["employee"].date_joined else 0),
        )
    )

    return {
        "immigration_rows": rows,
        "immigration_search": search_query,
        "immigration_flagged_only": flagged_only,
    }
