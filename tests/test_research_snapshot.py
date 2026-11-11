"""Unified snapshots, deduplicated acquisition, and price-basis eligibility."""
import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import analysis
import price_basis
import refresh
import research_batch
import research_data
import storage

TZ = ZoneInfo('Asia/Shanghai')
AT = dt.datetime(2026, 11, 11, 10, 46, tzinfo=TZ)
CFG = {'history': {}, 'watchlist': ['002241', '002242'],
       'market_indices': ['sh000001'], 'stock_names': {'002241': '歌尔股份'},
       'sector_mapping': {'002241': {'industry': '消费电子'},
                          '002242': {'industry': '消费电子'}},
       'news': {'active_event_days': 7, 'queries': {'macro': 'test'}}}


def bar(symbol, end, close=10, captured=None, tf='15'):
    return dict(symbol=symbol, timeframe=tf, bar_end=end.isoformat(), open=close,
                high=close+1, low=close-1, close=close, volume=100, amount=1000,
                adjust='', source='akshare', captured_at=(captured or end).isoformat(),
                quality='valid')


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(':memory:')
        refresh.setup(self.db)

    def tearDown(self):
        self.db.close()

    def put(self, symbol='002241'):
        for tf, ends in [('15', [AT.replace(hour=9, minute=45)+dt.timedelta(minutes=15*i) for i in range(5)]),
                         ('60', [AT.replace(hour=10, minute=30)-dt.timedelta(days=i) for i in (2,1,0)]),
                         ('daily', [AT.replace(hour=15, minute=0)-dt.timedelta(days=i) for i in (3,2,1)])]:
            storage.save_bars(self.db, [bar(symbol, end, tf=tf) for end in ends])

    def verify(self, start='2026-11-01', end='2026-11-11', dates=(), checked=AT):
        return price_basis.record(self.db, '002241', start, end, list(dates),
                                  'https://example.invalid/announcement', checked)


class SnapshotTests(SnapshotFixture):
    def test_unknown_basis_masks_cross_day_but_keeps_intraday_ma(self):
        ends = [AT.replace(hour=15, minute=0)-dt.timedelta(days=1)]
        ends += [AT.replace(hour=9, minute=45)+dt.timedelta(minutes=15*i) for i in range(5)]
        rows = [bar('002241', end, 10+i/10) for i,end in enumerate(ends)]
        f = price_basis.apply(self.db, '002241', rows, analysis.features(rows), AT)
        self.assertIsNotNone(f['moving_averages']['5']['value'])
        self.assertIsNone(f['moving_averages']['5']['slope_pct'])
        self.assertIsNotNone(f['returns_pct']['4'])
        self.assertIsNone(f['range']['high'])
        self.assertEqual(f['price_basis']['status'], 'unverified')

    def test_known_ex_date_blocks_only_crossing_windows(self):
        self.verify(dates=['2026-11-11'])
        ends=[AT.replace(hour=15,minute=0)-dt.timedelta(days=1)]
        ends += [AT.replace(hour=9,minute=45)+dt.timedelta(minutes=15*i) for i in range(5)]
        rows=[bar('002241',end) for end in ends]
        f=price_basis.apply(self.db,'002241',rows,analysis.features(rows),AT)
        self.assertEqual(f['price_basis']['status'],'crosses_ex_date')
        self.assertIsNotNone(f['moving_averages']['5']['value'])
        self.assertIsNone(f['moving_averages']['5']['slope_pct'])
        self.assertIsNotNone(f['returns_pct']['4'])

    def test_verification_cannot_leak_into_past(self):
        self.verify(checked=AT+dt.timedelta(minutes=1))
        self.assertEqual(price_basis.check(self.db,'002241','2026-11-10','2026-11-11',AT)['status'],'unverified')
        self.assertEqual(price_basis.check(self.db,'002241','2026-11-10','2026-11-11',AT+dt.timedelta(minutes=2))['status'],'verified')

    def test_partial_check_does_not_certify_entire_window(self):
        self.verify(start='2026-11-10',end='2026-11-10')
        self.assertEqual(price_basis.check(self.db,'002241','2026-11-09','2026-11-11',AT)['status'],'unverified')

    def test_corrected_same_window_uses_eligible_revision(self):
        self.verify(dates=['2026-11-11'],checked=AT)
        self.verify(dates=[],checked=AT+dt.timedelta(minutes=1))
        self.assertEqual(price_basis.check(self.db,'002241','2026-11-10','2026-11-11',AT)['status'],'crosses_ex_date')
        self.assertEqual(price_basis.check(self.db,'002241','2026-11-10','2026-11-11',AT+dt.timedelta(minutes=2))['status'],'verified')

    def test_snapshot_excludes_future_revisions_and_uses_one_cutoff(self):
        self.put();self.put('002242');self.put('sh000001');self.put('board:industry:消费电子')
        storage.save_bars(self.db,[bar('002241',AT.replace(minute=45),99,AT+dt.timedelta(minutes=1))])
        result=research_batch.snapshot(self.db,CFG,['002241','002242'],AT)
        self.assertEqual(result['meta']['as_of'],AT.isoformat())
        self.assertEqual(result['stocks'][0]['periods']['15']['close'],10)
        self.assertEqual(result['stocks'][0]['as_of'],result['stocks'][1]['as_of'])
        self.assertEqual(len(result['market']),1)
        self.assertEqual(len(result['boards']),1)

    def test_snapshot_retains_original_acquisition_statuses_without_provider_payloads(self):
        results = [dict(key='stock:002241:15', status='updated', latest_bar_end=AT.isoformat(),
                        captured_at=AT.isoformat(), count=5, bars=[{'large': 'payload'}]),
                   dict(key='news:x', status='failed', last_error='offline'),
                   dict(key='ranking:industry', status='deferred', last_error='cooldown'),
                   dict(key='lhb:list:20261111', status='pending'),
                   dict(key='stock:002241:60', status='cached', last_success=AT.isoformat())]
        result = research_batch.snapshot(self.db, CFG, ['002241'], AT, acquisitions=results)
        acquisition = result['acquisition']
        self.assertEqual(acquisition['count'], 5)
        for state in ('updated', 'cached', 'failed', 'deferred', 'pending'):
            self.assertEqual(acquisition[state], 1)
        self.assertEqual([r['status'] for r in acquisition['results']], [r['status'] for r in results])
        self.assertEqual(acquisition['results'][1]['last_error'], 'offline')
        self.assertEqual(acquisition['results'][0]['latest_bar_end'], AT.isoformat())
        self.assertNotIn('bars', acquisition['results'][0])
        self.assertIn('bars', results[0])

    def test_missing_data_is_incomplete_not_success(self):
        result=research_batch.snapshot(self.db,CFG,['002241'],AT)
        self.assertEqual(result['status'],'incomplete')
        self.assertTrue(any(x['severity']=='error' for x in result['issues']))
        self.assertEqual(result['synthesis']['action'],'WAIT')
        self.assertNotIn('direction',result['synthesis'])

    def test_stale_bar_is_not_cured_by_recent_capture(self):
        self.put();self.put('sh000001');self.put('board:industry:消费电子')
        result=research_batch.snapshot(self.db,CFG,['002241'],AT+dt.timedelta(minutes=15))
        self.assertTrue(any(x['code']=='bar_behind_boundary' for x in result['issues']))
        self.assertTrue(result['stocks'][0]['periods']['15']['reference_only'])
        self.assertEqual(result['stocks'][0]['periods']['15']['descriptive_label'],'unavailable')

    def test_batch_deduplicates_shared_work_and_keeps_failures(self):
        def collect(db,cfg,name,kind,tf,board_type=None,**kw):
            return {'key':f'{kind}:{name}:{tf}','status':'failed' if name=='002242' else 'cached','last_error':'offline' if name=='002242' else None}
        with patch.object(refresh,'collect_bars',side_effect=collect) as bars, \
             patch.object(refresh,'collect_board_members',return_value=[]) as members, \
             patch.object(refresh,'collect_news',return_value={'key':'news:x','status':'cached'}) as news, \
             patch.object(research_batch.lhb_collector,'collect',return_value={'status':'cached','watchlist_records':0,'seat_errors':[]}) as lhb:
            result=research_batch.acquire(self.db,CFG,['002241','002242','002241'],include_news=True,lhb_date='20261110')
        self.assertEqual(len(bars.call_args_list),8)  # 6 stock + 1 index + 1 board
        self.assertEqual(members.call_count,1);self.assertEqual(news.call_count,1)
        self.assertEqual(lhb.call_args.args[2],['002241','002242'])
        self.assertTrue(any(x['status']=='failed' for x in result))

    def test_as_of_ranking_excludes_future_and_future_error(self):
        for moment in [AT-dt.timedelta(minutes=2),AT+dt.timedelta(minutes=2)]:
            self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                [(moment.isoformat(),'industry',code,code,v,None) for code,v in [('A',2),('B',1)]])
        self.db.execute('INSERT INTO refresh_status VALUES (?,?,?,?,?,?)',
            ('ranking:industry',None,(AT+dt.timedelta(minutes=3)).isoformat(),'future failure',None,None))
        r=refresh.rank_output(self.db,'industry',5,as_of=AT)
        self.assertEqual(r['captured_at'],(AT-dt.timedelta(minutes=2)).isoformat())
        self.assertEqual(r['status'],'available')


class ResearchCLITests(unittest.TestCase):
    def test_historical_research_never_calls_collectors_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out, \
             patch.object(research_batch,'acquire') as acquire:
            rc=research_data.main(['--db',str(Path(tmp)/'db'),'research','--symbols','002241','--as-of',AT.isoformat(),'--format','json'])
        acquire.assert_not_called()
        self.assertEqual(rc,1)
        self.assertEqual(json.loads(out.getvalue())['meta']['as_of'],AT.isoformat())

    def test_rejects_naive_as_of(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            research_data.main(['context','--symbol','002241','--as-of','2026-11-11T10:45:00'])

    def test_rejects_force_with_historical_read(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            research_data.main(['research','--symbols','002241','--as-of',AT.isoformat(),'--force'])

class AdditionalSnapshotTests(SnapshotFixture):
    def test_day_return_uses_capture_cutoff_not_midnight(self):
        # Yesterday's close was first captured this morning, and is already known.
        rows=[bar('sh000001',AT.replace(hour=15,minute=0)-dt.timedelta(days=1),10,AT),
              bar('sh000001',AT.replace(minute=45),11,AT)]
        storage.save_bars(self.db,rows)
        r=research_batch.day_return(self.db,'sh000001',AT,rows,equity=False)
        self.assertAlmostEqual(r['value_pct'],10)

    def test_previous_partial_session_is_not_a_daily_close(self):
        rows=[bar('sh000001',AT.replace(hour=14,minute=45)-dt.timedelta(days=1)),
              bar('sh000001',AT.replace(minute=45))]
        storage.save_bars(self.db,rows)
        self.assertIsNone(research_batch.day_return(self.db,'sh000001',AT,rows,equity=False)['value_pct'])

    def test_basis_check_validates_coverage_and_sources(self):
        for start,end,dates,url in [
                ('2026-11-12','2026-11-11',[],'https://example.invalid'),
                ('2026-11-01','2026-11-12',[],'https://example.invalid'),
                ('2026-11-01','2026-11-11',['2026-10-31'],'https://example.invalid'),
                ('2026-11-01','2026-11-11',[],'http://example.invalid')]:
            with self.subTest(start=start,end=end,dates=dates,url=url),self.assertRaises(ValueError):
                price_basis.record(self.db,'002241',start,end,dates,url,AT)

    def test_duplicate_bar_end_is_reported_not_averaged_silently(self):
        self.put();self.put('sh000001');self.put('board:industry:消费电子')
        extra=bar('002241',AT.replace(minute=45),10)
        extra['source']='eastmoney-web'
        storage.save_bars(self.db,[extra])
        r=research_batch.snapshot(self.db,CFG,['002241'],AT)
        self.assertTrue(any(x['code']=='duplicate_bar_end' for x in r['issues']))
        self.assertIsNone(r['stocks'][0]['periods']['15']['close'])
        self.assertIsNone(r['stocks'][0]['day_return']['value_pct'])
        self.assertEqual(r['status'],'incomplete')

    def test_recorded_error_survives_a_later_success(self):
        import update_policy
        update_policy.setup(self.db)
        self.db.executemany('INSERT INTO refresh_attempts VALUES (?,?,?,?,?)',[
            ('ranking:industry',(AT-dt.timedelta(minutes=1)).isoformat(),'failed',None,'offline'),
            ('ranking:industry',(AT+dt.timedelta(minutes=1)).isoformat(),'updated',(AT+dt.timedelta(minutes=1)).isoformat(),None)])
        self.assertEqual(update_policy.state_at(self.db,'ranking:industry',AT)['last_error'],'offline')
        self.assertIsNone(update_policy.state_at(self.db,'ranking:industry',AT+dt.timedelta(minutes=2))['last_error'])

    def test_lhb_context_uses_recorded_error_at_cutoff(self):
        import update_policy
        import lhb_collector
        update_policy.setup(self.db)
        self.db.executemany('INSERT INTO refresh_attempts VALUES (?,?,?,?,?)',[
            ('lhb:list:20261110',(AT-dt.timedelta(minutes=1)).isoformat(),'failed',None,'offline'),
            ('lhb:list:20261110',(AT+dt.timedelta(minutes=1)).isoformat(),'updated',(AT+dt.timedelta(minutes=1)).isoformat(),None)])
        self.assertEqual(lhb_collector.context(self.db,'002241',AT)['status'],'collection_error')

    def test_unknown_basis_does_not_invent_day_return(self):
        rows=[bar('002241',AT.replace(hour=15,minute=0)-dt.timedelta(days=1),10,AT),
              bar('002241',AT.replace(minute=45),11,AT)]
        storage.save_bars(self.db,rows)
        self.assertEqual(research_batch.day_return(self.db,'002241',AT,rows,equity=True)['reason'],'price_basis_unverified')
        self.verify()
        self.assertAlmostEqual(research_batch.day_return(self.db,'002241',AT,rows,equity=True)['value_pct'],10)

    def test_one_failed_collector_does_not_skip_later_dependencies(self):
        calls=[]
        def fail(db,cfg,name,kind,tf,board_type=None,**kw):
            calls.append((kind,name,tf))
            if name=='002241': raise OSError('offline')
            return {'key':name,'status':'cached'}
        with patch.object(refresh,'collect_bars',side_effect=fail),patch.object(refresh,'collect_board_members',return_value=[]):
            results=research_batch.acquire(self.db,CFG,['002241','002242'])
        self.assertEqual(len(calls),8)
        self.assertEqual(sum(r['status']=='failed' for r in results),3)


class AdditionalCLITests(unittest.TestCase):
    def test_valid_lhb_date_stays_provider_string(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()), \
             patch.object(research_batch,'acquire',return_value=[]) as acq:
            research_data.main(['--db',str(Path(tmp)/'db'),'research','--symbols','002241','--lhb-date','20261110'])
        self.assertEqual(acq.call_args.kwargs['lhb_date'],'20261110')

    def test_utc_as_of_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()) as out:
            research_data.main(['--db',str(Path(tmp)/'db'),'bars','--symbol','002241','--timeframe','15','--as-of','2026-11-11T02:46:00Z'])
        self.assertEqual(json.loads(out.getvalue())['as_of'],AT.isoformat())

    def test_future_as_of_rejected_before_database_open(self):
        future=(dt.datetime.now(TZ)+dt.timedelta(days=1)).isoformat()
        with contextlib.redirect_stderr(io.StringIO()),patch.object(storage,'connect') as connect,self.assertRaises(SystemExit):
            research_data.main(['context','--symbol','002241','--as-of',future])
        connect.assert_not_called()

    def test_context_historical_capture_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db')
            with storage.connect(path) as db:
                rows=[bar('002241',AT.replace(hour=10,minute=15),10),
                      bar('002241',AT.replace(hour=10,minute=30),11),
                      bar('002241',AT.replace(hour=10,minute=45),12),
                      bar('002241',AT.replace(hour=10,minute=45),99,AT+dt.timedelta(minutes=1))]
                storage.save_bars(db,rows)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                research_data.main(['--db',path,'context','--symbol','002241','--as-of',AT.isoformat()])
            self.assertEqual(json.loads(out.getvalue())['stock']['15']['close'],12)

class FinalBoundaryTests(SnapshotFixture):
    def test_missing_previous_weekday_is_not_silently_used_as_yesterday(self):
        old=AT.replace(hour=15,minute=0)-dt.timedelta(days=2)
        rows=[bar('sh000001',old),bar('sh000001',AT.replace(minute=45))]
        storage.save_bars(self.db,rows)
        r=research_batch.day_return(self.db,'sh000001',AT,rows,equity=False)
        self.assertIsNone(r['value_pct'])
        self.assertEqual(r['reason'],'previous_session_missing_or_calendar_unverified')

    def test_verified_crossing_masks_return_but_preserves_current_close(self):
        self.verify(dates=['2026-11-11'])
        self.put()
        c=analysis.build(self.db,'002241',CFG,AT)
        self.assertEqual(c['stock']['daily']['close'],10)
        self.assertIsNotNone(c['stock']['15']['returns_pct']['4'])
        self.assertIsNone(c['stock']['60']['returns_pct']['1'])

    def test_pending_child_forces_incomplete_with_available_bars(self):
        self.put();self.put('sh000001');self.put('board:industry:消费电子')
        r=research_batch.snapshot(self.db,CFG,['002241'],AT,acquisitions=[
            {'key':'news','status':'pending','reason':'waiting'}])
        self.assertEqual(r['status'],'incomplete')
        self.assertTrue(any(x['code']=='acquisition_pending' for x in r['issues']))

    def test_hourly_lag_is_annotated_without_false_alignment_error(self):
        self.put();self.put('sh000001');self.put('board:industry:消费电子')
        r=research_batch.snapshot(self.db,CFG,['002241'],AT)
        self.assertFalse(r['stocks'][0]['hourly_covers_latest_15m'])
        self.assertEqual(r['status'],'limited')
        self.assertEqual(r['meta']['aligned_15_bar_end'],AT.replace(minute=45).isoformat())

    def test_legacy_failure_preserved_before_recovery(self):
        import update_policy
        past=AT-dt.timedelta(minutes=2)
        self.db.execute('INSERT INTO refresh_status VALUES (?,?,?,?,?,?)',
                        ('x',None,past.isoformat(),'previous failure',None,None))
        self.db.commit()
        update_policy.acquire(self.db,'x',AT,30,lambda:1,force=True)
        self.assertEqual(update_policy.state_at(self.db,'x',AT)['last_error'],'previous failure')

    def test_snapshot_does_not_commit_callers_open_transaction(self):
        self.db.execute('INSERT INTO sector_constituents VALUES (?,?,?,?,?)',
                        (AT.isoformat(),'板块','industry','000001','name'))
        research_batch.snapshot(self.db,CFG,['002241'],AT)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT count(*) FROM sector_constituents').fetchone()[0],0)

    def test_utc_price_coverage_query_uses_same_instant(self):
        self.verify()
        utc=AT.astimezone(dt.timezone.utc)
        self.assertEqual(price_basis.check(self.db,'002241','2026-11-10','2026-11-11',utc)['status'],'verified')

    def test_unaligned_market_suppresses_relative_return(self):
        self.put();self.put('board:industry:消费电子')
        storage.save_bars(self.db,[bar('sh000001',AT.replace(hour=10,minute=m)) for m in (0,15,30)])
        r=research_batch.snapshot(self.db,CFG,['002241'],AT)
        self.assertIsNone(r['stocks'][0]['relative_last_bar_pp']['sh000001'])
        self.assertIsNone(r['meta']['aligned_15_bar_end'])
        self.assertTrue(any(x['code']=='stock_index_time_mismatch' for x in r['issues']))

class EntryAndHistoryTests(SnapshotFixture):
    def test_same_timestamp_attempt_does_not_crash_or_keep_old_error(self):
        import update_policy
        class Clock(dt.datetime):
            @classmethod
            def now(cls,tz=None): return AT
        def fail(): raise OSError('offline')
        with patch.object(update_policy.dt,'datetime',Clock):
            self.assertEqual(update_policy.acquire(self.db,'x',AT,30,fail,force=True)['status'],'failed')
            self.assertEqual(update_policy.acquire(self.db,'x',AT,30,lambda:1,force=True)['status'],'updated')
        self.assertIsNone(update_policy.state_at(self.db,'x',AT)['last_error'])

    def test_basis_cli_requires_explicit_attestation(self):
        with contextlib.redirect_stderr(io.StringIO()),patch.object(storage,'connect') as connect,self.assertRaises(SystemExit):
            research_data.main(['basis-check','--symbol','002241','--start','2026-11-04','--end','2026-11-04',
                                '--source-url','https://example.invalid/notice','--ex-date','2026-11-04'])
        connect.assert_not_called()

    def test_basis_cli_registers_event_but_does_not_certify_wider_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'db')
            with contextlib.redirect_stdout(io.StringIO()):
                rc=research_data.main(['--db',path,'basis-check','--symbol','002241','--start','2026-11-04',
                                      '--end','2026-11-04','--source-url','https://example.invalid/notice',
                                      '--ex-date','2026-11-04','--confirm'])
            self.assertEqual(rc,0)
            with storage.connect(path) as db:
                now=dt.datetime.now(TZ)
                self.assertEqual(price_basis.check(db,'002241','2026-11-03','2026-11-05',now)['status'],'crosses_ex_date')
                self.assertEqual(price_basis.check(db,'002241','2026-11-05','2026-11-10',now)['status'],'unverified')
