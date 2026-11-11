"""Synthetic, offline scenarios. These dates/prices are NOT market observations."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analysis
import bar_quality
import data_collector
import forecast
import forecast_ledger as ledger
import forecast_plan as plan
import forecast_validation as validation
from forecast_basis import BasisBook
import refresh
import storage
import stock_agent
from trade_clock import Calendar, TZ, aware, snapshot_role

AT=dt.datetime(2026,11,11,10,45,tzinfo=TZ)
CFG={'history':{},'watchlist':['002241'],'market_indices':[], 'sector_mapping':{},
     'stock_names':{'002241':'synthetic stock'},'news':{'active_event_days':7}}


def calendar_manifest():
    start=dt.date(2026,10,1);end=dt.date(2026,11,30)
    return {'verification':'operator_attested','start':str(start),'end':str(end),
        'verified_at':'2026-10-01T08:00:00+08:00','source_url':'https://example.invalid/synthetic-calendar',
        'open_dates':[(start+dt.timedelta(days=i)).isoformat() for i in range((end-start).days+1) if (start+dt.timedelta(days=i)).weekday()<5]}


def basis_manifest(end='2026-11-11',actions=None):
    return {'symbol':'002241','verification':'operator_attested','start':'2026-10-01','end':end,
        'verified_at':end+'T09:00:00+08:00','source_url':'https://example.invalid/synthetic-actions','actions':actions or []}


def bar(at,price=100.0,tf='1',symbol='002241',capture=None):
    return dict(symbol=symbol,timeframe=tf,bar_end=at.isoformat(),open=price,high=price+0.01,
        low=price-0.01,close=price,amount=1000,volume=100,adjust='',source='akshare',
        captured_at=(capture or at).isoformat(),quality='valid')


class ClockTests(unittest.TestCase):
    def setUp(self):self.cal=Calendar.load(calendar_manifest(),AT)

    def test_intraday_add_excludes_lunch_and_never_spills_overnight(self):
        at=AT.replace(hour=11,minute=25)
        self.assertEqual(self.cal.add_intraday(at,15),AT.replace(hour=13,minute=10))
        self.assertIsNone(self.cal.add_intraday(AT.replace(hour=14),150))
        self.assertIsNone(self.cal.add_intraday(AT.replace(hour=12),15))

    def test_explicit_holiday_skipped_and_outside_coverage_not_certified(self):
        m=calendar_manifest();m['open_dates'].remove('2026-11-12')
        cal=Calendar.load(m,AT)
        self.assertEqual(cal.next_day(AT.date()).isoformat(),'2026-11-13')
        self.assertFalse(cal.verified(dt.date(2027,1,1)))
        with self.assertRaises(ValueError):cal.next_day(dt.date(2026,11,30))

    def test_future_attestation_cannot_enter_historical_input(self):
        m=calendar_manifest();m['verified_at']='2026-11-12T09:00:00+08:00'
        with self.assertRaises(ValueError):Calendar.load(m,AT)

    def test_snapshot_roles_distinguish_close_context_from_live(self):
        self.assertEqual(snapshot_role('2026-11-11T16:06:00+08:00',AT.replace(hour=23),300),'session_context')
        self.assertEqual(snapshot_role('2026-11-10T16:06:00+08:00',AT.replace(hour=23),300),'historical')
        self.assertEqual(snapshot_role(AT-dt.timedelta(seconds=30),AT,300),'live_candidate')

    def test_missing_bar_masks_only_dependent_metrics(self):
        rows=[bar(t,tf='15') for t in self.cal.ends(AT.date(),'15')[:6] if t.hour!=10 or t.minute!=15]
        f=analysis.features(rows,self.cal)
        self.assertEqual(f['returns_pct']['1'],0)
        self.assertIsNone(f['returns_pct']['4'])
        self.assertIn('returns_pct.4',f['window_issues'])
        self.assertFalse(bar_quality.complete(rows,calendar=self.cal))

    def test_lunch_and_weekend_are_not_missing_bars(self):
        rows=[bar(AT.replace(hour=11,minute=30),tf='15'),bar(AT.replace(hour=13,minute=15),tf='15')]
        self.assertTrue(bar_quality.complete(rows,calendar=self.cal))
        fri=dt.datetime(2026,11,13,15,tzinfo=TZ);mon=dt.datetime(2026,11,16,9,45,tzinfo=TZ)
        self.assertTrue(bar_quality.complete([bar(fri,tf='15'),bar(mon,tf='15')],calendar=self.cal))

    def test_one_and_five_minute_end_labels(self):
        self.assertEqual(data_collector.end_time('2026-11-11T01:31:00+00:00','1').hour,9)
        self.assertEqual(data_collector.end_time('2026-11-11 09:35:00','5').minute,35)
        with self.assertRaises(ValueError):data_collector.end_time('2026-11-11 09:34:00','5')

    def test_half_hour_gap_not_exported_as_single_fifteen_minute_return(self):
        rows=[bar(AT.replace(hour=10,minute=m),100+i,tf='15') for i,m in enumerate((0,15,45))]
        f=analysis.features(rows,self.cal)
        self.assertIsNone(f['return_last_bar_pct'])
        self.assertEqual(f['return_intervals']['1']['start'],'2026-11-11T10:15:00+08:00')


class ForecastFixture(unittest.TestCase):
    def setUp(self):
        self.db=storage.connect(':memory:');refresh.setup(self.db)
        self.manifest=calendar_manifest();self.cal=Calendar.load(self.manifest,AT)
        for tf in ('1','5','15','60','daily'):
            times=self.cal.expected(AT-dt.timedelta(days=35),AT,tf)
            storage.save_bars(self.db,[bar(t,100+i/1000,tf=tf) for i,t in enumerate(times[-400:])])
        self.prepared=plan.prepare(self.db,CFG,['002241'],AT,calendar_manifest=self.manifest,basis_manifests=[basis_manifest()])

    def tearDown(self):self.db.close()

    def decision(self,direction='BULLISH'):
        d=plan.decision_template(self.prepared)
        for h in plan.HORIZONS:
            d['stocks'][0]['horizons'][h].update(direction=direction,reasoning='synthetic test direction',evidence_ids=['S:002241:5','S:002241:daily'])
        return d

    def record(self,direction='BULLISH'):
        return ledger.record(self.db,self.prepared,self.decision(direction),mode='replay',issued_at=AT,now=AT)

    def future(self,skip=None,price=101):
        last=dt.datetime(2026,11,13,15,tzinfo=TZ)
        rows=[bar(t,price,capture=t+dt.timedelta(seconds=1)) for t in self.cal.expected(AT,last,'1') if t!=skip]
        storage.save_bars(self.db,rows)
        return last+dt.timedelta(minutes=1)


class PlanTests(ForecastFixture):
    def test_five_independent_slots_in_four_families(self):
        s=self.prepared['stocks'][0]
        self.assertEqual(set(s['horizons']),set(plan.HORIZONS))
        self.assertTrue(all(h['eligibility']=='eligible' for h in s['horizons'].values()))
        self.assertEqual(s['horizons']['short_15m']['target_at'],'2026-11-11T11:00:00+08:00')
        self.assertEqual(s['horizons']['long_150m']['target_at'],'2026-11-11T14:45:00+08:00')
        self.assertIsNone(s['horizons']['long_150m']['checkpoints']['180'])

    def test_after_close_only_intraday_slots_not_applicable(self):
        for tf in ('60','daily'):
            storage.save_bars(self.db,[bar(t,tf=tf) for t in self.cal.ends(AT.date(),tf) if t>AT])
        p=plan.prepare(self.db,CFG,['002241'],AT.replace(hour=23),calendar_manifest=self.manifest,basis_manifests=[basis_manifest()])
        hs=p['stocks'][0]['horizons']
        for h in plan.HORIZONS[:3]:self.assertEqual(hs[h]['eligibility'],'not_applicable')
        self.assertEqual(hs['next_1d']['eligibility'],'eligible')
        self.assertEqual(p['stocks'][0]['baseline']['bar_end'],'2026-11-11T15:00:00+08:00')

    def test_preopen_today_is_a_real_target_not_mistaken_for_off_session_intraday(self):
        before=AT.replace(hour=8,minute=0)
        hs=plan.targets(before,self.cal)
        self.assertEqual(hs['short_15m']['applicability'],'not_applicable')
        self.assertEqual(hs['today_close']['target_at'],'2026-11-11T15:00:00+08:00')
        self.assertEqual(hs['today_close']['applicability'],'applicable')
        self.assertEqual(plan.targets(before,Calendar())['next_1d']['applicability'],'unknown')

    def test_calendar_missing_does_not_invent_trade_dates(self):
        p=plan.prepare(self.db,CFG,['002241'],AT,basis_manifests=[basis_manifest()])
        self.assertEqual(p['stocks'][0]['horizons']['short_15m']['eligibility'],'insufficient')
        self.assertIsNone(p['stocks'][0]['horizons']['next_1d']['target_at'])

    def test_unverified_cross_day_data_does_not_block_valid_intraday_windows(self):
        p=plan.prepare(self.db,CFG,['002241'],AT,calendar_manifest=self.manifest)
        hs=p['stocks'][0]['horizons']
        self.assertEqual(hs['short_15m']['eligibility'],'eligible')
        self.assertEqual(hs['next_1d']['eligibility'],'insufficient')

    def test_directional_ineligible_slot_rejected_but_other_horizons_independent(self):
        d=self.decision();self.prepared['stocks'][0]['horizons']['short_15m']['eligibility']='insufficient'
        self.prepared['input_hash']=plan.digest({k:v for k,v in self.prepared.items() if k!='input_hash'});d['input_hash']=self.prepared['input_hash']
        with self.assertRaisesRegex(ValueError,'ineligible'):plan.validate_decision(self.prepared,d)
        d['stocks'][0]['horizons']['short_15m']['direction']='UNCERTAIN'
        plan.validate_decision(self.prepared,d)

    def test_unknown_evidence_or_modified_input_is_rejected(self):
        d=self.decision();d['stocks'][0]['horizons']['short_15m']['evidence_ids']=['S:fake:5']
        with self.assertRaises(ValueError):plan.validate_decision(self.prepared,d)
        self.prepared['meta']['prepared_at']='changed'
        with self.assertRaises(ValueError):plan.verify_input(self.prepared)

    def test_invented_level_and_single_bar_invalidation_rejected(self):
        d=self.decision();h=d['stocks'][0]['horizons']['short_15m']
        h['invalidation']={'kind':'single_bar_reference','price':1,'formed_at':AT.isoformat(),
                           'relation':'below','timeframe':'5','evidence_id':'S:002241:5'}
        with self.assertRaises(ValueError):plan.validate_decision(self.prepared,d)
        h['invalidation']['kind']='pivot'
        with self.assertRaisesRegex(ValueError,'not found'):plan.validate_decision(self.prepared,d)

    def test_range_requires_actual_observed_interval(self):
        d=self.decision('RANGE');h=d['stocks'][0]['horizons']['short_15m']
        h['range']={'low':1,'high':2,'evidence_id':'S:002241:5'}
        with self.assertRaisesRegex(ValueError,'observed interval'):plan.validate_decision(self.prepared,d)

    def test_versions_pin_config_policy_source_tree_and_model_status(self):
        v=self.prepared['versions']
        self.assertEqual(len(v['code_tree_hash']),64);self.assertEqual(v['model_identity'],'unknown')
        self.assertEqual(len(v['config_hash']),64);self.assertIn('forecast_plan.py',v['source_hashes'])

    def test_stale_lower_timeframe_is_not_eligible_just_because_one_minute_is_fresh(self):
        self.db.execute("DELETE FROM bars WHERE timeframe='5' AND bar_end=?",(AT.isoformat(),));self.db.commit()
        p=plan.prepare(self.db,CFG,['002241'],AT,calendar_manifest=self.manifest,basis_manifests=[basis_manifest()])
        self.assertIn('stale_5',p['stocks'][0]['horizons']['short_15m']['blockers'])
        self.assertEqual(p['stocks'][0]['horizons']['next_1d']['eligibility'],'eligible')

    def test_recorded_collector_error_blocks_affected_horizon_even_on_readonly_prepare(self):
        import update_policy
        update_policy.setup(self.db)
        self.db.execute('INSERT INTO refresh_attempts VALUES (?,?,?,?,?)',
            ('bars:stock:-:002241:5',AT.isoformat(),'failed',None,'offline'))
        self.db.commit()
        p=plan.prepare(self.db,CFG,['002241'],AT,calendar_manifest=self.manifest,basis_manifests=[basis_manifest()])
        self.assertIn('recorded_collection_error_5',p['stocks'][0]['horizons']['short_15m']['blockers'])

    def test_verified_event_availability_is_enforced_without_title_based_inference(self):
        fact={'event_id':'synthetic-fact','verification':'operator_attested','fact':'Synthetic fact, not an actual disclosure',
            'symbols':['002241'],'published_at':AT.isoformat(),'available_at':AT.isoformat(),
            'verified_at':AT.isoformat(),'source_url':'https://example.invalid/synthetic-fact'}
        p=plan.prepare(self.db,CFG,['002241'],AT,verified_events=[fact])
        self.assertIn('V:synthetic-fact',p['stocks'][0]['allowed_evidence_ids'])
        fact['available_at']=(AT+dt.timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'not available'):plan.prepare(self.db,CFG,['002241'],AT,verified_events=[fact])

    def test_breakout_confirmation_and_invalidation_share_actual_observed_boundary(self):
        d=self.decision();e=self.prepared['evidence']['S:002241:5']
        c={'kind':'breakout_boundary','price':e['features']['range']['high'],
            'formed_at':e['range_formed_at'],'evidence_id':'S:002241:5','relation':'above','timeframe':'5'}
        h=d['stocks'][0]['horizons']['short_15m'];h['confirmation']=c
        h['invalidation']=dict(c,relation='below')
        plan.validate_decision(self.prepared,d)
        h['invalidation']['relation']='above'
        with self.assertRaisesRegex(ValueError,'same boundary'):plan.validate_decision(self.prepared,d)

    def test_direction_requires_price_evidence_from_its_own_horizon(self):
        d=self.decision();d['stocks'][0]['horizons']['next_1d']['evidence_ids']=['S:002241:1']
        with self.assertRaisesRegex(ValueError,'own horizon'):plan.validate_decision(self.prepared,d)

    def test_future_fitted_policy_cannot_leak_into_prediction(self):
        with self.assertRaisesRegex(ValueError,'fitted after'):
            plan.prepare(self.db,CFG,['002241'],AT,calendar_manifest=self.manifest,
                         policy={'fitted_at':'2026-11-12T10:00:00+08:00'})


class LedgerTests(ForecastFixture):
    def test_live_cannot_be_backdated_and_replay_not_in_live_scores(self):
        with self.assertRaises(ValueError):ledger.record(self.db,self.prepared,self.decision(),issued_at=AT,now=AT)
        self.record()
        self.assertEqual(ledger.score(self.db,AT,mode='live')['groups'],[])
        self.assertEqual(len(ledger.score(self.db,AT,mode='replay')['groups']),5)

    def test_expired_preparation_and_boundary_crossing_block_live_record(self):
        with self.assertRaisesRegex(ValueError,'too old'):
            ledger.record(self.db,self.prepared,self.decision(),now=AT+dt.timedelta(minutes=3))
        self.assertEqual(self.db.execute("SELECT count(*) FROM sqlite_master WHERE name='forecast_predictions'").fetchone()[0],0)

    def test_properly_issued_predictions_are_append_only_and_replay_idempotent(self):
        first=self.record();second=self.record()
        self.assertEqual([p['prediction_id'] for p in first],[p['prediction_id'] for p in second])
        self.assertEqual(self.db.execute('SELECT count(*) FROM forecast_predictions').fetchone()[0],5)
        self.assertEqual(first[0]['issued_at'],AT.isoformat())

    def test_pending_then_complete_and_direction_aware_bearish_excursions(self):
        ps=self.record('BEARISH')
        pending=ledger.evaluate(self.db,AT)
        self.assertTrue(all(e['status']=='pending' for e in pending))
        end=self.future(price=98)
        outcomes=ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        short=next(e for e in outcomes if e['prediction_id']==ps[0]['prediction_id'])
        self.assertEqual(short['status'],'complete');self.assertTrue(short['correct'])
        self.assertGreater(short['directional_mfe_pct'],0)
        self.assertEqual(short['measured_at'],'2026-11-11T11:00:00+08:00')
        self.assertIn('10',short['checkpoints'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM forecast_evaluations').fetchone()[0],10)

    def test_missing_future_minute_never_filled_or_scored(self):
        ps=self.record();end=self.future(skip=AT.replace(hour=10,minute=55))
        es=ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        e=next(e for e in es if e['prediction_id']==ps[0]['prediction_id'])
        self.assertEqual(e['status'],'pending');self.assertIn('missing',e['reason'])

    def test_future_capture_revision_not_available_before_evaluation_cutoff(self):
        self.record();self.future()
        short_cutoff=AT.replace(hour=11,minute=1)
        e=ledger.evaluate(self.db,short_cutoff)
        self.assertEqual(sum(x['status']=='complete' for x in e),1)
        original=next(x for x in e if x['status']=='complete')['return_pct']
        storage.save_bars(self.db,[bar(AT.replace(hour=11,minute=0),200,capture=short_cutoff+dt.timedelta(days=1))])
        again=ledger.evaluate(self.db,short_cutoff)
        self.assertEqual(next(x for x in again if x['status']=='complete')['return_pct'],original)

    def test_cross_day_evaluation_requires_updated_attested_basis(self):
        self.record();end=self.future()
        es=ledger.evaluate(self.db,end)
        self.assertTrue(any(e['status']=='unscorable' and e.get('reason')=='evaluation_price_basis_unverified' for e in es))

    def test_baselines_match_actual_issue_baseline_and_policy_without_models(self):
        self.record();end=self.future()
        ids=validation.record_baselines(self.db,end)
        self.assertEqual(len(ids),15)
        ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        cmp=validation.paired_comparison(self.db,end,mode='replay')
        self.assertEqual(len(cmp['groups']),15)
        self.assertTrue(all(g['matched']==1 for g in cmp['groups']))
        self.assertEqual(ledger.score(self.db,end,mode='live')['groups'],[])

    def test_purged_training_excludes_predictions_whose_target_overlaps_split(self):
        self.record();end=self.future();ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        train=ledger.score(self.db,end,mode='replay',train_before='2026-11-11T11:10:00+08:00')
        self.assertEqual(len(train['groups']),1)
        self.assertEqual(train['groups'][0]['horizon'],'short_15m')
        with self.assertRaises(ValueError):validation.walk_forward(self.db,end,[{'train_end':AT.isoformat(),'test_start':end.isoformat(),'test_end':AT.isoformat()}])

    def test_calibration_requires_enough_cases_and_no_future_training(self):
        self.record()
        with self.assertRaisesRegex(ValueError,'insufficient'):validation.calibrate(self.db,AT,AT,mode='replay')
        with self.assertRaises(ValueError):validation.calibrate(self.db,AT,AT+dt.timedelta(days=1),mode='replay')

    def test_positive_confirmation_uses_two_fully_post_issuance_closed_candles(self):
        d=self.decision();e=self.prepared['evidence']['S:002241:5']
        c={'kind':'breakout_boundary','price':e['features']['range']['high'],
            'formed_at':e['range_formed_at'],'evidence_id':'S:002241:5','relation':'above','timeframe':'5'}
        h=d['stocks'][0]['horizons']['short_15m'];h['confirmation']=c;h['invalidation']=dict(c,relation='below')
        ps=ledger.record(self.db,self.prepared,d,mode='replay',issued_at=AT,now=AT)
        end=self.future(price=110)
        es=ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        e=next(e for e in es if e['prediction_id']==ps[0]['prediction_id'])
        self.assertEqual(e['triggers']['confirmation']['at'],'2026-11-11T10:55:00+08:00')
        self.assertFalse(e['triggers']['invalidation']['triggered'])

    def test_range_is_not_correct_merely_because_endpoint_returns_to_baseline(self):
        d=self.decision('UNCERTAIN');h=d['stocks'][0]['horizons']['short_15m']
        r=self.prepared['evidence']['S:002241:5']['features']['range']
        h.update(direction='RANGE',range=dict(low=r['low'],high=r['high'],evidence_id='S:002241:5'))
        ps=ledger.record(self.db,self.prepared,d,mode='replay',issued_at=AT,now=AT)
        base=ps[0]['baseline']['close'];end=self.future(price=base)
        t=AT.replace(hour=10,minute=50);wild=bar(t,base,capture=end)
        wild.update(high=130,low=80);storage.save_bars(self.db,[wild])
        es=ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        e=next(e for e in es if e['prediction_id']==ps[0]['prediction_id'])
        self.assertEqual(e['actual_label'],'NEUTRAL_ENDPOINT');self.assertFalse(e['correct'])

    def test_unknown_cost_is_not_zero_and_observed_batch_cost_is_allocated(self):
        d=self.decision();d['resource_usage']={'cost_usd':1.0,'input_tokens':100,'output_tokens':50}
        ledger.record(self.db,self.prepared,d,mode='replay',issued_at=AT,now=AT)
        g=ledger.score(self.db,AT,mode='replay')['groups'][0]
        self.assertEqual(g['mean_allocated_cost_usd'],0.2)
        self.assertEqual(g['cost_observation_coverage'],1)

    def test_training_policy_fits_each_horizon_and_never_claims_validation(self):
        self.record();end=self.future();ledger.evaluate(self.db,end,basis_manifests=[basis_manifest('2026-11-13')])
        end+=dt.timedelta(minutes=1)
        p=validation.calibrate(self.db,end,end,mode='replay',minimum=1)
        self.assertEqual(set(p['by_horizon']),set(plan.HORIZONS))
        self.assertEqual(p['status'],'train_fitted_not_validated')
        folds=[{'train_end':AT.isoformat(),'test_start':AT.isoformat(),'test_end':end.isoformat()}]
        result=validation.walk_forward(self.db,end,folds,mode='replay')
        self.assertEqual(result['folds'][0]['train']['groups'],[])
        self.assertEqual(len(result['folds'][0]['test']['groups']),5)

    def test_legacy_evaluation_never_claims_row_offsets_are_horizon_accuracy(self):
        self.db.execute('INSERT INTO analysis_runs(symbol,as_of,context_hash,context_json,decision_json,created_at) VALUES (?,?,?,?,?,?)',('002241',AT.isoformat(),'legacy','{}','{}',AT.isoformat()))
        with contextlib.redirect_stdout(io.StringIO()):stock_agent.evaluate(self.db)
        e=json.loads(self.db.execute('SELECT evaluated_json FROM analysis_runs').fetchone()[0])
        self.assertEqual(e['status'],'legacy_unscorable');self.assertNotIn('returns_pct',e)


class BasisTests(unittest.TestCase):
    def test_cross_dividend_factor_preserves_comparable_price_not_false_bearish(self):
        m=basis_manifest(actions=[{'ex_date':'2026-11-11','pre_price_multiplier':0.9}])
        book=BasisBook([m],AT)
        prev=bar(AT-dt.timedelta(days=1),100);current=bar(AT,90)
        rows=book.adjusted('002241',[prev,current])
        self.assertEqual(rows[0]['close'],90);self.assertEqual(prev['close'],100)
        m['actions'][0]['pre_price_multiplier']=None
        self.assertIsNone(BasisBook([m],AT).adjusted('002241',[prev,current]))

    def test_invalid_factors_future_attestation_and_uncovered_intervals_rejected(self):
        m=basis_manifest();book=BasisBook([m],AT)
        self.assertIsNone(book.scale('002241','2026-09-01','2026-11-11'))
        m['verified_at']='2026-11-12T09:00:00+08:00'
        with self.assertRaises(ValueError):BasisBook([m],AT)
        for factor in (0,-1,True,float('nan')):
            m=basis_manifest(actions=[{'ex_date':'2026-11-11','pre_price_multiplier':factor}])
            with self.assertRaises(ValueError):BasisBook([m],AT)


class CLITests(unittest.TestCase):
    def test_prepare_replay_template_report_and_no_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(plan,'acquire') as acquire:
            snapshot=Path(tmp)/'snapshot.json';template=Path(tmp)/'decision.json'
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc=forecast.main(['--db',str(Path(tmp)/'db'),'prepare','--symbols','002241',
                    '--as-of',AT.isoformat(),'--snapshot-out',str(snapshot),'--template-out',str(template)])
            self.assertEqual(rc,2);acquire.assert_not_called()
            self.assertIn('多周期研究证据',out.getvalue())
            p=json.loads(snapshot.read_text());d=json.loads(template.read_text())
            self.assertEqual(p['meta']['forecast_type'],'historical_replay')
            plan.validate_decision(p,d)
            with storage.connect(Path(tmp)/'db') as db:
                with self.assertRaisesRegex(ValueError,'historical'):
                    ledger.record(db,p,d,now=AT)
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                forecast.main(['--db',str(Path(tmp)/'db'),'prepare','--symbols','002241','--snapshot-out',str(snapshot)])

    def test_refresh_and_historical_cutoff_cannot_mix(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            forecast.main(['prepare','--symbols','002241','--as-of',AT.isoformat(),'--refresh'])

    def test_preparation_preserves_explicit_board_selection_without_editing_config(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()) as out:
            forecast.main(['--db',str(Path(tmp)/'db'),'prepare','--symbols','002241','--as-of',AT.isoformat(),
                           '--board','industry:synthetic-board','--format','json'])
        p=json.loads(out.getvalue())
        self.assertIn('synthetic-board',p['stocks'][0]['sector_mapping']['industry'])


if __name__=='__main__':unittest.main()
