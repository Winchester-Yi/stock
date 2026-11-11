"""Regressions reproduced during the real manual-research trial."""
import datetime as dt
import io
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import analysis
import events
import news_collector
import refresh
import storage

TZ = ZoneInfo('Asia/Shanghai')
START = dt.datetime(2026, 11, 11, 9, 0, tzinfo=TZ)


class TrialRegressions(unittest.TestCase):
    def setUp(self):
        self.db = storage.connect(':memory:')
        refresh.setup(self.db)

    def tearDown(self):
        self.db.close()

    def article(self, key, title, published, category='company_watchlist', seen=None):
        stamp = (seen or START).isoformat()
        self.db.execute('INSERT INTO news_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (key, title, category, published, stamp, stamp, 'unverified',
                         'https://example.invalid/' + key, 'source', None, 'query'))

    def context(self):
        cfg = {'history': {}, 'market_indices': [], 'sector_mapping': {},
               'stock_names': {'002241': '歌尔股份'}, 'news': {'active_event_days': 7}}
        return analysis.build(self.db, '002241', cfg, START + dt.timedelta(minutes=1))

    def test_news_cannot_be_seen_before_each_feed_arrives(self):
        class Clock(dt.datetime):
            current = START
            @classmethod
            def now(cls, tz=None):
                return cls.current
        def response(url, **kwargs):
            Clock.current += dt.timedelta(seconds=10)
            index = int((Clock.current - START).total_seconds())
            index = 10 if index == 30 else index
            return io.BytesIO(f'<rss><channel><item><title>Report {index}</title>'
                             f'<link>https://example.invalid/{index}</link>'
                             '<pubDate>Wed, 11 Nov 2026 00:00:00 GMT</pubDate>'
                             '</item></channel></rss>'.encode())
        cfg = {'news': {'queries': {'macro': 'one', 'policy': 'two'}}}
        with patch.object(news_collector.dt, 'datetime', Clock), \
                patch.object(news_collector.urllib.request, 'urlopen', side_effect=response):
            self.assertEqual(news_collector.collect(self.db, cfg), (2, []))
            events.aggregate(self.db)
            since = (START - dt.timedelta(days=1)).isoformat()
            self.assertEqual(events.as_of(self.db, (START + dt.timedelta(seconds=5)).isoformat(), since), [])
            self.assertEqual(len(events.as_of(self.db, (START + dt.timedelta(seconds=15)).isoformat(), since)), 1)
            self.assertEqual(len(events.as_of(self.db, (START + dt.timedelta(seconds=25)).isoformat(), since)), 2)
            # Re-observation must not rewrite the original availability time.
            self.assertEqual(news_collector.collect(self.db, {'news': {'queries': {'macro': 'one'}}}), (0, []))
        stamps = [r[0] for r in self.db.execute('SELECT first_seen_at FROM news_events ORDER BY first_seen_at')]
        self.assertEqual(stamps[0], (START + dt.timedelta(seconds=10)).isoformat())
        self.assertEqual(self.db.execute('SELECT last_seen_at FROM news_events WHERE title=?',
                                        ('Report 10',)).fetchone()[0],
                         (START + dt.timedelta(seconds=30)).isoformat())

    def test_identical_provider_duplicates_are_deduplicated(self):
        import sys
        from types import SimpleNamespace
        class Frame:
            def to_dict(self, mode):
                return [{'板块代码': 'a', '板块名称': 'A', '涨跌幅': 2.},
                        {'板块代码': 'a', '板块名称': 'A', '涨跌幅': 2.},
                        {'板块代码': 'b', '板块名称': 'B', '涨跌幅': 1.}]
        fake = SimpleNamespace(stock_board_industry_name_em=lambda: Frame())
        with patch.dict(sys.modules, {'akshare': fake}):
            result = refresh.board_ranking(self.db, 'industry')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['duplicate_rows'], 1)
        self.assertEqual(refresh.rank_output(self.db, 'industry', 10)['status'], 'available')

    def test_conflicting_provider_duplicates_are_not_silently_overwritten(self):
        import sys
        from types import SimpleNamespace
        class Frame:
            def to_dict(self, mode):
                return [{'板块代码': 'a', '板块名称': 'A', '涨跌幅': 2.},
                        {'板块代码': 'a', '板块名称': 'A', '涨跌幅': 3.}]
        fake = SimpleNamespace(stock_board_industry_name_em=lambda: Frame())
        with patch.dict(sys.modules, {'akshare': fake}), \
                patch.object(refresh, '_stable_board_rows', return_value=Frame().to_dict('records')):
            with self.assertRaisesRegex(ValueError, 'conflicting board rows'):
                refresh.board_ranking(self.db, 'industry')
        self.assertEqual(self.db.execute('SELECT count(*) FROM board_rank_snapshots').fetchone()[0], 0)

    def test_conflicting_dynamic_pages_use_stable_code_fallback(self):
        import sys
        from types import SimpleNamespace
        class Frame:
            def to_dict(self, mode):
                return [{'板块代码': 'a', '板块名称': 'A', '涨跌幅': 2.},
                        {'板块代码': 'a', '板块名称': 'A', '涨跌幅': 3.}]
        stable = [{'板块代码': 'a', '板块名称': 'A', '涨跌幅': 3.},
                  {'板块代码': 'b', '板块名称': 'B', '涨跌幅': 1.}]
        fake = SimpleNamespace(stock_board_industry_name_em=lambda: Frame())
        with patch.dict(sys.modules, {'akshare': fake}), \
                patch.object(refresh, '_stable_board_rows', return_value=stable) as fallback:
            out = refresh.board_ranking(self.db, 'industry')
        fallback.assert_called_once_with('industry')
        self.assertTrue(out['pagination_fallback'])
        self.assertEqual(out['source'], 'eastmoney-web')
        self.assertEqual(out['count'], 2)

    def test_stable_pages_use_code_order_and_validate_total(self):
        import json
        import urllib.parse
        calls = []
        def response(request, **kwargs):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            calls.append(params)
            page = int(params['pn'][0])
            return io.BytesIO(json.dumps({'data': {'total': 2, 'diff': [
                {'f12': str(page), 'f14': 'Board', 'f3': float(page)}]}}).encode())
        with patch.object(refresh.urllib.request, 'urlopen', side_effect=response):
            rows = refresh._stable_board_rows('industry')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(p['fid'] == ['f12'] and p['po'] == ['0'] for p in calls))
        def incomplete(request, **kwargs):
            return io.BytesIO(json.dumps({'data': {'total': 2, 'diff': [
                {'f12': 'a', 'f14': 'A', 'f3': 1.}]}}).encode())
        with patch.object(refresh.urllib.request, 'urlopen', side_effect=incomplete):
            with self.assertRaisesRegex(ValueError, 'duplicate code'):
                refresh._stable_board_rows('industry')

    def test_failed_refresh_does_not_present_old_ranking_as_current(self):
        stamp = START.isoformat()
        self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                            [(stamp, 'industry', 'a', 'A', 2., None),
                             (stamp, 'industry', 'b', 'B', 1., None)])
        self.db.commit()
        def fail():
            raise OSError('offline')
        refresh.acquire(self.db, 'ranking:industry', START, 1, fail)
        out = refresh.rank_output(self.db, 'industry', 10)
        self.assertEqual(out['status'], 'stale')
        self.assertIn('offline', out['last_error'])
        self.assertFalse(out['comparison_available'])
        self.assertTrue(all(b['rank'] is None for b in out['boards']))

    def test_uniform_ranking_is_unusable_without_fabricated_ranks(self):
        stamp = START.isoformat()
        for change in (0., 2.):
            self.db.execute('DELETE FROM board_rank_snapshots')
            self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                                [(stamp, 'industry', 'a', 'A', change, None),
                                 (stamp, 'industry', 'b', 'B', change, None)])
            out = refresh.rank_output(self.db, 'industry', 10)
            self.assertEqual(out['status'], 'unusable')
            self.assertFalse(out['comparison_available'])
            self.assertTrue(all(b['rank'] is None and b['rank_change'] is None for b in out['boards']))

    def test_ties_share_rank_and_invalid_previous_snapshot_is_not_compared(self):
        t1 = START.isoformat(); t2 = (START + dt.timedelta(minutes=1)).isoformat()
        self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                            [(t1, 'industry', code, code, 0., None) for code in ('a', 'b', 'c')] +
                            [(t2, 'industry', code, code, change, None)
                             for code, change in [('a', 3.), ('b', 3.), ('c', 1.)]])
        out = refresh.rank_output(self.db, 'industry', 10)
        self.assertEqual([b['rank'] for b in out['boards']], [1, 1, 3])
        self.assertFalse(out['comparison_available'])
        self.assertTrue(all(b['prior_rank'] is None and b['rank_change'] is None for b in out['boards']))

    def test_valid_previous_ties_have_equal_prior_rank(self):
        t1 = START.isoformat(); t2 = (START + dt.timedelta(minutes=1)).isoformat()
        self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                            [(t1, 'industry', code, code, change, None)
                             for code, change in [('a', 3.), ('b', 3.), ('c', 1.)]] +
                            [(t2, 'industry', code, code, change, None)
                             for code, change in [('a', 1.), ('b', 2.), ('c', 3.)]])
        out = refresh.rank_output(self.db, 'industry', 10)
        self.assertTrue(out['comparison_available'])
        self.assertEqual([b['prior_rank'] for b in out['boards']], [3, 1, 1])
        self.assertEqual([b['rank_change'] for b in out['boards']], [2, -1, -2])

    def test_filtered_source_cannot_leave_an_uncited_assessment(self):
        import json
        self.article('old', 'Federal Reserve holds interest rates steady - A',
                     '2026-11-10T08:00:00+08:00', 'macro', START - dt.timedelta(days=8))
        self.article('new', 'Federal Reserve holds interest rates steady - B',
                     '2026-11-10T08:00:00+08:00', 'macro')
        events.aggregate(self.db)
        event_id = self.db.execute('SELECT event_id FROM event_groups').fetchone()[0]
        self.db.execute('INSERT INTO event_assessments VALUES (?,?,?)',
                        (event_id, START.isoformat(), json.dumps({'claims': [
                            {'text': 'Report claim', 'source_ids': ['old']}], 'impacts': []})))
        event = self.context()['events'][0]
        self.assertIsNone(event['assessment'])
        self.assertIsNone(event['assessment_at'])

    def test_changed_board_coverage_is_not_rank_movement(self):
        t1 = START.isoformat(); t2 = (START + dt.timedelta(minutes=1)).isoformat()
        self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                            [(t1, 'industry', 'a', 'A', 1., None), (t1, 'industry', 'b', 'B', 2., None),
                             (t2, 'industry', 'a', 'A', 1., None), (t2, 'industry', 'c', 'C', 2., None)])
        out = refresh.rank_output(self.db, 'industry', 10)
        self.assertFalse(out['comparison_available'])
        self.assertTrue(all(b['rank_change'] is None for b in out['boards']))

    def test_target_news_is_selected_before_global_limit_and_old_news_excluded(self):
        for i in range(40):
            self.article(f'm{i}', f'Macro headline {i}', '2026-11-11T08:00:00+08:00', 'macro')
            self.article(f'o{i}', f'润建股份 other headline {i}', '2026-11-11T08:00:00+08:00')
        self.article('target1', '歌尔股份获得专利甲', '2026-11-07T11:00:00+08:00')
        self.article('target2', '002241 发布报告乙', '2026-11-07T12:00:00+08:00')
        self.article('old', '歌尔股份旧公告', '2025-11-01T12:00:00+08:00')
        self.article('future', '歌尔股份未来消息', '2026-11-12T12:00:00+08:00')
        events.aggregate(self.db)
        out = self.context()['events']
        titles = [e['title'] for e in out]
        self.assertEqual(len(out), 30)
        self.assertEqual(titles[:2], ['002241 发布报告乙', '歌尔股份获得专利甲'])
        self.assertFalse(any('润建' in title or '旧公告' in title or '未来消息' in title for title in titles))
        self.assertEqual(out, self.context()['events'])

    def test_unknown_publication_time_is_explicit_and_missing_mapping_is_visible(self):
        self.article('unknown', '歌尔股份未注明时间', None)
        events.aggregate(self.db)
        ctx = self.context()
        self.assertTrue(ctx['events'][0]['publication_time_unknown'])
        self.assertIn('板块映射未配置', ' '.join(ctx['meta']['warnings']))

    def test_merged_old_sources_do_not_reenter_active_context(self):
        self.article('old', 'Federal Reserve holds interest rates steady - A',
                     '2026-11-10T08:00:00+08:00', 'macro', START - dt.timedelta(days=8))
        self.article('new', 'Federal Reserve holds interest rates steady - B',
                     '2026-11-10T08:00:00+08:00', 'macro')
        events.aggregate(self.db)
        self.assertEqual(len(self.context()['events']), 1)
        self.assertEqual([s['event_key'] for s in self.context()['events'][0]['sources']], ['new'])


if __name__ == '__main__':
    unittest.main()
