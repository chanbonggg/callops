"""Conservative Korean date/time interpretation for single timed candidates."""

from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


CALENDAR_DATE = r"(?:(?P<year>[0-9]{4})년\s*)?(?P<month>[0-9]{1,2})월\s*(?P<day>[0-9]{1,2})일"
DATE_PHRASES = re.compile(
    rf"{CALENDAR_DATE}|[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}|(?<![0-9])(?P<short_day>[0-9]{{1,2}}일)"
    r"|(?:이번|다음|지난)\s*(?:주|달)(?:\s*[월화수목금토일]요일)?(?:쯤)?"
    r"|(?:어제|오늘|내일|모레|글피)(?:쯤)?|[월화수목금토일]요일"
)
CLOCK = r"(?:(?:오전|오후|아침|새벽|저녁|밤)\s*)?[0-9]{1,2}(?::[0-9]{2}|시\s*(?:반|[0-9]{1,2}분)?)"
CLOCK_PARTS = re.compile(
    r"(?:(오전|오후|아침|새벽|저녁|밤)\s*)?([0-9]{1,2})(?::([0-9]{2})|시\s*(반|[0-9]{1,2}분)?)"
)
CLOCK_RANGE = re.compile(
    rf"({CLOCK})\s*(?:부터|[~～\-–])\s*"
    rf"(?:(다음\s*날|익일|(?:[0-9]{{4}}년\s*)?[0-9]{{1,2}}월\s*[0-9]{{1,2}}일)\s*)?"
    rf"({CLOCK})\s*(?:까지|로)?"
)
DAY_REPLACEMENT = re.compile(
    r"(?<![0-9])([0-9]{1,2})일\s*대신\s*([0-9]{1,2}일)([^.!?]*?)"
    r"(?:변경(?:합니다|하겠습니다|하죠|했습니다)|바꾸(?:겠습니다|죠))(?=[\s.!?]|$)"
)
RELATIVE_DAYS = {"어제": -1, "오늘": 0, "내일": 1, "모레": 2}
WEEKDAYS = "월화수목금토일"
UNSUPPORTED = re.compile(r"종일|매일|매주|매월|매년|격주|반복")


def date_choices(segments):
    matches = [match for segment in segments for match in DATE_PHRASES.finditer(segment.text)]
    if any(match["short_day"] for match in matches) and any(match["month"] for match in matches):
        return list(dict.fromkeys(match[0] for match in matches))
    return []


def resolve_date(raw, local_date, evidence):
    if not raw:
        return None
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            return date.fromisoformat(raw)
        if raw in RELATIVE_DAYS:
            return local_date + timedelta(days=RELATIVE_DAYS[raw]) if local_date else None
        match = re.fullmatch(r"(이번|다음)\s*주\s*([월화수목금토일])요일", raw)
        if match and local_date:
            week = 7 if match[1] == "다음" else 0
            return local_date + timedelta(days=week + WEEKDAYS.index(match[2]) - local_date.weekday())
        match = re.fullmatch(CALENDAR_DATE, raw)
        if match:
            year = int(match["year"]) if match["year"] else None
            if year is None and local_date:
                year = local_date.year
            return date(year, int(match["month"]), int(match["day"])) if year else None
        match = re.fullmatch(r"([0-9]{1,2})일", raw)
        if match:
            months = {
                (int(d["year"]), int(d["month"]))
                for text in evidence for d in re.finditer(CALENDAR_DATE, text) if d["year"]
            }
            if len(months) == 1:
                year, month = months.pop()
                return date(year, month, int(match[1]))
    except (ValueError, OverflowError):
        return None
    return None


def resolve_time(raw, period=None):
    match = CLOCK_PARTS.fullmatch(raw.strip())
    if match is None:
        return None, None
    marker, hour, digits, minutes = match.groups()
    hour = int(hour)
    minute = int(digits) if digits else 30 if minutes == "반" else int(minutes[:-1]) if minutes else 0
    if marker:
        period = "am" if marker in {"오전", "아침", "새벽"} else "pm"
        if not 1 <= hour <= 12 or (marker == "밤" and hour == 12):
            return None, None
    if period is not None and 1 <= hour <= 12:
        hour = hour % 12 + (12 if period == "pm" else 0)
    elif not marker and hour < 13 and hour != 0 and digits is None:
        # Bare Korean hours such as "3시" need an AM/PM cue.
        return None, None
    try:
        return time(hour, minute), period
    except ValueError:
        return None, None


def local_datetime(day, clock, zone):
    if not (day and clock and zone):
        return None
    naive = datetime.combine(day, clock)
    before = naive.replace(tzinfo=zone, fold=0)
    after = naive.replace(tzinfo=zone, fold=1)
    # A transition can make a wall time ambiguous or nonexistent.
    return before if before.utcoffset() == after.utcoffset() else None


def revised_event(event, local_date, evidence):
    """Find a single cited day replacement grounded in an earlier date."""
    if event.status == "cancelled" or not event.date_text:
        return event, False
    selected = resolve_date(event.date_text, local_date, evidence)
    if selected is None:
        return event, False
    earlier_dates = set()
    changes = []
    for text in evidence:
        for match in DAY_REPLACEMENT.finditer(text):
            old = resolve_date(f"{match[1]}일", local_date, evidence)
            new = resolve_date(match[2], local_date, evidence)
            if old in earlier_dates and new is not None and old != new and selected in (old, new):
                changes.append((match, new))
        for phrase in DATE_PHRASES.finditer(text):
            day = resolve_date(phrase[0], local_date, evidence)
            if day is not None:
                earlier_dates.add(day)
    if len(changes) != 1:
        return event, False
    change, new = changes[0]
    if selected == new:
        return event, True
    clock = CLOCK_RANGE.fullmatch(change[3].strip())
    return event.model_copy(update={"date_text": change[2], "time_text": clock[0] if clock else None}), True


def event_times(event, zone, local_date, evidence):
    raw = event.time_text
    if not raw or any(UNSUPPORTED.search(text) for text in evidence):
        return None, None
    start_day = resolve_date(event.date_text, local_date, evidence)
    match = CLOCK_RANGE.fullmatch(raw)
    if not match:
        start, _ = resolve_time(raw)
        return local_datetime(start_day, start, zone), None
    start, period = resolve_time(match[1])
    end, _ = resolve_time(match[3], period)
    end_day = start_day
    if match[2]:
        if match[2].replace(" ", "") in {"다음날", "익일"}:
            end_day = None if start_day is None or start_day == date.max else start_day + timedelta(days=1)
        else:
            end_day = resolve_date(match[2], local_date, evidence)
    start_at = local_datetime(start_day, start, zone)
    end_at = local_datetime(end_day, end, zone)
    if start_at and end_at and end_at > start_at:
        return start_at, end_at
    return start_at, None


def task_deadline(raw, zone, local_date, evidence):
    if not raw:
        return None
    match = re.fullmatch(rf"(.+?)\s+({CLOCK})\s*(?:까지)?", raw)
    if match is None:
        return None
    clock, _ = resolve_time(match[2])
    return local_datetime(resolve_date(match[1], local_date, evidence), clock, zone)


def call_clock(transcript):
    zone = ZoneInfo(transcript.timezone) if transcript.timezone else None
    local_date = transcript.call_started_at.astimezone(zone).date() if zone and transcript.call_started_at else None
    return zone, local_date
