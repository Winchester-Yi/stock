"""Append-only prediction/evaluation ledger. No models, trades, or scheduling."""
import datetime as dt
import json
import statistics
import hashlib
from pathlib import Path

from forecast_basis import BasisBook
from forecast_plan import canonical, digest, targets, validate_decision, scoring_policy
import storage
from trade_clock import Calendar, aware

SCHEMA = '''
CREATE TABLE IF NOT EXISTS forecast_inputs (
 input_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_predictions (
 prediction_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, symbol TEXT NOT NULL,
 horizon TEXT NOT NULL, issued_at TEXT NOT NULL, target_at TEXT,
 mode TEXT NOT NULL, strategy_version TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS forecast_lookup ON forecast_predictions(symbol,issued_at,horizon);
CREATE TABLE IF NOT EXISTS forecast_evaluations (
 evaluation_id TEXT PRIMARY KEY, prediction_id TEXT NOT NULL, evaluated_at TEXT NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS forecast_eval_lookup ON forecast_evaluations(prediction_id,evaluated_at);
'''


def setup(db):
    db.executescript(SCHEMA)


def latest_baseline(db,symbol,at):
    candidates=[]
    for tf in ('1','15','daily'):
        rows=storage.bars_at(db,symbol,tf,at.isoformat(),2)
        if rows:
            if len(rows)>1 and rows[-1]['bar_end']==rows[-2]['bar_end']:
                raise ValueError('conflicting baseline sources')
            candidates.append(rows[-1])
    if not candidates:return None
    r=max(candidates,key=lambda r:(r['bar_end'],r['timeframe']=='1'))
    return {k:r[k] for k in ('close','bar_end','captured_at','source','timeframe')}


def record(db,prepared,decision,*,issued_at=None,mode='live',now=None):
    """Live calls use the actual clock; explicit replay stays separately labelled."""
    now=aware(now or dt.datetime.now().astimezone())
    at=now if mode=='live' else aware(issued_at) if mode=='replay' and issued_at else None
    if mode not in ('live','replay') or at is None or at>now:
        raise ValueError('invalid prediction mode/time')
    if mode=='live' and issued_at is not None:
        raise ValueError('live predictions cannot backdate issued_at; use explicit replay')
    validate_decision(prepared,decision)
    if mode=='live' and prepared['meta'].get('forecast_type')!='prospective':
        raise ValueError('historical inputs cannot be recorded as live predictions')
    cutoff=aware(prepared['meta']['information_cutoff'])
    age=(at-cutoff).total_seconds()
    if age<0 or age>prepared['policy']['max_prepare_age_seconds']:
        raise ValueError('prepared evidence too old or from the future; prepare again')
    cal=Calendar.load(prepared['calendar'],at)
    plan=targets(at,cal);ready={s['symbol']:s for s in prepared['stocks']}
    pending=[]
    for s in decision['stocks']:
        base=latest_baseline(db,s['symbol'],at)
        if base is None:
            base=ready[s['symbol']]['baseline']
        if base and (aware(base['bar_end'])>at or aware(base['captured_at'])>at):
            raise ValueError('baseline unavailable at issuance')
        for horizon,d in s['horizons'].items():
            h=plan[horizon];original=ready[s['symbol']]['horizons'][horizon]
            if d['direction']!='UNCERTAIN':
                if not base or h['applicability']!='applicable' or original['eligibility']!='eligible':
                    raise ValueError('horizon became inapplicable before issuance')
                if horizon in ('short_15m','long_150m') or horizon=='today_close' and cal.state(at) in ('trading','lunch_break'):
                    maxage=prepared['policy']['max_baseline_age_seconds'] if horizon=='short_15m' else prepared['policy']['max_quote_age_long_seconds']
                    if base['timeframe']!='1' or (cal.last_completed(at,'1')-aware(base['bar_end'])).total_seconds()>maxage:
                        raise ValueError('baseline too stale at actual issuance')
            p={'input_hash':prepared['input_hash'],'symbol':s['symbol'],'horizon':horizon,
               'issued_at':at.isoformat(),'target_at':h['target_at'],'checkpoints':h['checkpoints'],
               'applicability':h['applicability'],'eligibility':original['eligibility'],
               'blockers':original['blockers'],'mode':mode,'baseline':base,
               'decision_source':'manual_assistant','recorded_at':now.isoformat(),
               'market_regime':ready[s['symbol']]['market_regime'],
               'decision':d,'policy':prepared['policy'],'versions':prepared['versions'],
               'resource_usage':decision.get('resource_usage'),'resource_allocation_slots':len(s['horizons'])}
            p['prediction_id']=digest({k:v for k,v in p.items() if k!='recorded_at'});pending.append(p)
    # Validate the entire batch before any insert. Duplicate replay is idempotent.
    setup(db)
    with db:
        db.execute('INSERT OR IGNORE INTO forecast_inputs VALUES (?,?,?)',
                   (prepared['input_hash'],canonical(prepared),now.isoformat()))
        for p in pending:
            db.execute('INSERT OR IGNORE INTO forecast_predictions VALUES (?,?,?,?,?,?,?,?,?)',
                       (p['prediction_id'],p['input_hash'],p['symbol'],p['horizon'],p['issued_at'],
                        p['target_at'],mode,p['versions']['strategy_version'],canonical(p)))
    return pending


def _full_path(db,symbol,issued,target,as_of,calendar):
    # Only fully post-issuance minutes contribute extrema; exclude partial first minute.
    endpoint=target.replace(second=0,microsecond=0)
    start=issued.replace(second=0,microsecond=0)
    if issued>start:
        start+=dt.timedelta(minutes=1)
    expected=calendar.expected(start,endpoint,'1')
    if not expected:
        return None,'no_full_post_issuance_minutes'
    rows=storage.bars_at(db,symbol,'1',as_of.isoformat(),20000,bar_end_at=endpoint.isoformat())
    rows=[r for r in rows if start<aware(r['bar_end'])<=endpoint]
    if [aware(r['bar_end']) for r in rows]!=expected:
        return None,'missing_or_duplicate_post_issuance_minutes'
    if any(aware(r['captured_at'])<aware(r['bar_end']) for r in rows):
        return None,'invalid_capture_time'
    return rows,None


def _condition(rows,condition,calendar,default_holds):
    if condition is None:
        return None
    tf=condition['timeframe'];holds=condition.get('holds',default_holds)
    if not rows:
        return None
    minute_map={r['bar_end']:r for r in rows}
    first,last=aware(rows[0]['bar_end']),aware(rows[-1]['bar_end'])
    consecutive=0;previous=None
    for end in calendar.expected(first-dt.timedelta(minutes=1),last,tf):
        if tf=='daily':
            start=end.replace(hour=9,minute=30)
        elif tf=='60':
            start=end-dt.timedelta(minutes=60)
        else:
            start=end-dt.timedelta(minutes=int(tf))
        needed=calendar.expected(start,end,'1')
        # A whole candle, not a suffix of a candle starting before issuance.
        if not needed or any(t.isoformat() not in minute_map for t in needed):
            consecutive=0;previous=None;continue
        close=minute_map[end.isoformat()]['close']
        matched=close>condition['price'] if condition['relation']=='above' else close<condition['price']
        segment=(end.date(),end.hour<12)
        if segment!=previous:
            consecutive=0
        consecutive=consecutive+1 if matched else 0
        previous=segment
        if consecutive>=holds:
            return {'triggered':True,'at':end.isoformat(),'price':close,'holds':holds}
    return {'triggered':False}


def outcome(db,p,prepared,as_of,*,basis_manifests=None):
    as_of=aware(as_of)
    if p['applicability']=='not_applicable':
        return {'status':'not_applicable'}
    if not p['target_at']:
        return {'status':'ineligible','reason':'target_calendar_unverified'}
    if p['eligibility']!='eligible':
        return {'status':'ineligible','blockers':p['blockers']}
    issued,target=aware(p['issued_at']),aware(p['target_at'])
    if target>as_of:
        return {'status':'pending','reason':'target_not_reached'}
    cal=Calendar.load(prepared['calendar'],issued)
    if not cal.verified(issued.date()) or not cal.verified(target.date()):
        return {'status':'unscorable','reason':'calendar_unverified'}
    rows,error=_full_path(db,p['symbol'],issued,target,as_of,cal)
    if error:
        return {'status':'pending','reason':error}
    base=p['baseline']
    if not base:
        return {'status':'unscorable','reason':'baseline_missing'}
    book=BasisBook(prepared['basis'] if basis_manifests is None else basis_manifests,as_of)
    base_factor=book.scale(p['symbol'],base['bar_end'],rows[-1]['bar_end'])
    adjusted=book.adjusted(p['symbol'],rows)
    if base_factor is None or adjusted is None:
        return {'status':'unscorable','reason':'evaluation_price_basis_unverified'}
    base_price=base['close']*base_factor
    closes=[r['close'] for r in adjusted];high=max(r['high'] for r in adjusted);low=min(r['low'] for r in adjusted)
    ret=(closes[-1]/base_price-1)*100;policy=scoring_policy(p['policy'],p['horizon']);band=policy['neutral_band_pct']
    label='BULLISH' if ret>band else 'BEARISH' if ret < -band else 'NEUTRAL_ENDPOINT'
    direction=p['decision']['direction'];signed=1 if direction=='BULLISH' else -1 if direction=='BEARISH' else None
    r=p['decision'].get('range');occupancy=None
    if r:
        # Prediction boundaries are on the issued-date scale as well.
        rlow,rhigh=r['low']*base_factor,r['high']*base_factor
        occupancy=sum(rlow<=c<=rhigh for c in closes)/len(closes)
    if direction=='UNCERTAIN':
        correct=None
    elif direction=='RANGE':
        correct=bool(r and occupancy>=policy['range_occupancy_min'] and
                     (high/low-1)*100<=policy['range_amplitude_pct'] and label=='NEUTRAL_ENDPOINT')
    else:
        correct=direction==label
    # Trigger levels are evaluated on a common scale; corporate-action crossings
    # require typed level rebasing, so defer triggers rather than compare raw levels.
    crosses_action=base_factor!=1 or any(book.scale(p['symbol'],r['bar_end'],rows[-1]['bar_end'])!=1 for r in rows)
    triggers={'confirmation':None,'invalidation':None}
    if not crosses_action:
        triggers={k:_condition(rows,p['decision'].get(k),cal,2 if k=='confirmation' else 1) for k in triggers}
    return {'status':'complete','actual_label':label,'correct':correct,'return_pct':ret,
            'up_excursion_pct':(high/base_price-1)*100,'down_excursion_pct':(low/base_price-1)*100,
            'directional_mfe_pct':((high/base_price-1)*100 if signed==1 else (1-low/base_price)*100) if signed else None,
            'directional_mae_pct':((low/base_price-1)*100 if signed==1 else (1-high/base_price)*100) if signed else None,
            'range_occupancy':occupancy,'amplitude_pct':(high/low-1)*100,
            'baseline_at':base['bar_end'],'baseline_age_seconds':(issued-aware(base['bar_end'])).total_seconds(),
            'measured_at':rows[-1]['bar_end'],'target_sampling_lag_seconds':(target-aware(rows[-1]['bar_end'])).total_seconds(),
            'path_note':'partial first minute excluded; baseline is latest pre-issuance close, not an executable quote',
            'data_hash':digest(rows),'basis_hash':digest(book.payload()),'triggers':triggers,
            'trigger_limitation':'cross-action triggers not scored' if crosses_action else None}


def evaluate(db,as_of,*,basis_manifests=None,prediction_ids=None):
    setup(db);at=aware(as_of);allowed=set(prediction_ids) if prediction_ids is not None else None;out=[]
    for row in db.execute('SELECT * FROM forecast_predictions WHERE issued_at<=? ORDER BY issued_at,prediction_id',(at.isoformat(),)):
        if allowed is not None and row['prediction_id'] not in allowed:continue
        p=json.loads(row['payload'])
        prepared=json.loads(db.execute('SELECT payload FROM forecast_inputs WHERE input_hash=?',(p['input_hash'],)).fetchone()[0])
        result=outcome(db,p,prepared,at,basis_manifests=basis_manifests)
        evaluation_sources={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                            for name in ('forecast_ledger.py','forecast_basis.py','trade_clock.py','bar_quality.py')}
        result.update(prediction_id=p['prediction_id'],evaluated_at=at.isoformat(),
                      evaluator_hash=digest(evaluation_sources))
        # Checkpoints are diagnostic outcomes, not silently substituted main targets.
        result['checkpoints']={}
        if result['status']=='complete':
            for name,target in p.get('checkpoints',{}).items():
                result['checkpoints'][name]=outcome(db,dict(p,target_at=target,applicability='applicable' if target else 'not_applicable'),prepared,at,basis_manifests=basis_manifests)
        ident=digest(result)
        db.execute('INSERT OR IGNORE INTO forecast_evaluations VALUES (?,?,?,?,?)',(ident,p['prediction_id'],at.isoformat(),result['status'],canonical(result)))
        out.append(result)
    db.commit();return out


def observations(db,as_of):
    at=aware(as_of).isoformat();out=[]
    for row in db.execute('SELECT * FROM forecast_predictions WHERE issued_at<=? ORDER BY issued_at,prediction_id',(at,)):
        p=json.loads(row['payload'])
        e=db.execute('SELECT payload FROM forecast_evaluations WHERE prediction_id=? AND evaluated_at<=? ORDER BY evaluated_at DESC,evaluation_id DESC LIMIT 1',(row['prediction_id'],at)).fetchone()
        out.append((p,json.loads(e[0]) if e else {'status':'pending'}))
    return out


def score(db,as_of,*,mode='live',from_at=None,to_at=None,train_before=None,test_after=None):
    """Purge training rows whose target overlaps test start; modes never mix."""
    setup(db);groups={}
    for p,e in observations(db,as_of):
        issued=aware(p['issued_at']);target=aware(p['target_at']) if p['target_at'] else issued
        if p['mode']!=mode or (from_at and issued<aware(from_at)) or (to_at and issued>=aware(to_at)):continue
        if train_before and (issued>=aware(train_before) or target>=aware(train_before)):continue
        if test_after and issued<aware(test_after):continue
        v=p['versions'];key=(p['symbol'],p['horizon'],v['strategy_version'],v['code_tree_hash'],v['model_id'],v['policy_hash'],p.get('market_regime','unknown'))
        g=groups.setdefault(key,{'symbol':key[0],'horizon':key[1],'strategy_version':key[2],
             'code_tree_hash':key[3],'model_id':key[4],'policy_hash':key[5],
             'market_regime':key[6],'mode':mode,'policy_status':p['policy']['status'],'total':0,
             'applicable':0,'eligible':0,'insufficient':0,'not_applicable':0,'complete':0,'abstentions':0,'directional':0,'correct':0,
             'returns':[],'latencies':[],'invalidations':0,'observed_costs':[]})
        g['total']+=1
        usage=p.get('resource_usage') or {}
        if usage.get('cost_usd') is not None:g['observed_costs'].append(usage['cost_usd']/p.get('resource_allocation_slots',5))
        g['applicable']+=int(p['applicability']=='applicable')
        g['eligible']+=int(p['eligibility']=='eligible')
        g['insufficient']+=int(p['eligibility']=='insufficient')
        g['not_applicable']+=int(p['applicability']!='applicable')
        if p['decision']['direction']=='UNCERTAIN' and p['eligibility']=='eligible':g['abstentions']+=1
        if e['status']!='complete':continue
        g['complete']+=1
        if e.get('correct') is not None:
            g['directional']+=1;g['correct']+=int(e['correct'])
        if (e.get('triggers',{}).get('invalidation') or {}).get('triggered'):g['invalidations']+=1
        g['returns'].append(e['return_pct'])
        g['latencies'].append((issued-aware(json.loads(db.execute('SELECT payload FROM forecast_inputs WHERE input_hash=?',(p['input_hash'],)).fetchone()[0])['meta']['prepared_at'])).total_seconds())
    for g in groups.values():
        costs=g.pop('observed_costs')
        g['cost_observation_coverage']=len(costs)/g['total'] if g['total'] else None
        g['mean_allocated_cost_usd']=statistics.mean(costs) if costs else None
        g['accuracy_when_judged']=g['correct']/g['directional'] if g['directional'] else None
        g['judged_coverage']=g['directional']/g['eligible'] if g['eligible'] else None
        g['evaluation_coverage']=g['complete']/g['eligible'] if g['eligible'] else None
        g['mean_return_pct']=statistics.mean(g.pop('returns')) if g['complete'] else None
        if 'returns' in g:g.pop('returns')
        g['mean_prepare_to_issue_seconds']=statistics.mean(g.pop('latencies')) if g['complete'] else None
        if 'latencies' in g:g.pop('latencies')
    return {'groups':list(groups.values()),'notice':'Research metrics, not executable returns. No claim of superiority without matched out-of-sample comparisons.'}
