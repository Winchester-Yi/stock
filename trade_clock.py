"""Explicit CN exchange clock. Attested coverage is never inferred from weekdays."""
import datetime as dt
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
WINDOWS = ((570, 690), (780, 900))
TIMEFRAMES = ('1', '5', '15', '60', 'daily')


def aware(value):
    out = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    if out.tzinfo is None:
        raise ValueError('timezone required')
    return out.astimezone(TZ)


def source_url(value):
    p = urlparse(value)
    if p.scheme != 'https' or not p.netloc or p.username or p.password:
        raise ValueError('HTTPS provenance without credentials required')
    return value


@dataclass(frozen=True)
class Calendar:
    manifest: dict | None = None

    @classmethod
    def load(cls, value, as_of):
        if value is None:
            return cls()
        if value.get('verification') != 'operator_attested':
            raise ValueError('calendar must be operator_attested; downloading is not verification')
        start, end = dt.date.fromisoformat(value['start']), dt.date.fromisoformat(value['end'])
        if start > end or aware(value['verified_at']) > aware(as_of):
            raise ValueError('calendar coverage or availability invalid')
        source_url(value['source_url'])
        dates = [dt.date.fromisoformat(x) for x in value['open_dates']]
        if len(dates) != len(set(dates)) or any(not start <= x <= end for x in dates):
            raise ValueError('calendar dates outside coverage or duplicated')
        return cls(dict(value, open_dates=sorted(x.isoformat() for x in dates)))

    def verified(self, date):
        return bool(self.manifest and self.manifest['start'] <= date.isoformat() <= self.manifest['end'])

    def is_open(self, date):
        if self.manifest and self.verified(date):
            return date.isoformat() in self.manifest['open_dates']
        return date.weekday() < 5  # Schedule estimate only.

    def next_day(self, date, offset=1):
        for _ in range(370):
            date += dt.timedelta(days=1)
            if not self.verified(date):
                raise ValueError('calendar coverage missing for target session')
            if self.is_open(date):
                offset -= 1
                if offset == 0:
                    return date
        raise ValueError('target session outside calendar range')

    def state(self, at):
        at = aware(at)
        if not self.is_open(at.date()):
            return 'closed'
        minute = at.hour * 60 + at.minute
        if any(a <= minute < b for a, b in WINDOWS):
            return 'trading'
        return 'lunch_break' if 690 <= minute < 780 else 'preopen' if minute<570 else 'closed'

    def ends(self, date, timeframe):
        if timeframe not in TIMEFRAMES:
            raise ValueError('unsupported timeframe')
        if not self.is_open(date):
            return []
        minutes = [900] if timeframe == 'daily' else [630, 690, 840, 900] if timeframe == '60' else [m for a, b in WINDOWS for m in range(a + int(timeframe), b + 1, int(timeframe))]
        return [dt.datetime.combine(date, dt.time(m // 60, m % 60), TZ) for m in minutes]

    def expected(self, start, end, timeframe, limit=20000):
        start, end = aware(start), aware(end)
        out, day = [], start.date()
        if end < start or (end.date() - day).days > 3700:
            raise ValueError('invalid or excessive clock window')
        while day <= end.date():
            out.extend(t for t in self.ends(day, timeframe) if start < t <= end)
            if len(out) > limit:
                raise ValueError('clock window exceeds row limit')
            day += dt.timedelta(days=1)
        return out

    def last_completed(self, at, timeframe):
        at=aware(at)
        day=at.date()
        for _ in range(370):
            candidates=[t for t in self.ends(day,timeframe) if t<=at]
            if candidates:
                return candidates[-1]
            day-=dt.timedelta(days=1)
        raise ValueError('no completed session in clock coverage')

    def add_intraday(self, at, minutes):
        at = aware(at)
        if self.state(at) != 'trading' or not isinstance(minutes, int) or minutes <= 0:
            return None
        remaining, cursor = minutes * 60, at
        for a, b in WINDOWS:
            left = at.replace(hour=a // 60, minute=a % 60, second=0, microsecond=0)
            right = at.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)
            cursor = max(cursor, left)
            if cursor >= right:
                continue
            available = (right - cursor).total_seconds()
            if remaining <= available:
                return cursor + dt.timedelta(seconds=remaining)
            remaining -= available
            cursor = right
        return None  # Never silently spill an intraday forecast into tomorrow.


def snapshot_role(captured, as_of, ttl, calendar=None):
    """Real-time candidate, end-session context, or historical; not quote verification."""
    if not captured:
        return 'historical'
    captured, as_of = aware(captured), aware(as_of)
    if captured > as_of:
        return 'historical'
    cal = calendar or Calendar()
    if cal.state(as_of) == 'trading':
        start = as_of.replace(hour=9 if as_of.hour < 12 else 13,
                              minute=30 if as_of.hour < 12 else 0, second=0, microsecond=0)
        return 'live_candidate' if captured >= start and (as_of-captured).total_seconds() <= ttl else 'historical'
    end = cal.last_completed(as_of,'15')
    return 'session_context' if end <= captured <= as_of else 'historical'
