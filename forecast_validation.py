"""Frozen-input baselines, purged walk-forward comparison, and train-only label policies."""
import copy
import json
import statistics
import hashlib
from pathlib import Path

from forecast_plan import HORIZONS, canonical, digest, policy_checked
from forecast_ledger import observations, setup
from trade_clock import aware


def baseline_decision(prepared,symbol,horizon,kind):
    s=next(s for s in prepared['stocks'] if s['symbol']==symbol)
    d={'direction':'UNCERTAIN','reasoning':f'Frozen-input {kind} benchmark; not a validated strategy',
       'evidence_ids':[],'confirmation':None,'invalidation':None,'range':None}
    if s['horizons'][horizon]['eligibility']!='eligible':return d
    tf='5' if horizon=='short_15m' else 'daily' if horizon.startswith('next_') else '15'
    f=s['periods'][tf];signal=None;ident=f'S:{symbol}:{tf}'
    if kind=='trend':signal=(f.get('returns_pct') or {}).get('4')
    elif kind=='price_action':
        ma=(f.get('moving_averages') or {}).get('5')
        if ma and ma.get('distance_pct') is not None and f.get('return_last_bar_pct') is not None:
            if ma['distance_pct']*f['return_last_bar_pct']>0:signal=ma['distance_pct']
    elif kind=='market':
        for m in prepared['shared'].get('market',[]):
            if not m.get('reference_only') and m.get('last_bar_end')==s['periods']['15'].get('last_bar_end'):
                signal=(m.get('returns_pct') or {}).get('1');ident='M:'+m['symbol'];break
    else:raise ValueError('unknown baseline')
    if signal is not None and signal!=0:
        d.update(direction='BULLISH' if signal>0 else 'BEARISH',evidence_ids=[ident])
    return d


def record_baselines(db,as_of,prediction_ids=None):
    setup(db);allowed=set(prediction_ids) if prediction_ids else None;out=[]
    rows=db.execute('SELECT * FROM forecast_predictions WHERE issued_at<=?',(aware(as_of).isoformat(),)).fetchall()
    with db:
        for row in rows:
            p=json.loads(row['payload'])
            if p.get('decision_source')=='deterministic_baseline' or (allowed is not None and p['prediction_id'] not in allowed):continue
            prepared=json.loads(db.execute('SELECT payload FROM forecast_inputs WHERE input_hash=?',(p['input_hash'],)).fetchone()[0])
            for kind in ('trend','price_action','market'):
                b=copy.deepcopy(p);b.pop('prediction_id',None)
                b['decision']=baseline_decision(prepared,p['symbol'],p['horizon'],kind)
                b['versions']=dict(b['versions'],strategy_version='baseline/'+kind+'-v1',model_id='deterministic')
                b['decision_source']='deterministic_baseline'
                b['versions']['baseline_implementation_hash']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                b['derived_from']=p['prediction_id'];b['derived_at']=aware(as_of).isoformat()
                b['prediction_id']=digest({k:v for k,v in b.items() if k!='derived_at'})
                db.execute('INSERT OR IGNORE INTO forecast_predictions VALUES (?,?,?,?,?,?,?,?,?)',
                    (b['prediction_id'],b['input_hash'],b['symbol'],b['horizon'],b['issued_at'],b['target_at'],b['mode'],b['versions']['strategy_version'],canonical(b)))
                out.append(b['prediction_id'])
    return out


def paired_comparison(db,as_of,*,mode='live',test_after=None,test_before=None):
    """Compare only exactly matched issued-time, baseline, target, mode and policy cases."""
    records=observations(db,as_of);by_id={p['prediction_id']:(p,e) for p,e in records};groups={}
    for b,be in records:
        if b.get('decision_source')!='deterministic_baseline' or b['mode']!=mode:continue
        if test_after and aware(b['issued_at'])<aware(test_after):continue
        if test_before and aware(b['issued_at'])>=aware(test_before):continue
        parent=by_id.get(b['derived_from'])
        if not parent:continue
        p,pe=parent
        if any(p[k]!=b[k] for k in ('issued_at','target_at','baseline','policy','mode')):
            raise ValueError('mismatched benchmark case')
        key=(p['horizon'],p['versions']['strategy_version'],b['versions']['strategy_version'],p['versions']['code_tree_hash'],p['versions']['model_id'],p['versions']['policy_hash'])
        g=groups.setdefault(key,{'horizon':key[0],'strategy':key[1],'baseline':key[2],
            'code_tree_hash':key[3],'model_id':key[4],'policy_hash':key[5],
            'matched':0,'both_complete':0,'strategy_judged':0,'baseline_judged':0,
            'strategy_correct':0,'baseline_correct':0,'both_judged':0,
            'wins':0,'losses':0,'ties':0,'incompatible_evaluations':0})
        g['matched']+=1
        if pe['status']!='complete' or be['status']!='complete':continue
        if any(pe.get(k)!=be.get(k) for k in ('evaluator_hash','data_hash','basis_hash')):
            g['incompatible_evaluations']+=1;continue
        g['both_complete']+=1
        for name,e in (('strategy',pe),('baseline',be)):
            if e.get('correct') is not None:
                g[name+'_judged']+=1;g[name+'_correct']+=int(e['correct'])
        if pe.get('correct') is not None and be.get('correct') is not None:
            g['both_judged']+=1
            if pe['correct']==be['correct']:g['ties']+=1
            elif pe['correct']:g['wins']+=1
            else:g['losses']+=1
    for g in groups.values():
        for name in ('strategy','baseline'):
            g[name+'_accuracy']=g[name+'_correct']/g[name+'_judged'] if g[name+'_judged'] else None
            g[name+'_coverage']=g[name+'_judged']/g['matched'] if g['matched'] else None
    return {'groups':list(groups.values()),'notice':'Matched cases only; differing abstention coverage must not be hidden. Baselines are frozen-input retrospective computations, not live forecasts.'}


def walk_forward(db,as_of,folds,*,mode='replay'):
    """Explicit, non-overlapping test folds. Training is purged through each target."""
    from forecast_ledger import score
    out=[];previous_end=None
    for fold in folds:
        train,test,end=map(aware,(fold['train_end'],fold['test_start'],fold['test_end']))
        if not train<=test<end or (previous_end and test<previous_end) or end>aware(as_of):
            raise ValueError('invalid, overlapping or future test fold')
        previous_end=end
        out.append({'fold':fold,
            'train':score(db,as_of,mode=mode,train_before=train.isoformat()),
            'test':score(db,as_of,mode=mode,from_at=test.isoformat(),to_at=end.isoformat()),
            'paired_test':paired_comparison(db,as_of,mode=mode,test_after=test.isoformat(),test_before=end.isoformat())})
    return {'folds':out,'notice':'Fold membership is time-based, purged at target time. Policy/model selection must be frozen before each test; this tool does not tune on test scores.'}


def calibrate(db,as_of,train_end,*,mode='replay',minimum=30):
    """Distribution-based neutral/range bands, not optimized accuracy or a promise."""
    end=aware(train_end)
    if end>aware(as_of):raise ValueError('training end in future')
    groups={}
    for p,e in observations(db,end):
        if p['mode']!=mode or p.get('decision_source')=='deterministic_baseline':continue
        if aware(p['issued_at'])>=end or not p['target_at'] or aware(p['target_at'])>=end:continue
        if e['status']=='complete':
            g=groups.setdefault(p['horizon'],{'values':[],'amplitudes':[],'ids':[]})
            g['values'].append(abs(e['return_pct']));g['amplitudes'].append(e['amplitude_pct']);g['ids'].append(p['prediction_id'])
    if any(h not in groups or len(groups[h]['values'])<minimum for h in HORIZONS):
        raise ValueError('insufficient completed training cases per horizon')
    by_horizon={}
    ids=[]
    for h,g in groups.items():
        band=max(statistics.median(g['values'])*0.5,0.000001)
        by_horizon[h]={'neutral_band_pct':band,'range_amplitude_pct':max(statistics.median(g['amplitudes']),band,0.000001)}
        ids.extend(g['ids'])
    return policy_checked({'version':'distribution-fit-v1','status':'train_fitted_not_validated',
        'by_horizon':by_horizon,'training_through':end.isoformat(),'fitted_at':aware(as_of).isoformat(),
        'training_cases_by_horizon':{h:len(g['values']) for h,g in groups.items()},'training_ids_hash':digest(sorted(ids))})
