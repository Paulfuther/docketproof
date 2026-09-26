import csv
from io import StringIO

from django.db.models import Q
from django.utils import timezone

from .constants import IMMIGRATION_STATUS_TYPES

# User-facing label when a checkbox or an overrides-permit event
# keeps the employee work-authorized. The status code stays
# extension_pending so audit ranking does not move.
AUTHORIZED_EXTENSION_LABEL = "Authorized - extension on file"


def _get_sin_value(user):
    """
    Returns digits only from decrypted SIN.
    Legacy raw `sin` field is ignored.
    """
    raw = user.sin_plain or ""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def requires_work_permit(user):
    """True only when the SIN is temporary (digits start with 9).

    Permanent SINs, a blank SIN, and a SIN that cannot be decrypted do
    not need a work permit. A leftover work_permit_expiration_date on
    those rows is not a milestone reminder.
    """
    return _get_sin_value(user).startswith("9")


def _days_until(target_date, today=None):
    if not target_date:
        return None
    today = today or timezone.localdate()
    return (target_date - today).days


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

        if days_left is not None and days_left <= 120:
            return {
                "code": "expiring_soon",
                "label": "Temporary SIN",
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


def permit_override_status_types():
    """Immigration event types that prove work authorization."""
    return [
        code
        for code, meta in IMMIGRATION_STATUS_TYPES.items()
        if meta.get("overrides_permit")
    ]


def _permit_status(user, sin_info, permit_override=False):
    expiry = getattr(user, "work_permit_expiration_date", None)
    days_left = _days_until(expiry)

    # Checkbox ("extension letter on file") or an active event whose
    # type has overrides_permit. Either one means authorized with proof.
    if user.work_permit_extension_requested or permit_override:
        return {
            "code": "extension_pending",
            "label": AUTHORIZED_EXTENSION_LABEL,
            "pill_class": "primary",
            "expiry": expiry,
            "days_left": days_left,
        }

    # existing logic continues below

    if not sin_info["is_temporary"]:
        return {
            "code": "not_required",
            "label": "Permit Not Required",
            "pill_class": "dark",
            "expiry": expiry,
            "days_left": days_left,
        }

    if not expiry:
        return {
            "code": "missing_expiry",
            "label": "Missing Permit Expiry",
            "pill_class": "danger",
            "expiry": expiry,
            "days_left": days_left,
        }

    if days_left is not None and days_left < 0:
        return {
            "code": "expired",
            "label": "Expired",
            "pill_class": "danger",
            "expiry": expiry,
            "days_left": days_left,
        }

    if days_left is not None and days_left <= 120:
        return {
            "code": "expiring_soon",
            "label": "Expiring Soon",
            "pill_class": "warning",
            "expiry": expiry,
            "days_left": days_left,
        }

    return {
        "code": "valid",
        "label": "Valid",
        "pill_class": "success",
        "expiry": expiry,
        "days_left": days_left,
    }


def _overall_status(sin_info, permit_info):
    # Extension / maintained-status path overrides SIN expiry.
    # A temporary SIN may expire while the employee remains work-authorized.
    # A permanent SIN leaves Compliant only for a real extension letter
    # (checkbox) or an active overrides-permit event, not a leftover
    # permit date.
    if permit_info["code"] == "extension_pending":
        return {
            "code": "extension_pending",
            "label": AUTHORIZED_EXTENSION_LABEL,
            "pill_class": "primary",
        }

    # Permanent SIN does not need a work permit. A leftover
    # work_permit_expiration_date — even one that is expired or inside
    # 120 days — must not flip the overall pill to expiring soon or urgent.
    if sin_info["code"] == "permanent":
        return {
            "code": "compliant",
            "label": "Compliant",
            "pill_class": "success",
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
            "label": "Expiring Soon",
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
            "label": "Expiring Soon",
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
    override_types = permit_override_status_types()

    for employee in employees:
        latest_event = (
            employee.immigration_status_events
            .filter(is_active=True)
            .order_by("-effective_date", "-created_at")
            .first()
        )

        permit_event = (
            employee.immigration_status_events
            .filter(
                is_active=True,
                status_type__in=override_types,
            )
            .order_by("-created_at")
            .first()
        )

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

        permit_info = _permit_status(
            employee,
            sin_info,
            permit_override=permit_event is not None,
        )
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

    PRIORITY_MAP = {
        "urgent": 0,
        "expiring_soon": 1,
        "compliant_sin_update_needed": 1,
        "extension_pending": 2,
        "needs_review": 3,
        "compliant": 4,
    }

    def _rank_permit_days(row):
        # A permanent SIN is Permit Not Required. Do not rank that row
        # by a leftover work_permit_expiration_date.
        if row["permit_info"]["code"] == "not_required":
            return 9999
        if row["permit_days"] is None:
            return 9999
        return row["permit_days"]

    rows.sort(
        key=lambda r: (
            PRIORITY_MAP.get(r["overall_status"]["code"], 99),
            _rank_permit_days(r),
            -(r["employee"].date_joined.timestamp() if r["employee"].date_joined else 0),
        )
    )

    return {
        "immigration_rows": rows,
        "immigration_search": search_query,
        "immigration_flagged_only": flagged_only,
    }


IMMIGRATION_EXPORT_COLUMNS = (
    ("name", "Name"),
    ("email", "Email"),
    ("store", "Store"),
    ("employer", "Employer"),
    ("masked_sin", "Masked SIN"),
    ("sin_type", "SIN Type"),
    ("sin_expiry", "SIN Expiry"),
    ("sin_days_left", "SIN Days Left"),
    ("permit_expiry", "Permit Expiry"),
    ("permit_days_left", "Permit Days Left"),
    ("extension_authorized", "Extension Authorized"),
    ("overall_status", "Overall Status"),
    ("overall_status_code", "Overall Status Code"),
    ("latest_event_type", "Latest Event Type"),
    ("latest_event_effective_date", "Latest Event Effective Date"),
    ("latest_event_has_document", "Latest Event Has Document"),
    ("latest_event_has_reference", "Latest Event Has Reference"),
)


def _csv_date(value):
    return value.isoformat() if value else ""


def _csv_days(value):
    return "" if value is None else str(value)


def _csv_yes(value):
    return "Yes" if value else "No"


def _sin_type_for_export(sin_info):
    if sin_info["code"] == "missing":
        return "missing"
    if sin_info["is_temporary"]:
        return "temporary"
    return "permanent"


def immigration_audit_export_records(employer):
    """All active employees, in the same order as the unfiltered audit."""
    audit = build_immigration_audit(employer)
    employer_name = employer.name if employer else ""
    records = []
    for row in audit["immigration_rows"]:
        employee = row["employee"]
        event = row["latest_immigration_event"]
        store = ""
        if employee.store_id and employee.store is not None:
            store = str(employee.store.number)
        full_name = (employee.get_full_name() or "").strip()
        records.append(
            {
                "name": full_name or employee.username or "",
                "email": employee.email or "",
                "store": store,
                "employer": employer_name,
                "masked_sin": row["sin_masked"],
                "sin_type": _sin_type_for_export(row["sin_info"]),
                "sin_expiry": _csv_date(row["sin_expiry"]),
                "sin_days_left": _csv_days(row["sin_days"]),
                "permit_expiry": _csv_date(row["permit_expiry"]),
                "permit_days_left": _csv_days(row["permit_days"]),
                "extension_authorized": _csv_yes(
                    row["overall_status"]["code"] == "extension_pending"
                ),
                "overall_status": row["overall_status"]["label"],
                "overall_status_code": row["overall_status"]["code"],
                "latest_event_type": (
                    event.get_status_type_display() if event else ""
                ),
                "latest_event_effective_date": (
                    _csv_date(event.effective_date) if event else ""
                ),
                "latest_event_has_document": _csv_yes(
                    bool(event and event.document_file_id)
                ),
                "latest_event_has_reference": _csv_yes(
                    bool(event and (event.reference_number or "").strip())
                ),
            }
        )
    return records


def render_immigration_audit_csv(employer):
    """Excel-openable CSV. Caller adds a UTF-8 BOM for Excel."""
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _key, label in IMMIGRATION_EXPORT_COLUMNS])
    for record in immigration_audit_export_records(employer):
        writer.writerow(
            [record[key] for key, _label in IMMIGRATION_EXPORT_COLUMNS]
        )
    return buffer.getvalue()
