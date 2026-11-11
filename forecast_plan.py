"""Five independent horizons in four families. Evidence preparation never calls a model."""
import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import subprocess

import analysis
from forecast_basis import BasisBook
import price_basis
import refresh
import research_batch
import storage
import update_policy
from trade_clock import Calendar, aware, source_url

DIRECTIONS = ('BULLISH','BEARISH','RANGE','UNCERTAIN')
HORIZONS = ('short_15m','long_150m','today_close','next_1d','next_2d')
DEFAULT_POLICY = {'version':'research-default-v1','status':'uncalibrated',
                  'neutral_band_pct':0.15,'range_amplitude_pct':0.8,
                  'range_occupancy_min':0.8,'max_baseline_age_seconds':90,
                  'max_prepare_age_seconds':120,'max_quote_age_long_seconds':300}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def policy_checked(value=None):
    out = dict(DEFAULT_POLICY, **(value or {}))
    for key in ('neutral_band_pct','range_amplitude_pct','range_occupancy_min',
                'max_baseline_age_seconds','max_prepare_age_seconds','max_quote_age_long_seconds'):
        v=out[key]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:
            raise ValueError('invalid evaluation policy '+key)
    if out['range_occupancy_min']>1 or out['range_amplitude_pct']<out['neutral_band_pct']:
        raise ValueError('invalid range evaluation policy')
    by=out.get('by_horizon',{})
    if not isinstance(by,dict) or any(k not in HORIZONS for k in by):
        raise ValueError('unknown per-horizon scoring policy')
    for key,overrides in by.items():
        if not isinstance(overrides,dict) or any(k not in ('neutral_band_pct','range_amplitude_pct','range_occupancy_min') for k in overrides):
            raise ValueError('invalid per-horizon policy override')
        policy_checked(dict(out,by_horizon={},**overrides))
    return out


def scoring_policy(policy,horizon):
    return dict(policy,**policy.get('by_horizon',{}).get(horizon,{}))


def versions(root, cfg, strategy, policy, model_id='not_reported'):
    root=Path(root)
    commit=subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True)
    hashes={}
    for pattern in ('*.py','docs/claude/*.md'):
        for p in sorted(root.glob(pattern)):
            hashes[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
    return {'code_commit':commit.stdout.strip() if commit.returncode==0 else None,
            'code_tree_hash':digest(hashes),'source_hashes':hashes,'strategy_version':strategy,
            'config_hash':digest(cfg),'policy_hash':digest(policy),
            'model_id':model_id,'model_identity':'caller_declared' if model_id!='not_reported' else 'unknown',
            'prompt_version':'multi-horizon-v1','schema_version':'forecast-v1'}


def targets(issued_at, calendar):
    at=aware(issued_at); trading=calendar.state(at)=='trading'
    end=at.replace(hour=15,minute=0,second=0,microsecond=0)
    result={}
    for key,minutes,checks in [('short_15m',15,(10,20)),('long_150m',150,(120,180)),('today_close',None,())]:
        today_available=calendar.is_open(at.date()) and at<end
        t=end if minutes is None and today_available else calendar.add_intraday(at,minutes) if minutes else None
        reason=None if t else 'off_session' if not trading else 'insufficient_remaining_session'
        result[key]={'target_at':t.isoformat() if t else None,
                     'applicability':'applicable' if t else 'not_applicable','reason':reason,
                     'checkpoints':{str(n):(calendar.add_intraday(at,n).isoformat() if calendar.add_intraday(at,n) else None) for n in checks}}
    for offset in (1,2):
        try:
            day=calendar.next_day(at.date(),offset)
            t=dt.datetime.combine(day,dt.time(15),at.tzinfo)
            reason=None
        except ValueError:
            t=None;reason='target_calendar_unverified'
        result[f'next_{offset}d']={'target_at':t.isoformat() if t else None,
            'applicability':'applicable' if t else 'unknown','reason':reason,'checkpoints':{}}
    return result


def acquire(db,cfg,symbols,news=False,hotspots=False):
    """Single serial plan; explicit forecast preparation, no background loop."""
    updates=research_batch.acquire(db,cfg,symbols,include_news=news,include_hotspots=hotspots)
    dependencies=[(s,'stock',None) for s in dict.fromkeys(symbols)]
    dependencies += [(s,'index',None) for s in dict.fromkeys(cfg.get('market_indices',[]))]
    dependencies += [(name,'board',kind) for kind,name in research_batch.board_plan(cfg,symbols)]
    for name,kind,board_type in dependencies:
        for tf in ('1','5'):
            try:
                updates.append(refresh.collect_bars(db,cfg,name,kind,tf,board_type))
            except Exception as exc:
                db.rollback()
                updates.append({'key':f'bars:{kind}:{board_type or "-"}:{name}:{tf}',
                                'status':'failed','last_error':f'{type(exc).__name__}: {exc}'})
    return updates


def qualified_features(db,symbol,rows,as_of,calendar,basis,equity=True):
    if not rows:
        return {'status':'insufficient','count':0}
    adjusted=basis.adjusted(symbol,rows) if equity else copy.deepcopy(rows)
    if adjusted is None:
        # Preserve only a same-session suffix; never refill masked cross-day indicators.
        adjusted=[copy.deepcopy(r) for r in rows if r['bar_end'][:10]==rows[-1]['bar_end'][:10]]
    features=analysis.features(adjusted,calendar)
    if equity and basis.adjusted(symbol,rows) is None:
        features=price_basis.apply(db,symbol,adjusted,features,as_of)
    features['price_scale']='attested_adjusted' if equity and basis.adjusted(symbol,rows) is not None and len({r['bar_end'][:10] for r in rows})>1 else 'same_session' if equity else 'index_raw'
    return features


def prepare(db,cfg,symbols,at,*,strategy='multi-horizon-v1',calendar_manifest=None,
            basis_manifests=(),policy=None,root=None,model_id='not_reported',updates=(),verified_events=()):
    at=aware(at);symbols=list(dict.fromkeys(symbols));policy=policy_checked(policy)
    if not isinstance(strategy,str) or not strategy.strip():raise ValueError('strategy version required')
    if not symbols or any(len(s)!=6 or not s.isascii() or not s.isdigit() for s in symbols):
        raise ValueError('at least one six-digit stock symbol required')
    cal=Calendar.load(calendar_manifest,at);basis=BasisBook(basis_manifests,at)
    verified_events=copy.deepcopy(list(verified_events))
    seen_events=set()
    for event in verified_events:
        if event.get('verification')!='operator_attested' or not isinstance(event.get('fact'),str) or not event['fact'].strip():
            raise ValueError('verified event needs an attested factual statement')
        if aware(event['verified_at'])>at or aware(event['available_at'])>at or aware(event['published_at'])>at:
            raise ValueError('event was not available at the information cutoff')
        source_url(event['source_url'])
        if not event.get('event_id') or event['event_id'] in seen_events:
            raise ValueError('event identity missing or duplicated')
        seen_events.add(event['event_id'])
        if not isinstance(event.get('symbols'),list) or not event['symbols']:
            raise ValueError('verified event must declare explicit target symbols')
    if policy.get('fitted_at') and aware(policy['fitted_at'])>at:
        raise ValueError('policy was fitted after the prediction cutoff')
    if policy.get('training_through') and aware(policy['training_through'])>=at:
        raise ValueError('calibration must precede the prediction cutoff')
    result={'meta':{'prepared_at':at.isoformat(),'information_cutoff':at.isoformat(),
                    'session':cal.state(at),'calendar_verified':cal.verified(at.date()),
                    'forecast_type':'prospective','action':'WAIT'},
            'versions':versions(root or Path(__file__).parent,cfg,strategy,policy,model_id),
            'calendar':calendar_manifest,'basis':basis.payload(),'policy':policy,
            'notice':'Uncalibrated qualitative research, not trading instructions or validated probabilities.',
            'stocks':[],'evidence':{},'acquisition':list(updates),'verified_events':verified_events}
    with research_batch.read_view(db):
        shared=research_batch.snapshot(db,cfg,symbols,at,acquisitions=updates)
        result['shared']=shared
        for symbol in symbols:
            periods={};raw={};ids=[]
            for tf in ('1','5','15','60','daily'):
                rows=storage.bars_at(db,symbol,tf,at.isoformat(),400)
                raw[tf]=rows
                f=qualified_features(db,symbol,rows,at,cal,basis)
                key=f'S:{symbol}:{tf}';ids.append(key)
                result['evidence'][key]={'features':f,'bar_end':rows[-1]['bar_end'] if rows else None,
                    'captured_at':rows[-1]['captured_at'] if rows else None,
                    'source':rows[-1]['source'] if rows else None,
                    'range_formed_at':rows[-2]['bar_end'] if len(rows)>1 else None,
                    'last_bar':{k:rows[-1][k] for k in ('high','low','bar_end')} if rows else None}
                periods[tf]=f
            for market in shared['market']:
                key='M:'+market['symbol'];result['evidence'][key]=market;ids.append(key)
            selected_boards=set(research_batch.board_plan(cfg,[symbol]))
            for board in shared['boards']:
                if (board['type'],board['name']) not in selected_boards:continue
                key='B:'+board['symbol'];result['evidence'][key]=board;ids.append(key)
            for e in shared['events']:
                if e['event_id'] in next(s for s in shared['stocks'] if s['symbol']==symbol)['event_ids']:
                    result['evidence'][e['event_id']]=e;ids.append(e['event_id'])
            candidates=[rows[-1] for tf,rows in raw.items() if rows and tf in ('1','15','daily')]
            baseline=max(candidates,key=lambda r:(r['bar_end'],r['timeframe']=='1')) if candidates else None
            for e in verified_events:
                if symbol in e['symbols']:
                    key='V:'+e['event_id'];result['evidence'][key]=e;ids.append(key)

            plan=targets(at,cal)
            failure_keys={r['key'] for r in updates if r.get('status') not in ('updated','cached')}
            for key,h in plan.items():
                reasons=[]
                if h['applicability']=='not_applicable':
                    h.update(eligibility='not_applicable',blockers=[h['reason']]);continue
                if not cal.verified(at.date()): reasons.append('calendar_unverified')
                if h['applicability']=='unknown':reasons.append(h['reason'])
                if not baseline: reasons.append('baseline_missing')
                intraday=key in ('short_15m','long_150m') or key=='today_close' and cal.state(at) in ('trading','lunch_break')
                if intraday:
                    maxage=policy['max_baseline_age_seconds'] if key=='short_15m' else policy['max_quote_age_long_seconds']
                    if not raw['1'] or (cal.last_completed(at,'1')-aware(raw['1'][-1]['bar_end'])).total_seconds()>maxage:
                        reasons.append('one_minute_baseline_stale_or_missing')
                    required=('1','5') if key=='short_15m' else ('5','15')
                else:
                    required=('60','daily')
                    if not raw['daily'] or (at.date()-aware(raw['daily'][-1]['bar_end']).date()).days>7:
                        reasons.append('daily_baseline_missing_or_old')
                for tf in required:
                    f=periods[tf]
                    if not f.get('last_bar_end') or aware(f['last_bar_end'])<cal.last_completed(at,tf):
                        reasons.append('stale_'+tf)
                    state=update_policy.state_at(db,f'bars:stock:-:{symbol}:{tf}',at)
                    if state.get('last_error'):
                        reasons.append('recorded_collection_error_'+tf)
                    if f.get('status')!='ok' or f.get('return_last_bar_pct') is None:
                        reasons.append('insufficient_qualified_'+tf)
                    if f'bars:stock:-:{symbol}:{tf}' in failure_keys:
                        reasons.append('collection_failed_'+tf)
                if intraday and ('bars:stock:-:'+symbol+':1' in failure_keys or update_policy.state_at(db,f'bars:stock:-:{symbol}:1',at).get('last_error')):
                    reasons.append('one_minute_collection_failed')
                if baseline and len(raw[baseline['timeframe']])>1 and raw[baseline['timeframe']][-2]['bar_end']==baseline['bar_end']:
                    reasons.append('baseline_source_conflict')
                h.update(eligibility='eligible' if not reasons else 'insufficient',blockers=reasons)
            market_signs=[(m.get('returns_pct') or {}).get('4') for m in shared['market'] if not m.get('reference_only')]
            market_signs=[v for v in market_signs if v is not None]
            regime='up' if market_signs and all(v>0 for v in market_signs) else 'down' if market_signs and all(v<0 for v in market_signs) else 'mixed' if market_signs else 'unknown'
            result['stocks'].append({'symbol':symbol,'name':cfg.get('stock_names',{}).get(symbol),
                'baseline':{k:baseline[k] for k in ('close','bar_end','captured_at','source','timeframe')} if baseline else None,
                'periods':periods,'horizons':plan,'allowed_evidence_ids':ids,
                'sector_mapping':cfg.get('sector_mapping',{}).get(symbol,{}),'market_regime':regime,
                'limitations':['sector_mapping_missing'] if not cfg.get('sector_mapping',{}).get(symbol) else []})
    eligible=sum(h['eligibility']=='eligible' for stock in result['stocks'] for h in stock['horizons'].values())
    result['meta']['eligible_slots']=eligible
    result['meta']['total_slots']=len(symbols)*len(HORIZONS)
    result['meta']['readiness']='blocked' if not eligible else 'ready' if eligible==result['meta']['total_slots'] else 'partial'
    result['input_hash']=digest(result)
    return result


def verify_input(value):
    unhashed={k:v for k,v in value.items() if k!='input_hash'}
    if value.get('input_hash')!=digest(unhashed):
        raise ValueError('prepared input hash mismatch')
    return value


def decision_template(prepared):
    return {'input_hash':prepared['input_hash'],'action':'WAIT','stocks':[
        {'symbol':s['symbol'],'horizons':{k:{'direction':'UNCERTAIN','reasoning':'待人工综合证据',
        'evidence_ids':[],'confirmation':None,'invalidation':None,'range':None}
        for k in HORIZONS}} for s in prepared['stocks']]}


def validate_decision(prepared,decision):
    verify_input(prepared)
    if decision.get('input_hash')!=prepared['input_hash'] or decision.get('action')!='WAIT':
        raise ValueError('decision identity mismatch or executable action')
    usage=decision.get('resource_usage')
    if usage is not None:
        if not isinstance(usage,dict) or any(k not in ('cost_usd','input_tokens','output_tokens','latency_seconds') for k in usage):
            raise ValueError('invalid resource usage fields')
        for key,value in usage.items():
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<0:
                raise ValueError('invalid resource usage value')
            if key.endswith('_tokens') and int(value)!=value:raise ValueError('token counts must be integers')
    expected={s['symbol']:s for s in prepared['stocks']}
    stocks=decision.get('stocks',[])
    if len(stocks)!=len(expected) or {s['symbol'] for s in stocks}!=set(expected):
        raise ValueError('all selected stocks must appear exactly once')
    for s in stocks:
        prepared_stock=expected[s['symbol']]
        if set(s.get('horizons',{}))!=set(HORIZONS):
            raise ValueError('all horizons required')
        for key,d in s['horizons'].items():
            if d.get('direction') not in DIRECTIONS or not isinstance(d.get('reasoning'),str) or not d['reasoning'].strip():
                raise ValueError('direction and nonempty reasoning required')
            h=prepared_stock['horizons'][key]
            if h['eligibility']!='eligible' and d['direction']!='UNCERTAIN':
                raise ValueError('ineligible horizon cannot have a directional forecast')
            ids=d.get('evidence_ids')
            if not isinstance(ids,list) or any(not isinstance(i,str) or i not in prepared_stock['allowed_evidence_ids'] for i in ids):
                raise ValueError('unknown or out-of-scope evidence ID')
            if d['direction']!='UNCERTAIN' and not ids:
                raise ValueError('directional forecast requires cited evidence')
            if d['direction']!='UNCERTAIN':
                primary=('1','5') if key=='short_15m' else ('60','daily') if key.startswith('next_') else ('5','15','60')
                if not any(f'S:{s["symbol"]}:{tf}' in ids for tf in primary):
                    raise ValueError('direction needs qualified price evidence for its own horizon')
            r=d.get('range')
            if d['direction']=='RANGE':
                if not isinstance(r,dict) or not all(isinstance(r.get(k),(int,float)) and not isinstance(r[k],bool) and math.isfinite(r[k]) for k in ('low','high')) or not 0<r['low']<r['high']:
                    raise ValueError('RANGE needs an explicit observed interval')
                if r.get('evidence_id') not in ids:
                    raise ValueError('range needs cited structural evidence')
                cited=prepared['evidence'][r['evidence_id']]
                observed=(cited.get('features',cited).get('range') or {})
                if not all(observed.get(k) is not None and math.isclose(r[k],observed[k],rel_tol=1e-10) for k in ('low','high')):
                    raise ValueError('range is not the observed interval in cited evidence')
            for name in ('confirmation','invalidation'):
                c=d.get(name)
                if c is None: continue  # Unknown is better than invented precision.
                if not isinstance(c,dict) or c.get('kind') not in ('pivot','range_boundary','breakout_boundary','single_bar_reference'):
                    raise ValueError('typed structural condition required')
                if c.get('evidence_id') not in ids or c.get('timeframe') not in ('1','5','15','60','daily'):
                    raise ValueError('condition provenance or timeframe missing')
                if c.get('relation') not in ('above','below') or not isinstance(c.get('price'),(float,int)) or isinstance(c['price'],bool) or not math.isfinite(c['price']) or c['price']<=0:
                    raise ValueError('invalid condition price/relation')
                if aware(c['formed_at'])>aware(prepared['meta']['information_cutoff']):
                    raise ValueError('future structural condition')
                if c.get('holds',2 if name=='confirmation' else 1) not in ((2,) if name=='confirmation' else (1,2)):
                    raise ValueError('confirmation requires two closed bars; invalidation one or two')
                evidence=prepared['evidence'][c['evidence_id']]
                f=evidence.get('features',evidence)
                allowed=[]
                if c['kind']=='pivot':
                    allowed=[(x['price'],x['at']) for x in f.get('local_pivots',f.get('pivots',[]))]
                elif c['kind'] in ('range_boundary','breakout_boundary'):
                    allowed=[(v,evidence.get('range_formed_at') or evidence.get('last_bar_end')) for k,v in (f.get('range') or {}).items() if k in ('low','high') and v is not None]
                elif evidence.get('last_bar'):
                    allowed=[(evidence['last_bar'][k],evidence['last_bar']['bar_end']) for k in ('low','high')]
                if not any(math.isclose(c['price'],v,rel_tol=1e-10) and t and aware(t)==aware(c['formed_at']) for v,t in allowed):
                    raise ValueError('condition price/time not found in cited qualified structure')
                if name=='invalidation' and c['kind']=='single_bar_reference':
                    raise ValueError('single-bar reference is not structural invalidation')
            a,b=d.get('confirmation'),d.get('invalidation')
            if a and a['kind']=='breakout_boundary' and (not b or b['kind']!='breakout_boundary' or a['price']!=b['price'] or a['relation']==b['relation'] or a['timeframe']!=b['timeframe']):
                raise ValueError('breakout confirmation and invalidation require the same boundary')
    return decision
