"""Text, number and time formatting shared by every page.

Nothing here reads state or touches a broker: each function turns a value that
was *already* read into the exact string a page may show, and refuses to invent
one when it is missing.  "Unavailable" with a reason is a valid answer; a zero is
not, unless zero is what was measured.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any, Iterable, Mapping

UNKNOWN = "Unavailable"
EMPTY = "—"


def text(value: Any) -> str:
    """Escape anything for HTML. ``None`` becomes an explicit absence, not "None"."""

    if value is None:
        return EMPTY
    return escape(str(value))


def attr(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def plain(value: Any) -> str:
    """A single line of text: no newlines, no runs of spaces."""

    return " ".join(str(value if value is not None else "").split())


def money(value: Any, currency: str = "") -> str:
    if value is None or str(value).strip() == "":
        return EMPTY
    suffix = f" {currency}".rstrip()
    return f"{plain(value)}{suffix}"


def quantity(value: Any) -> str:
    return EMPTY if value is None or str(value).strip() == "" else plain(value)


def instant(value: Any) -> datetime | None:
    """Parse an ISO instant, tolerating the forms the read model already holds."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utc(value: Any) -> str:
    """An absolute UTC instant. Every time on every page is written this way."""

    parsed = instant(value)
    if parsed is None:
        return EMPTY
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def utc_short(value: Any) -> str:
    parsed = instant(value)
    if parsed is None:
        return EMPTY
    return parsed.astimezone(timezone.utc).strftime("%H:%M:%S")


def age(value: Any, read_at: Any) -> str:
    """How old a value is, measured against the moment the page read its source."""

    stamp, moment = instant(value), instant(read_at)
    if stamp is None:
        return "no timestamp"
    if moment is None:
        return "age unavailable"
    seconds = max(0.0, (moment - stamp).total_seconds())
    if seconds < 1:
        return "just now"
    if seconds < 90:
        return f"{seconds:.0f} s old"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min old"
    return f"{seconds / 3600:.1f} h old"


def age_seconds(value: Any, read_at: Any) -> float | None:
    stamp, moment = instant(value), instant(read_at)
    if stamp is None or moment is None:
        return None
    return max(0.0, (moment - stamp).total_seconds())


def pct(value: Any, digits: int = 1) -> str:
    if value is None or str(value).strip() == "":
        return EMPTY
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return plain(value)


def joined(values: Iterable[Any], separator: str = " · ", *, suffix: str = "") -> str:
    parts = [plain(value) for value in values if value is not None and str(value).strip() != ""]
    if not parts:
        return EMPTY
    body = separator.join(parts)
    return f"{body} {suffix}".rstrip() if suffix else body


def or_reason(value: Any, available: bool, note: str) -> str:
    """A figure, or the sentence explaining why there is none."""

    if available and value is not None and str(value).strip() != "":
        return plain(value)
    return plain(note) if note else "Unavailable — this value cannot be read right now."


def tone_for(ok: bool, *, warn: bool = False, danger: bool = False) -> str:
    if danger:
        return "danger"
    if warn:
        return "warn"
    return "ok" if ok else "muted"


def bool_text(value: Any, yes: str = "yes", no: str = "no") -> str:
    return yes if value else no


def humanize_code(value: Any) -> str:
    """``MAX_DAILY_LOSS_EXCEEDED`` → ``Max daily loss exceeded`` (display only)."""

    raw = plain(value).replace("-", "_")
    if not raw:
        return EMPTY
    words = raw.split("_")
    sentence = " ".join(word.lower() for word in words if word)
    return sentence[:1].upper() + sentence[1:] if sentence else EMPTY


def first(mapping: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    """The first present, non-empty key — for shape-tolerant reads of view dicts."""

    for key in keys:
        if not mapping:
            break
        value = mapping.get(key)
        if value not in (None, "", [], {}, ()):
            return value
    return default
