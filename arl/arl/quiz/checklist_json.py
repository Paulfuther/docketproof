"""Parse checklist JSON exports into template name, description, and items.

Keep this module free of Django so parser tests can run without a database.
Field limits match ChecklistTemplate.name (200) and ChecklistTemplateItem.text (500).
"""

from __future__ import annotations

from dataclasses import dataclass
import json

TEMPLATE_NAME_MAX = 200
ITEM_TEXT_MAX = 500
MAX_IMPORT_BYTES = 5 * 1024 * 1024


class ChecklistImportError(ValueError):
    """Raised when a JSON payload cannot be turned into a checklist template."""


@dataclass(frozen=True)
class ParsedChecklistItem:
    text: str
    requires_photo: bool
    order: int


@dataclass(frozen=True)
class ParsedChecklistTemplate:
    name: str
    description: str
    items: tuple[ParsedChecklistItem, ...]


def _first_nonempty_text(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return default


def _item_requires_photo(entry: dict) -> bool:
    """Map requires_photo | photo_required. Ignore allow_photo."""
    if "requires_photo" in entry:
        return _as_bool(entry.get("requires_photo"), default=False)
    if "photo_required" in entry:
        return _as_bool(entry.get("photo_required"), default=False)
    return False


def _item_order(entry: dict, index: int) -> int:
    value = entry.get("order")
    if value is None or value == "":
        return index
    try:
        order = int(value)
    except (TypeError, ValueError):
        return index
    return order if order >= 0 else index


def parse_checklist_template_payload(data) -> ParsedChecklistTemplate:
    """Map a checklist JSON object to template name, description, and items."""
    if not isinstance(data, dict):
        raise ChecklistImportError("JSON root must be an object.")

    name = _first_nonempty_text(
        data.get("name"),
        data.get("title"),
        data.get("document"),
    )
    if not name:
        raise ChecklistImportError(
            "Template name is required (use name, title, or document)."
        )
    name = name[:TEMPLATE_NAME_MAX]

    description = _first_nonempty_text(
        data.get("description"),
        data.get("notes"),
        data.get("purpose"),
    )

    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise ChecklistImportError("JSON must include an items array.")

    items = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            continue
        text = _first_nonempty_text(entry.get("text"), entry.get("title"))
        if not text:
            continue
        items.append(
            ParsedChecklistItem(
                text=text[:ITEM_TEXT_MAX],
                requires_photo=_item_requires_photo(entry),
                order=_item_order(entry, index),
            )
        )

    if not items:
        raise ChecklistImportError("No checklist items found in items[].")

    return ParsedChecklistTemplate(
        name=name,
        description=description,
        items=tuple(items),
    )


def parse_checklist_template_bytes(raw: bytes) -> ParsedChecklistTemplate:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ChecklistImportError("File must be UTF-8 JSON.") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChecklistImportError(f"Invalid JSON: {exc.msg}") from exc
    return parse_checklist_template_payload(data)
