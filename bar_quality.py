"""Metric-local continuity gates. Missing bars are never filled or treated as flat."""
import copy
from trade_clock import Calendar, aware


def complete(rows, timeframe=None, calendar=None):
    if not rows:
        return False
    timeframe = timeframe or rows[-1].get('timeframe')
    if not timeframe:
        return True  # Legacy numerical helper, not a qualified provider series.
    cal = calendar or Calendar()
    try:
        times = [aware(r['bar_end']) for r in rows]
        if len(set(times)) != len(times) or times != sorted(times):
            return False
        if any(t not in cal.ends(t.date(), timeframe) for t in times):
            return False
        return cal.expected(times[0], times[-1], timeframe) == times[1:]
    except (ValueError, TypeError, KeyError):
        return False


def apply(rows, features, calendar=None):
    out = copy.deepcopy(features)
    if out.get('status') != 'ok':
        return out
    excluded = {}
    def valid(key, window):
        ok = complete(window, calendar=calendar)
        if not ok:
            excluded[key] = 'bar_window_gap'
        return ok
    for n in (1, 4, 20):
        if len(rows) > n and not valid('returns_pct.'+str(n), rows[-n-1:]):
            out['returns_pct'][str(n)] = None
    out['return_last_bar_pct'] = out['returns_pct']['1']
    if not valid('trend_simple', rows[-min(6, len(rows)):]):
        out['trend_simple'] = None
    for n in (5, 20):
        ma = out['moving_averages'][str(n)]
        if ma is not None:
            if not valid(f'moving_averages.{n}.value', rows[-n:]):
                ma['value'] = ma['distance_pct'] = None
            if not valid(f'moving_averages.{n}.slope', rows[-n-1:]):
                ma['slope_pct'] = None
    if not valid('range', rows[-21:]):
        out['range']['high'] = out['range']['low'] = out['range']['breakout_candidate'] = None
    if len(rows) >= 20 and not valid('range_amplitude_20_pct', rows[-20:]):
        out['range_amplitude_20_pct'] = None
    indices = {r['bar_end']:i for i,r in enumerate(rows)}
    out['local_pivots'] = [p for p in out['local_pivots'] if valid('local_pivots', rows[indices[p['at']]-1:indices[p['at']]+2])]
    out['window_issues'] = excluded
    out['return_intervals'] = {str(n): {'start':rows[-n-1]['bar_end'], 'end':rows[-1]['bar_end']}
                               for n in (1,4,20) if len(rows)>n}
    return out
