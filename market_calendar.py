"""Exchange session schedules (not a complete exchange holiday calendar)."""
import datetime as dt
from zoneinfo import ZoneInfo

ZONES = {
    "CN": ZoneInfo("Asia/Shanghai"),
    "JP": ZoneInfo("Asia/Tokyo"),
    "KR": ZoneInfo("Asia/Seoul"),
}
# Cash-market regular trading windows, expressed in the exchange's local time.
SESSIONS = {
    "CN": (("09:30", "11:30"), ("13:00", "15:00")),
    "JP": (("09:00", "11:30"), ("12:30", "15:30")),
    "KR": (("09:00", "15:30"),),
}


def session_state(at, market, holidays=None):
    """Return (state, calendar_verified). Weekdays are schedules, not confirmed trading days.

    Pass a *verified* set of local YYYY-MM-DD exchange holidays to recognize holidays.
    Without an authoritative holiday list, calendar_verified is False on weekdays.
    """
    local = at.astimezone(ZONES[market])
    if local.weekday() >= 5:
        return "weekend", True
    if holidays is not None and local.date().isoformat() in holidays:
        return "holiday", True
    clock = local.strftime("%H:%M")
    windows = SESSIONS[market]
    if any(start <= clock < end for start, end in windows):
        return "trading", False
    if len(windows) > 1 and windows[0][1] <= clock < windows[1][0]:
        return "lunch_break", False
    return "closed", False


def trading_minutes_between(start, end, market):
    """Scheduled regular-session minutes between two timestamps, same local date only.

    Does not verify holidays; caller must separately check exchange calendar.
    """
    a, b = start.astimezone(ZONES[market]), end.astimezone(ZONES[market])
    if a.date() != b.date() or b <= a or a.weekday() >= 5:
        return 0.0
    total = 0.0
    for start_hm, end_hm in SESSIONS[market]:
        h1, m1 = map(int, start_hm.split(":"))
        h2, m2 = map(int, end_hm.split(":"))
        left = a.replace(hour=h1, minute=m1, second=0, microsecond=0)
        right = a.replace(hour=h2, minute=m2, second=0, microsecond=0)
        total += max(0.0, (min(b, right) - max(a, left)).total_seconds() / 60)
    return total
