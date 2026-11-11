"""Operator-attested price-basis coverage, not an automatic corporate-action feed.

Records are append-only and available only after checked_at. Unknown cross-day
comparisons are masked; original bars are never changed or back-adjusted.
"""
import copy
import datetime as dt
import json
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def record(db, symbol, start, end, ex_dates, source_url, checked_at):
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError('stock symbol must be six digits')
    start, end = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if checked_at.tzinfo is None:
        raise ValueError('checked_at must include timezone')
    checked_at = checked_at.astimezone(TZ)
    if start > end or end > checked_at.date():
        raise ValueError('invalid coverage interval or coverage ends in future')
    parsed = urlparse(source_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('source_url must be an HTTPS source without credentials')
    dates = sorted({dt.date.fromisoformat(x) for x in ex_dates})
    if any(not start <= x <= end for x in dates):
        raise ValueError('ex_date outside attested coverage')
    db.execute('INSERT INTO price_basis_checks VALUES (?,?,?,?,?,?)',
               (symbol, start.isoformat(), end.isoformat(), checked_at.isoformat(),
                source_url, json.dumps([x.isoformat() for x in dates])))
    db.commit()
    return {'symbol': symbol, 'coverage_start': start.isoformat(), 'coverage_end': end.isoformat(),
            'checked_at': checked_at.isoformat(), 'source_url': source_url,
            'ex_dates': [x.isoformat() for x in dates], 'verification': 'operator_attested'}


def checks_at(db, symbol, as_of):
    if as_of.tzinfo is None:
        raise ValueError('as_of must include timezone')
    as_of = as_of.astimezone(TZ)
    # Corrections replace only the same declared interval, never a past version.
    rows = db.execute('''SELECT * FROM price_basis_checks WHERE symbol=? AND checked_at<=?
                         ORDER BY checked_at DESC''', (symbol, as_of.isoformat())).fetchall()
    seen = set()
    out = []
    for row in rows:
        key = (row['coverage_start'], row['coverage_end'])
        if key not in seen:
            seen.add(key)
            out.append(dict(row))
    return out


def check(db, symbol, start, end, as_of, *, records=None):
    start, end = str(start)[:10], str(end)[:10]
    if start > end:
        raise ValueError('invalid price comparison interval')
    records = checks_at(db, symbol, as_of) if records is None else records
    known = sorted({date for r in records for date in json.loads(r['ex_dates_json'])
                    if start < date <= end})
    if known:
        return {'status': 'crosses_ex_date', 'ex_dates': known}
    if start == end:
        return {'status': 'same_session', 'ex_dates': []}
    coverage = next((r for r in records if r['coverage_start'] <= start and r['coverage_end'] >= end), None)
    if coverage:
        return {'status': 'verified', 'ex_dates': [], 'verification': 'operator_attested',
                'source_url': coverage['source_url'], 'checked_at': coverage['checked_at']}
    return {'status': 'unverified', 'ex_dates': []}


def apply(db, symbol, rows, features, as_of):
    """Mask each feature using its *actual* dependency interval (MA != slope)."""
    out = copy.deepcopy(features)
    records = checks_at(db, symbol, as_of)
    excluded = {}

    def eligible(name, window):
        if not window:
            return False
        result = check(db, symbol, window[0]['bar_end'], window[-1]['bar_end'], as_of, records=records)
        if result['status'] in ('unverified', 'crosses_ex_date'):
            excluded[name] = result['status']
            return False
        return True

    if out.get('status') == 'ok':
        for n in (1, 4, 20):
            if len(rows) > n and not eligible(f'returns_pct.{n}', rows[-n-1:]):
                out['returns_pct'][str(n)] = None
        out['return_last_bar_pct'] = out['returns_pct']['1']
        if not eligible('trend_simple', rows[-min(6, len(rows)):]):
            out['trend_simple'] = None
        for n in (5, 20):
            value = out['moving_averages'][str(n)]
            if value is None:
                continue
            if not eligible(f'moving_averages.{n}.value', rows[-n:]):
                value['value'] = value['distance_pct'] = None
            if not eligible(f'moving_averages.{n}.slope', rows[-n-1:]):
                value['slope_pct'] = None
        if not eligible('range', rows[-21:]):
            out['range']['high'] = out['range']['low'] = out['range']['breakout_candidate'] = None
        if len(rows) >= 20 and not eligible('range_amplitude_20_pct', rows[-20:]):
            out['range_amplitude_20_pct'] = None
        pivots = []
        indices = {r['bar_end']: i for i, r in enumerate(rows)}
        for pivot in out['local_pivots']:
            i = indices[pivot['at']]
            if eligible('local_pivots', rows[i-1:i+2]):
                pivots.append(pivot)
        out['local_pivots'] = pivots
        same = [r for r in rows[:-1] if r['bar_end'][11:16] == rows[-1]['bar_end'][11:16]
                and r['amount'] is not None and r['amount'] > 0]
        if same and not eligible('same_time_amount_ratio', [same[0], rows[-1]]):
            out['same_time_amount_ratio'] = None
    overall = check(db, symbol, rows[0]['bar_end'], rows[-1]['bar_end'], as_of, records=records) if rows else {'status': 'unverified', 'ex_dates': []}
    out['price_basis'] = {**overall, 'excluded_metrics': excluded,
                          'notice': 'operator coverage only; no automatic corporate-action verification'}
    return out
