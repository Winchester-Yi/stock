"""Contracts for disclosure-, session-, and close-driven acquisition."""
import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import storage
import refresh
import lhb_collector as lhb
import data_collector
import update_policy as policy

TZ = ZoneInfo('Asia/Shanghai')
def at(s): return dt.datetime.fromisoformat(s + '+08:00')


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(':memory:')
        refresh.setup(self.db)
    def tearDown(self): self.db.close()
    def seed(self, key, stamp, metadata):
        import json
        self.db.execute('INSERT INTO refresh_status VALUES (?,?,?,?,?,?)',
                        (key, stamp.isoformat(), stamp.isoformat(), None, 1, json.dumps(metadata)))
        self.db.commit()
    def test_settled_disclosure_survives_ttl_and_force_rechecks(self):
        self.seed('lhb:list:20261110', at('2026-11-11T09:00:00'), {'settled': True})
        fn = unittest.mock.Mock(return_value={'count': 1, 'settled': True})
        out = policy.acquire(self.db, 'lhb:list:20261110', at('2026-12-01T09:00:00'), 60, fn, policy='disclosure')
        self.assertEqual(out['status'], 'cached'); fn.assert_not_called()
        policy.acquire(self.db, 'lhb:list:20261110', at('2026-12-01T09:00:00'), 60, fn, policy='disclosure', force=True)
        fn.assert_called_once()
    def test_provisional_disclosure_is_checked_again_next_date(self):
        self.seed('x', at('2026-11-10T23:59:00'), {'settled': False})
        fn = unittest.mock.Mock(return_value={'count': 1, 'settled': True})
        policy.acquire(self.db, 'x', at('2026-11-11T00:01:00'), 1800, fn, policy='disclosure')
        fn.assert_called_once()
    def test_daily_cache_survives_night_weekend_until_new_close(self):
        self.seed('daily', at('2026-11-13T15:10:00'), {'latest_bar_end': '2026-11-13T15:00:00+08:00'})
        fn = unittest.mock.Mock(return_value=1)
        for now in ('2026-11-14T12:00:00', '2026-11-16T10:00:00'):
            out = policy.acquire(self.db, 'daily', at(now), 60, fn, policy='daily')
            self.assertEqual(out['status'], 'cached')
        fn.assert_not_called()
        policy.acquire(self.db, 'daily', at('2026-11-16T15:01:00'), 60, fn, policy='daily')
        fn.assert_called_once()
    def test_recent_capture_is_not_a_new_completed_daily_bar(self):
        now = at('2026-11-16T15:02:00')
        self.seed('daily', at('2026-11-16T15:01:00'), {'latest_bar_end': '2026-11-13T15:00:00+08:00'})
        fn = unittest.mock.Mock(return_value=1)
        out = policy.acquire(self.db, 'daily', now, 60, fn, policy='daily', retry_seconds=300)
        self.assertEqual(out['status'], 'pending'); fn.assert_not_called()
    def test_snapshots_freeze_after_close_but_reopen_invalidates(self):
        self.seed('breadth', at('2026-11-13T15:01:00'), {})
        fn = unittest.mock.Mock(return_value=1)
        out = policy.acquire(self.db, 'breadth', at('2026-11-16T09:20:00'), 300, fn, policy='session_snapshot')
        self.assertEqual(out['status'], 'cached'); fn.assert_not_called()
        policy.acquire(self.db, 'breadth', at('2026-11-16T09:31:00'), 300, fn, policy='session_snapshot')
        fn.assert_called_once()
    def test_failure_cooldown_keeps_error_visible_and_force_bypasses(self):
        now = dt.datetime.now(TZ)
        fn = unittest.mock.Mock(side_effect=OSError('offline'))
        first = policy.acquire(self.db, 'x', now, 300, fn, retry_seconds=300)
        second = policy.acquire(self.db, 'x', now + dt.timedelta(seconds=1), 300, fn, retry_seconds=300)
        self.assertEqual(first['status'], 'failed'); self.assertEqual(second['status'], 'deferred')
        self.assertIn('offline', second['last_error']); self.assertEqual(fn.call_count, 1)
        policy.acquire(self.db, 'x', now + dt.timedelta(seconds=2), 300, fn, retry_seconds=300, force=True)
        self.assertEqual(fn.call_count, 2)
    def test_new_bar_boundary_is_not_hidden_by_retry_cooldown(self):
        self.seed('bars', at('2026-11-11T09:59:59'), {'latest_bar_end': '2026-11-11T09:45:00+08:00'})
        fn = unittest.mock.Mock(return_value={'count': 1})
        out = policy.acquire(self.db, 'bars', at('2026-11-11T10:00:00'), 900, fn,
                             timeframe='15', policy='completed_bar', retry_seconds=300)
        self.assertEqual(out['status'], 'updated'); fn.assert_called_once()
    def test_completed_hour_bar_survives_expired_ttl_before_next_close(self):
        self.seed('bars', at('2026-11-10T15:01:00'), {'latest_bar_end': '2026-11-10T15:00:00+08:00'})
        fn = unittest.mock.Mock(return_value=1)
        out = policy.acquire(self.db, 'bars', at('2026-11-11T10:00:00'), 60, fn,
                             timeframe='60', policy='completed_bar')
        self.assertEqual(out['status'], 'cached'); fn.assert_not_called()
    def test_missing_catalog_does_not_bypass_failure_cooldown(self):
        cfg = {'refresh_ttl_seconds': {'breadth': 300}}
        with patch.object(data_collector, 'collect_members', side_effect=OSError('offline')) as source:
            first = refresh.collect_board_members(self.db, cfg, '板块', 'industry')
            second = refresh.collect_board_members(self.db, cfg, '板块', 'industry')
            self.assertEqual(first[0]['status'], 'failed')
            self.assertEqual(second[0]['status'], 'deferred'); self.assertEqual(source.call_count, 1)
            refresh.collect_board_members(self.db, cfg, '板块', 'industry', force=True)
            self.assertEqual(source.call_count, 2)
    def test_constituent_names_are_stable_while_quote_snapshots_change(self):
        import sys
        from types import SimpleNamespace
        class Frame:
            def __init__(self, rows): self.rows=rows
            def to_dict(self, mode): return self.rows
        first = [{'代码': '002241', '名称': '歌尔股份', '涨跌幅': 1.}]
        second = [{'代码': '002241', '名称': '歌尔股份', '涨跌幅': -1.}]
        third = second + [{'代码': '000530', '名称': '测试', '涨跌幅': 2.}]
        source = unittest.mock.Mock(side_effect=[Frame(first), Frame(second), Frame(third)])
        with patch.dict(sys.modules, {'akshare': SimpleNamespace(stock_board_industry_cons_em=source)}):
            data_collector.collect_members(self.db, '板块', 'industry')
            catalog = self.db.execute('SELECT MAX(as_of) FROM sector_constituents').fetchone()[0]
            data_collector.collect_members(self.db, '板块', 'industry')
            self.assertEqual(self.db.execute('SELECT MAX(as_of) FROM sector_constituents').fetchone()[0], catalog)
            self.assertEqual(self.db.execute('SELECT count(*) FROM sector_members').fetchone()[0], 2)
            data_collector.collect_members(self.db, '板块', 'industry')
            self.assertNotEqual(self.db.execute('SELECT MAX(as_of) FROM sector_constituents').fetchone()[0], catalog)
    def test_breadth_cache_is_shared_and_expires_independently_of_catalog(self):
        import sys
        from types import SimpleNamespace
        class Frame:
            def to_dict(self, mode): return [{'代码': '002241', '名称': '歌尔股份', '涨跌幅': 1.}]
        source = unittest.mock.Mock(return_value=Frame())
        with patch.dict(sys.modules, {'akshare': SimpleNamespace(stock_board_industry_cons_em=source)}):
            first = refresh.collect_board_members(self.db, {}, '板块', 'industry')
            second = refresh.collect_board_members(self.db, {}, '板块', 'industry')
        self.assertEqual(first[0]['status'], 'updated'); self.assertEqual(second[0]['status'], 'cached')
        self.assertEqual(second[1]['status'], 'cached'); self.assertEqual(source.call_count, 1)

    def test_news_queries_change_cache_scope(self):
        self.assertNotEqual(policy.news_key({'a': 'old'}), policy.news_key({'a': 'new'}))
        self.assertEqual(policy.news_key({'a': 'x', 'b': 'y'}), policy.news_key({'b': 'y', 'a': 'x'}))


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(':memory:')
    def tearDown(self): self.db.close()
    def records(self):
        return [dict(symbol=s, name='测试', trade_date='2026-11-10', reason='原因',
                     buy=10, sell=7, net=3, amount=17) for s in ('000530', '002241')]
    def test_complete_daily_list_is_shared_across_stocks_and_direct_calls(self):
        with patch.object(lhb, 'from_akshare', return_value=self.records()) as source:
            a = lhb.collect(self.db, '20261110', ['000530'], include_seats=False)
            b = lhb.collect(self.db, '20261110', ['002241'], include_seats=False)
        self.assertEqual(source.call_count, 1)
        self.assertEqual(a['watchlist_records'], 1); self.assertEqual(b['watchlist_records'], 1)
        self.assertEqual(b['list_cache']['status'], 'cached')
        self.assertEqual(self.db.execute('SELECT count(*) FROM lhb_records').fetchone()[0], 2)
    def test_no_match_is_cached_but_not_an_empty_provider_response(self):
        with patch.object(lhb, 'from_akshare', return_value=self.records()) as source:
            a = lhb.collect(self.db, '20261110', ['999999'], include_seats=False)
            b = lhb.collect(self.db, '20261110', ['999999'], include_seats=False)
        self.assertEqual(a['watchlist_records'], 0)
        self.assertEqual(b['list_cache']['status'], 'cached'); self.assertEqual(source.call_count, 1)
    def test_today_before_check_window_and_future_date_do_not_request(self):
        with patch.object(lhb, 'from_akshare') as source:
            for date in ('20261111', '20261112'):
                out = lhb.collect(self.db, date, ['002241'], now=at('2026-11-11T10:00:00'))
                self.assertEqual(out['status'], 'pending')
            source.assert_not_called()
    def test_seat_failure_does_not_redownload_list_and_seat_success_is_reused(self):
        detail = [dict(reason='原因', side='买入', department='机构专用', buy=10, sell=7, net=3)]
        with patch.object(lhb, 'from_akshare', return_value=self.records()) as source, \
                patch.object(lhb, 'seats', side_effect=[OSError('offline'), detail]) as seats:
            a = lhb.collect(self.db, '20261110', ['002241'])
            self.assertTrue(a['seat_errors'])
            b = lhb.collect(self.db, '20261110', ['002241'], force_seats=True)
            c = lhb.collect(self.db, '20261110', ['002241'])
        self.assertEqual(source.call_count, 1); self.assertEqual(seats.call_count, 2)
        self.assertFalse(b['seat_errors']); self.assertEqual(c['seat_cache'][0]['status'], 'cached')
    def test_seat_labels_need_not_equal_list_reasons_and_new_list_invalidates_old_seats(self):
        detail = [dict(reason='接口短类型名', side='买入', department='机构专用', buy=10, sell=7, net=3)]
        with patch.object(lhb, 'from_akshare', return_value=self.records()), patch.object(lhb, 'seats', return_value=detail) as source:
            lhb.collect(self.db, '20261110', ['002241'])
            self.assertEqual(len(lhb.context(self.db, '002241', dt.datetime.now(TZ))['seats']), 1)
            lhb.collect(self.db, '20261110', ['002241'], include_seats=False, force=True)
            self.assertEqual(lhb.context(self.db, '002241', dt.datetime.now(TZ))['seats'], [])
            lhb.collect(self.db, '20261110', ['002241'])
            self.assertEqual(source.call_count, 2)
            self.assertEqual(len(lhb.context(self.db, '002241', dt.datetime.now(TZ))['seats']), 1)
    def test_history_before_new_list_still_reads_original_revision(self):
        with patch.object(lhb, 'from_akshare', return_value=self.records()):
            lhb.collect(self.db, '20261110', ['002241'], include_seats=False)
        before = dt.datetime.now(TZ)
        with patch.object(lhb, 'from_akshare', return_value=self.records()[:1]):
            lhb.collect(self.db, '20261110', ['002241'], include_seats=False, force=True)
        self.assertEqual(len(lhb.context(self.db, '002241', before)['records']), 1)
        self.assertEqual(lhb.context(self.db, '002241', dt.datetime.now(TZ))['records'], [])

    def test_analysis_tags_old_records_as_background_and_respects_batch_replacement(self):
        import analysis
        with patch.object(lhb, 'from_akshare', return_value=self.records()):
            lhb.collect(self.db, '20261110', ['002241'], include_seats=False)
        cfg = {'history': {}, 'market_indices': [], 'news': {'active_event_days': 7}}
        ctx = analysis.build(self.db, '002241', cfg, dt.datetime.now(TZ))
        self.assertEqual(ctx['longhubang']['role'], 'post_close_background')
        self.assertEqual(ctx['longhubang']['latest_trade_date'], '2026-11-10')
        self.assertIsNotNone(ctx['longhubang']['age_calendar_days'])
        with patch.object(lhb, 'from_akshare', return_value=self.records()[:1]):
            lhb.collect(self.db, '20261110', ['002241'], include_seats=False, force=True)
        ctx = analysis.build(self.db, '002241', cfg, dt.datetime.now(TZ))
        self.assertEqual(ctx['longhubang']['records'], [])
        self.assertEqual(ctx['longhubang']['coverage'][0]['target_records'], 0)


if __name__ == '__main__': unittest.main()
