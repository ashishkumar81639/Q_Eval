"""IST clock, relative-time resolution and date spellings (build.md 4.3)."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

REL_RE = re.compile(r"^\s*([+-]?\d+)d\s+(\d{1,2}):(\d{2})\s*$")
REL_DATE_RE = re.compile(r"^\s*([+-]?\d+)\s*([dwmy])\s*$")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_FULL = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def to_ist(value: str | datetime) -> datetime:
    """Convert an ISO string or aware datetime to an IST-aware datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without offset: {value!r}")
    return dt.astimezone(IST)


def iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")


def is_relative(spec: str) -> bool:
    return bool(REL_RE.match(spec))


def has_offset(spec: str) -> bool:
    try:
        return datetime.fromisoformat(spec.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def valid_time_spec(spec: str) -> bool:
    return is_relative(spec) or has_offset(spec)


def resolve_time(spec: str, anchor: date, shift_days: int = 0) -> datetime:
    """`"-3d 20:00"` -> anchor-3 days at 20:00 IST; absolute ISO strings pass through (shifted)."""
    m = REL_RE.match(spec)
    if m:
        days, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = anchor + timedelta(days=days - shift_days)
        return datetime.combine(d, time(hh, mm), tzinfo=IST)
    if has_offset(spec):
        return to_ist(spec) - timedelta(days=shift_days)
    raise ValueError(f"bad time spec {spec!r}: use '-3d 20:00' or ISO 8601 with offset")


def future_shift_days(specs: list[str], anchor: date, now: datetime | None = None) -> int:
    """Whole days to shift a scenario back so no resolved time is in the future."""
    now = now or now_ist()
    if not specs:
        return 0
    latest = max(resolve_time(s, anchor) for s in specs)
    if latest <= now:
        return 0
    return (latest - now).days + 1 if (latest - now) > timedelta(0) else 0


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def resolve_date(spec: str, anchor: date) -> date:
    """`"-7w"` -> anchor - 7 weeks. Units: d, w, m (months), y."""
    m = REL_DATE_RE.match(spec)
    if not m:
        raise ValueError(f"bad date spec {spec!r}: use e.g. -7w, -26m, -3d, -40y")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return anchor + timedelta(days=n)
    if unit == "w":
        return anchor + timedelta(weeks=n)
    if unit == "m":
        return add_months(anchor, n)
    return add_months(anchor, 12 * n)


def date_spellings(d: date) -> list[str]:
    wd, wdf = WEEKDAYS[d.weekday()], WEEKDAYS_FULL[d.weekday()]
    mon, monf = MONTHS[d.month - 1], MONTHS_FULL[d.month - 1]
    day = d.day
    return [
        f"{wd} {day} {mon} {d.year}",
        f"{wdf} {day} {mon} {d.year}",
        f"{wd}, {day} {mon} {d.year}",
        f"{day} {mon} {d.year}",
        f"{day} {mon}",
        f"{day} {monf} {d.year}",
        f"{monf} {day}",
        f"{mon} {day}",
        f"{day:02d}/{d.month:02d}/{d.year}",
        f"{day:02d}/{d.month:02d}",
        d.isoformat(),
    ]


def date_regex(d: date) -> str:
    """Regex matching any common spelling of `d` (case-insensitive use expected)."""
    wd = f"(?:{WEEKDAYS[d.weekday()]}|{WEEKDAYS_FULL[d.weekday()]}),?\\s+"
    sept = "|Sept" if d.month == 9 else ""
    mon = f"(?:{MONTHS_FULL[d.month - 1]}{sept}|{MONTHS[d.month - 1]})\\.?"
    day = f"0?{d.day}(?:st|nd|rd|th)?"
    year = f"(?:,?\\s+{d.year})?"
    alts = [
        f"(?:{wd})?\\b{day}\\s+{mon}{year}\\b",
        f"\\b{mon}\\s+{day}{year}\\b",
        f"\\b0?{d.day}/0?{d.month}(?:/(?:{d.year}|{d.year % 100:02d}))?\\b",
        f"\\b{d.isoformat()}\\b",
    ]
    return "(?:" + "|".join(alts) + ")"


def fmt_ist(dt: datetime) -> str:
    """`Mon 28 Sep 2026, 8:10 PM IST`."""
    dt = dt.astimezone(IST)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{WEEKDAYS[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]} {dt.year}, {hour}:{dt:%M} {dt:%p} IST"
