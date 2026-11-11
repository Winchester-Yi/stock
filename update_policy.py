"""On-demand update policies. Scheduled boundaries never certify exchange holidays."""
import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo
import market_calendar
import trade_clock

TZ = ZoneInfo('Asia/Shanghai')


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS refresh_status (
        key TEXT PRIMARY KEY, last_success TEXT, last_attempt TEXT, last_error TEXT,
        last_count INTEGER, metadata TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS refresh_attempts (
        key TEXT NOT NULL, recorded_at TEXT NOT NULL, status TEXT NOT NULL,
        last_success TEXT, last_error TEXT, PRIMARY KEY(key,recorded_at))''')


def state_at(db, key, as_of):
    parsed = as_of if isinstance(as_of, dt.datetime) else dt.datetime.fromisoformat(as_of)
    if parsed.tzinfo is None: raise ValueError('as_of must include timezone')
    stamp = parsed.astimezone(TZ).isoformat()
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refresh_attempts'").fetchone()
    if exists:
        row = db.execute('SELECT * FROM refresh_attempts WHERE key=? AND recorded_at<=? ORDER BY recorded_at DESC LIMIT 1', (key,stamp)).fetchone()
        if row: return {**dict(row), 'last_attempt':row['recorded_at'], 'coverage':'recorded'}
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refresh_status'").fetchone()
    row = db.execute('SELECT * FROM refresh_status WHERE key=? AND last_attempt<=?', (key,stamp)).fetchone() if exists else None
    return {**dict(row), 'coverage':'legacy_latest_only'} if row else {'coverage':'unknown'}



def timestamp(value):
    try:
        out = dt.datetime.fromisoformat(value)
        return out.astimezone(TZ) if out.tzinfo else None
    except (TypeError, ValueError):
        return None


def last_boundary(now, timeframe):
    if now.weekday() >= 5: return None
    return max((t for t in trade_clock.Calendar().ends(now.date(),timeframe) if t<=now),default=None)


def daily_boundary(now):
    day = now.replace(hour=15,minute=0,second=0,microsecond=0)
    if now < day: day -= dt.timedelta(days=1)
    while day.weekday() >= 5: day -= dt.timedelta(days=1)
    return day


def completed_boundary(now,timeframe):
    if timeframe=='daily': return daily_boundary(now)
    return last_boundary(now,timeframe) or daily_boundary(now)


def cache_valid(success, now, ttl, timeframe=None):
    stamp = timestamp(success)
    if not stamp or stamp > now: return False
    age = (now-stamp).total_seconds()
    if timeframe:
        boundary = last_boundary(now,timeframe)
        if stamp.date()!=now.date() or (boundary and stamp<boundary): return False
        session,_ = market_calendar.session_state(now,'CN')
        if boundary and session in ('lunch_break','closed') and age<43200: return True
    return age < ttl


def _valid(previous, metadata, now, ttl, policy, timeframe):
    stamp = timestamp(previous.get('last_success'))
    if not stamp or stamp > now: return False
    if policy == 'disclosure':
        return metadata.get('settled') is True or (stamp.date()==now.date() and (now-stamp).total_seconds()<ttl)
    if policy in ('daily','completed_bar'):
        end = timestamp(metadata.get('latest_bar_end'))
        if policy=='completed_bar' and not end:
            return cache_valid(previous.get('last_success'),now,ttl,timeframe)
        return bool(end and end<=now and end>=completed_boundary(now,timeframe or 'daily'))
    if policy == 'session_snapshot':
        state,_ = market_calendar.session_state(now,'CN')
        if state == 'trading':
            hour = 9 if now.hour < 12 else 13
            start = now.replace(hour=hour,minute=30 if hour==9 else 0,second=0,microsecond=0)
            return stamp>=start and (now-stamp).total_seconds()<ttl
        end = daily_boundary(now)
        if now.weekday()<5 and (11,30)<=(now.hour,now.minute)<(13,0):
            end=now.replace(hour=11,minute=30,second=0,microsecond=0)
        return stamp>=end
    if policy != 'ttl': raise ValueError('unknown update policy: '+policy)
    return cache_valid(previous.get('last_success'),now,ttl,timeframe)


def acquire(db, key, now, ttl, fn, timeframe=None, force=False, *, policy='ttl', retry_seconds=0, refresh_due=False):
    if now.tzinfo is None: raise ValueError('update time must include timezone')
    now=now.astimezone(TZ)
    setup(db)
    row=db.execute('SELECT * FROM refresh_status WHERE key=?',(key,)).fetchone()
    previous=dict(row) if row else {}
    metadata=json.loads(previous.get('metadata') or '{}')
    def remember_previous():
        # Keep a known legacy state before its mutable row is overwritten.
        old_attempt = timestamp(previous.get('last_attempt'))
        if old_attempt and old_attempt <= now:
            db.execute('INSERT OR IGNORE INTO refresh_attempts VALUES (?,?,?,?,?)',
                       (key,old_attempt.isoformat(),'failed' if previous.get('last_error') else 'updated',
                        previous.get('last_success'),previous.get('last_error')))

    base={'key':key,'policy':policy,'last_success':previous.get('last_success'),
          'last_attempt':previous.get('last_attempt'),'last_error':previous.get('last_error'),
          'count':previous.get('last_count'),'metadata':metadata}
    if not force:
        attempt=timestamp(previous.get('last_attempt'))
        if previous.get('last_error') and attempt and 0<=(now-attempt).total_seconds()<retry_seconds:
            return {**base,'status':'deferred','reason':'retry_cooldown',
                    'retry_at':(attempt+dt.timedelta(seconds=retry_seconds)).isoformat(),
                    'stale_available':bool(previous.get('last_success'))}
        if not previous.get('last_error'):
            if not refresh_due and _valid(previous,metadata,now,ttl,policy,timeframe):
                return {**base,'status':'cached','reason':'policy_satisfied'}
            success=timestamp(previous.get('last_success'))
            expected=completed_boundary(now,timeframe or 'daily') if policy in ('daily','completed_bar') else None
            if expected and success and success>=expected and 0<=(now-success).total_seconds()<retry_seconds:
                return {**base,'status':'pending','reason':'waiting_for_completed_bar',
                        'expected_bar_end':expected.isoformat(),
                        'retry_at':(success+dt.timedelta(seconds=retry_seconds)).isoformat()}
    try:
        result=fn()
        if isinstance(result,int): result={'count':result}
        if not isinstance(result,dict): raise ValueError('invalid collector result')
        completed=dt.datetime.now(TZ).isoformat()
        count=result.get('count')
        db.execute('''INSERT INTO refresh_status VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
            last_success=excluded.last_success,last_attempt=excluded.last_attempt,last_error=NULL,
            last_count=excluded.last_count,metadata=excluded.metadata''',
            (key,completed,completed,None,count,json.dumps(result,ensure_ascii=False,allow_nan=False)))
        remember_previous()
        db.execute('INSERT OR REPLACE INTO refresh_attempts VALUES (?,?,?,?,?)', (key,completed,'updated',completed,None))
        db.commit()
        return {'key':key,'policy':policy,'status':'updated','last_success':completed,
                'last_attempt':completed,'last_error':None,'count':count,'metadata':result}
    except Exception as exc:
        db.rollback()
        completed=dt.datetime.now(TZ).isoformat(); error=f'{type(exc).__name__}: {exc}'
        db.execute('''INSERT INTO refresh_status(key,last_attempt,last_error) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET last_attempt=excluded.last_attempt,last_error=excluded.last_error''',
            (key,completed,error))
        remember_previous()
        db.execute('INSERT OR REPLACE INTO refresh_attempts VALUES (?,?,?,?,?)', (key,completed,'failed',previous.get('last_success'),error))
        db.commit()
        return {**base,'status':'failed','last_attempt':completed,'last_error':error,
                'retry_at':(dt.datetime.fromisoformat(completed)+dt.timedelta(seconds=retry_seconds)).isoformat(),
                'stale_available':bool(previous.get('last_success'))}


def news_key(queries):
    digest=hashlib.sha256(json.dumps(queries,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    return 'news:'+digest


def seconds(config,name,default,section='refresh_ttl_seconds'):
    value=config.get(section,{}).get(name,default)
    if not isinstance(value,int) or isinstance(value,bool) or not 1<=value<=604800:
        raise ValueError('invalid '+section+'.'+name)
    return value
