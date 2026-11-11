"""Batch acquisition and compact, time-bounded evidence. No model or trade calls."""
import datetime as dt
from contextlib import contextmanager

import analysis
import lhb_collector
import market_calendar
import price_basis
import refresh
import storage
import update_policy
import trade_clock

TZ = update_policy.TZ


def unique(values):
    return list(dict.fromkeys(values))


def board_plan(cfg, symbols):
    out = []
    for symbol in symbols:
        for kind, names in cfg.get('sector_mapping', {}).get(symbol, {}).items():
            if kind not in ('industry', 'concept'):
                continue
            for name in ([names] if isinstance(names, str) else names or []):
                if (kind, name) not in out:
                    out.append((kind, name))
    return out


def acquire(db, cfg, symbols, *, include_news=False, include_hotspots=False,
            lhb_date=None, force=False):
    """Serial SQLite writes; each distinct dependency is attempted once per run."""
    symbols = unique(symbols)
    if lhb_date:
        lhb_collector.date_arg(lhb_date)  # Reject before expensive requests.
    refresh.setup(db)
    results = []

    def attempt(key, fn):
        try:
            value = fn()
            results.extend(value if isinstance(value, list) else [value])
        except lhb_collector.CollectionUnavailable as exc:
            db.rollback()
            results.append(exc.result)
        except Exception as exc:
            db.rollback()
            results.append({'key': key, 'status': 'failed', 'last_error': f'{type(exc).__name__}: {exc}'})

    for symbol in symbols:
        for tf in ('15', '60', 'daily'):
            attempt(f'bars:stock:-:{symbol}:{tf}',
                    lambda symbol=symbol, tf=tf: refresh.collect_bars(db,cfg,symbol,'stock',tf,force=force))
    for index in unique(cfg.get('market_indices', [])):
        attempt(f'bars:index:-:{index}:15',
                lambda index=index: refresh.collect_bars(db,cfg,index,'index','15',force=force))
    for kind, name in board_plan(cfg, symbols):
        attempt(f'bars:board:{kind}:{name}:15',
                lambda kind=kind,name=name: refresh.collect_bars(db,cfg,name,'board','15',kind,force=force))
        attempt(f'breadth:{kind}:{name}',
                lambda kind=kind,name=name: refresh.collect_board_members(db,cfg,name,kind,force=force))
    if include_news:
        attempt('news', lambda: refresh.collect_news(db,cfg,force=force))
    if lhb_date:
        def collect_lhb():
            r = lhb_collector.collect(db,lhb_date,symbols,force=force,config=cfg)
            return {'key': 'lhb:list:'+lhb_date, 'status': 'failed' if r.get('seat_errors') else r['status'],
                    'count': r.get('watchlist_records'), 'last_error': '; '.join(r.get('seat_errors', [])) or None}
        attempt('lhb:list:'+lhb_date,collect_lhb)
    if include_hotspots:
        for kind in ('industry', 'concept'):
            attempt('ranking:'+kind, lambda kind=kind: refresh.acquire(
                db,'ranking:'+kind,dt.datetime.now(TZ),update_policy.seconds(cfg,'ranking',300),
                lambda: refresh.board_ranking(db,kind),force=force,policy='session_snapshot',
                retry_seconds=update_policy.seconds(cfg,'retry_seconds',300,'update_policy')))
    return results


@contextmanager
def read_view(db):
    # Pin SQLite's view as well as timestamps. Do not terminate a caller's transaction.
    owns = not db.in_transaction
    if owns:
        db.execute('BEGIN')
    try:
        yield
    finally:
        if owns:
            db.rollback()


def issue(code, scope, severity='warning', **detail):
    return {'code': code, 'scope': scope, 'severity': severity, **detail}


def series(db, symbol, timeframe, as_of, limit):
    rows = storage.bars_at(db,symbol,timeframe,as_of.isoformat(),limit)
    return rows, analysis.features(rows)


def compact(f, rows):
    """Only eligible features; no raw price history or repeated source payloads."""
    value = {k: f.get(k) for k in ('status','count','close','last_bar_end','returns_pct',
                                 'moving_averages','range','same_time_amount_ratio','same_time_baseline_samples','window_issues','return_intervals')}
    if rows:
        value.update(close=rows[-1]['close'],last_bar_end=rows[-1]['bar_end'],
                     captured_at=rows[-1]['captured_at'],source=rows[-1]['source'])
    value['pivots'] = f.get('local_pivots', [])[-2:]
    if 'price_basis' in f:
        value['price_basis'] = f['price_basis']
    ma = (f.get('moving_averages') or {}).get('5') or {}
    r4 = (f.get('returns_pct') or {}).get('4')
    if any(x is None for x in (ma.get('distance_pct'),ma.get('slope_pct'),r4)):
        label = 'insufficient'
    elif all(x > 0 for x in (ma['distance_pct'],ma['slope_pct'],r4)):
        label = 'strong_observation'
    elif all(x < 0 for x in (ma['distance_pct'],ma['slope_pct'],r4)):
        label = 'weak_observation'
    else:
        label = 'mixed'
    value['descriptive_label'] = label  # Not the final direction or an executable signal.
    return value


def series_issues(db, key, scope, f, as_of, timeframe, rows=()):
    out = []
    if len({r['bar_end'] for r in rows}) != len(rows):
        out.append(issue('duplicate_bar_end',scope,'error'))
    expected = update_policy.completed_boundary(as_of,timeframe).isoformat()
    if f.get('status') != 'ok':
        out.append(issue('insufficient_bars',scope,'error',count=f.get('count',0)))
    end = f.get('last_bar_end')
    if end and end < expected:
        out.append(issue('bar_behind_boundary',scope,'error',expected=expected,actual=end))
    state = update_policy.state_at(db,key,as_of)
    if state.get('last_error'):
        out.append(issue('collection_error',scope,'error',error=state['last_error']))
    if f.get('window_issues'):
        out.append(issue('bar_window_gap',scope,metrics=list(f['window_issues'])))
    basis = f.get('price_basis', {})
    if basis.get('excluded_metrics'):
        out.append(issue('price_basis_'+basis['status'],scope))
    return out


def apply_availability(row, problems):
    row['reference_only'] = any(p['severity']=='error' for p in problems)
    if row['reference_only']:
        row['descriptive_label'] = 'unavailable'
    if any(p['code']=='duplicate_bar_end' for p in problems):
        row['close'] = None  # Never pick an arbitrary price from conflicting sources.
    return row


def day_return(db, symbol, as_of, rows, *, equity):
    if not rows:
        return {'value_pct': None, 'reason': 'missing_bars'}
    date = rows[-1]['bar_end'][:10]
    previous = storage.bars_at(db,symbol,'15',as_of.isoformat(),1,
                                    bar_end_at=date+'T00:00:00+08:00')
    if not previous:
        return {'value_pct': None, 'reason': 'missing_previous_close'}
    # Do not use the last pre-suspension intraday tick as an alleged prior close.
    if previous[-1]['bar_end'][11:19] != '15:00:00':
        return {'value_pct': None, 'reason': 'previous_close_not_completed_session'}
    expected_previous=update_policy.daily_boundary(dt.datetime.fromisoformat(date+'T00:00:00+08:00')).isoformat()
    if previous[-1]['bar_end'] != expected_previous:
        return {'value_pct':None,'reason':'previous_session_missing_or_calendar_unverified'}
    if equity:
        check = price_basis.check(db,symbol,previous[-1]['bar_end'],rows[-1]['bar_end'],as_of)
        if check['status'] not in ('same_session','verified'):
            return {'value_pct': None, 'reason': 'price_basis_'+check['status']}
    return {'value_pct': (rows[-1]['close']/previous[-1]['close']-1)*100,
            'baseline': previous[-1]['close'], 'baseline_at': previous[-1]['bar_end']}


def snapshot(db, cfg, symbols, as_of, *, acquisitions=(), include_hotspots=False):
    if as_of.tzinfo is None:
        raise ValueError('as_of must include timezone')
    as_of = as_of.astimezone(TZ)
    symbols = unique(symbols)
    with read_view(db):
        return _snapshot(db,cfg,symbols,as_of,acquisitions,include_hotspots)


def _snapshot(db, cfg, symbols, as_of, acquisitions, include_hotspots):
    issues = []
    for r in acquisitions:
        if r.get('status') not in ('updated','cached'):
            issues.append(issue('acquisition_'+r.get('status','unknown'),r.get('key','unknown'),'error',
                                error=r.get('last_error') or r.get('reason')))
    contexts = {s:analysis.build(db,s,cfg,as_of) for s in symbols}
    market = []
    for name in unique(cfg.get('market_indices', [])):
        rows, f = series(db,name,'15',as_of,160)
        row = {'symbol':name,**compact(f,rows),'day_return':day_return(db,name,as_of,rows,equity=False)}
        problems=series_issues(db,f'bars:index:-:{name}:15',name,f,as_of,'15',rows)
        apply_availability(row,problems)
        if row['reference_only']: row['day_return']={'value_pct':None,'reason':'series_unavailable_at_cutoff'}
        market.append(row)
        issues.extend(problems)
    boards = []
    for kind,name in board_plan(cfg,symbols):
        board = next(b for c in contexts.values() for b in c['sectors'] if b['type']==kind and b['name']==name)
        key=f'board:{kind}:{name}'
        rows,f=series(db,key,'15',as_of,160)
        boards.append({'symbol':key,'type':kind,'name':name,**compact(f,rows),
                       'day_return':day_return(db,key,as_of,rows,equity=False),
                       'breadth':{k:board.get(k) for k in ('breadth','breadth_as_of','valid_members',
                                    'breadth_age_seconds','breadth_status','breadth_last_error')},
                       'quote_time_verified':False})
        problems=series_issues(db,f'bars:board:{kind}:{name}:15',key,f,as_of,'15',rows)
        apply_availability(boards[-1],problems)
        if boards[-1]['reference_only']: boards[-1]['day_return']={'value_pct':None,'reason':'series_unavailable_at_cutoff'}
        issues.extend(problems)
        issues.append(issue('breadth_quote_time_unverified',key))
        if not board['breadth_usable_for_current_session']:
            issues.append(issue('breadth_unavailable_or_old',key,error=board.get('breadth_last_error')))
    stocks=[]
    all_news={}
    for symbol,c in contexts.items():
        periods={};stock_rows={}
        for tf,f in c['stock'].items():
            rows=storage.bars_at(db,symbol,tf,as_of.isoformat(),cfg['history'].get(tf,160))
            stock_rows[tf]=rows
            periods[tf]=compact(f,rows)
            state=update_policy.state_at(db,f'bars:stock:-:{symbol}:{tf}',as_of)
            periods[tf]['collection_state']={k:state.get(k) for k in ('coverage','last_attempt','last_error')}
            problems=series_issues(db,f'bars:stock:-:{symbol}:{tf}',symbol+':'+tf,f,as_of,tf,rows)
            apply_availability(periods[tf],problems)
            issues.extend(problems)
        latest=periods['15'].get('last_bar_end')
        if not c['sectors']:
            issues.append(issue('sector_mapping_missing',symbol))
        if not cfg.get('stock_names',{}).get(symbol):
            issues.append(issue('stock_name_missing_for_news',symbol))
        relatives={}
        for index,f in c['market'].items():
            if f.get('last_bar_end')!=latest:
                issues.append(issue('stock_index_time_mismatch',symbol,'error',index=index))
            index_ok=next(m for m in market if m['symbol']==index)['reference_only'] is False
            relatives[index]=f.get('stock_relative_return_pp') if index_ok and not periods['15']['reference_only'] else None
        for board in c['sectors']:
            if board['features'].get('last_bar_end')!=latest:
                issues.append(issue('stock_board_time_mismatch',symbol,'error',board=board['name']))
            board_ok=next(b for b in boards if b['type']==board['type'] and b['name']==board['name'])['reference_only'] is False
            relatives[f"board:{board['type']}:{board['name']}"]=board['relative_return_pp'] if board_ok and not periods['15']['reference_only'] else None
        targets=[e for e in c['events'] if e.get('target_match')]
        macro=[e for e in c['events'] if not e.get('target_match')]
        selected_events=targets[:3]+macro[:2]
        event_ids=[]
        for event in selected_events:
            event_ids.append(event['event_id'])
            if event['event_id'] not in all_news:
                all_news[event['event_id']]={k:event.get(k) for k in ('event_id','title','status','latest_report_at','latest_seen_at')}
                sources=event.get('sources',[])
                all_news[event['event_id']]['source_count']=len(sources)
                all_news[event['event_id']]['sources']=unique(s.get('source') for s in sources if s.get('source'))[:3]
                all_news[event['event_id']]['first_seen_at']=min((s['first_seen_at'] for s in sources),default=None)
        lhb=c['longhubang']
        stocks.append({'symbol':symbol,'name':cfg.get('stock_names',{}).get(symbol),'as_of':as_of.isoformat(),
                       'periods':periods,'day_return':day_return(db,symbol,as_of,stock_rows['15'],equity=True)
                          if not periods['15']['reference_only'] else {'value_pct':None,'reason':'series_unavailable_at_cutoff'},
                       'relative_last_bar_pp':relatives,
                       'hourly_covers_latest_15m':periods['60'].get('last_bar_end')==latest if latest else False,
                       'event_ids':event_ids,
                       'event_coverage':{'eligible':len(c['events']),'target_matches':len(targets),
                                         'shown':len(event_ids),'truncated':len(event_ids)<len(c['events'])},
                       'longhubang':{k:lhb.get(k) for k in ('role','status','latest_checked_trade_date','latest_trade_date',
                                                            'age_calendar_days','coverage','refresh_issues')},
                       'previous_decision':c['previous_decision']})
        if lhb.get('refresh_issues'):
            issues.append(issue('lhb_collection_issue',symbol,detail=lhb['refresh_issues']))
    rankings={}
    if include_hotspots:
        for kind in ('industry','concept'):
            r=refresh.rank_output(db,kind,5,as_of=as_of)
            rankings[kind]=r
            if r['status']!='available':
                issues.append(issue('ranking_'+r['status'],kind,'error',error=r.get('last_error')))
            else:
                r['evidence_role']=trade_clock.snapshot_role(r['captured_at'],as_of,update_policy.seconds(cfg,'ranking',300))
                if r['evidence_role']=='historical':
                    issues.append(issue('ranking_age_requires_session_check',kind))
    issues.append(issue('calendar_not_verified','market'))
    status='incomplete' if any(x['severity']=='error' for x in issues) else 'limited' if issues else 'ready'
    ends={s['periods']['15'].get('last_bar_end') for s in stocks}
    ends.update(m.get('last_bar_end') for m in market)
    ends.update(b.get('last_bar_end') for b in boards)
    aligned_end=next(iter(ends)) if len(ends)==1 and None not in ends else None
    session,_=market_calendar.session_state(as_of,'CN')
    return {'meta':{'as_of':as_of.isoformat(),'information_cutoff':as_of.isoformat(),'session':session,
                    'schema_version':'research-snapshot-v1','aligned_15_bar_end':aligned_end,'calendar_verified':False,
                    'refresh_history_notice':'new attempts archived; overwritten pre-upgrade errors cannot be reconstructed',
                    'notice':'single SQLite read view; per-series bar ends may differ; not a final direction'},
            'status':status,'acquisition':{'count':len(acquisitions),
                'updated':sum(x.get('status')=='updated' for x in acquisitions),
                'cached':sum(x.get('status')=='cached' for x in acquisitions),
                'failed':sum(x.get('status')=='failed' for x in acquisitions),
                'deferred':sum(x.get('status')=='deferred' for x in acquisitions),
                'pending':sum(x.get('status')=='pending' for x in acquisitions),
                'results':[{k:r[k] for k in ('key','status','last_error','latest_bar_end',
                                             'captured_at','last_success','count') if k in r}
                           for r in acquisitions]},
            'issues':issues,'market':market,'boards':boards,'stocks':stocks,
            'events':list(all_news.values()),'rankings':rankings,
            'overseas':{'status':'not_collected','notice':'no verified overseas feed in this pipeline'},
            'synthesis':{'status':'requires_model_judgment','action':'WAIT',
                         'notice':'descriptive labels are not a directional vote or trade signal'}}
