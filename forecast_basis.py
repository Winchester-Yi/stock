"""Attested corporate-action factors for research; raw stored quotes never change.

A factor multiplies PRE-event prices onto the event-date price scale. It is not
inferred from a price gap. Coverage must list every action, including unknown factors.
"""
import copy
import datetime as dt
import math
from trade_clock import aware, source_url


class BasisBook:
    def __init__(self, manifests=(), as_of=None):
        self.entries = {}
        for original in manifests:
            m = copy.deepcopy(original)
            if m.get('verification') != 'operator_attested':
                raise ValueError('basis coverage requires operator attestation')
            start, end = dt.date.fromisoformat(m['start']), dt.date.fromisoformat(m['end'])
            checked = aware(m['verified_at'])
            if not as_of or checked > aware(as_of) or start > end or end > checked.date():
                raise ValueError('basis coverage or availability invalid')
            source_url(m['source_url'])
            symbol = m['symbol']
            if len(symbol)!=6 or not symbol.isascii() or not symbol.isdigit() or symbol in self.entries:
                raise ValueError('invalid or duplicate basis symbol')
            seen = set()
            for event in m.get('actions', []):
                date = dt.date.fromisoformat(event['ex_date'])
                if not start <= date <= end or date in seen:
                    raise ValueError('duplicate or out-of-coverage action')
                seen.add(date)
                factor = event.get('pre_price_multiplier')
                if factor is not None and (isinstance(factor,bool) or not isinstance(factor,(float,int)) or not math.isfinite(factor) or factor<=0):
                    raise ValueError('invalid pre-event price multiplier')
                source_url(event.get('source_url',m['source_url']))
            self.entries[symbol] = m

    def scale(self, symbol, start, end):
        start, end = str(start)[:10], str(end)[:10]
        if start > end:
            raise ValueError('reversed price interval')
        if start == end:
            return 1.0
        m = self.entries.get(symbol)
        if not m or not m['start'] <= start <= end <= m['end']:
            return None
        factor = 1.0
        for event in m.get('actions',[]):
            if start < event['ex_date'] <= end:
                if event.get('pre_price_multiplier') is None:
                    return None
                factor *= event['pre_price_multiplier']
        return factor if math.isfinite(factor) and factor>0 else None

    def adjusted(self, symbol, rows):
        if not rows:
            return []
        out = copy.deepcopy(rows)
        anchor = rows[-1]['bar_end']
        for row in out:
            factor = self.scale(symbol,row['bar_end'],anchor)
            if factor is None:
                return None
            for key in ('open','high','low','close'):
                row[key] *= factor
        return out

    def payload(self):
        return list(self.entries.values())
