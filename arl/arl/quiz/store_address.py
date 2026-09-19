"""Store header fields for checklist PDF and HTML reports."""


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def format_store_address_line(store=None) -> str:
    """Join non-empty Store address parts. Store has no postal field."""
    if store is None:
        return ""
    parts = [
        _text(getattr(store, "address", None)),
        _text(getattr(store, "address_two", None)),
        _text(getattr(store, "city", None)),
        _text(getattr(store, "province", None)),
    ]
    return ", ".join(part for part in parts if part)


def store_report_context(store=None) -> dict:
    """Context used by checklist PDF and on-screen report headers."""
    return {
        "store_number": getattr(store, "number", None) if store else None,
        "store_name": getattr(store, "name", None) if store else None,
        "store_address": getattr(store, "address", None) if store else None,
        "store_address_two": getattr(store, "address_two", None) if store else None,
        "store_city": getattr(store, "city", None) if store else None,
        "store_province": getattr(store, "province", None) if store else None,
        "store_address_line": format_store_address_line(store),
    }
