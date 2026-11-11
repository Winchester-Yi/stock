import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import analysis
import refresh
import storage

TZ=ZoneInfo('Asia/Shanghai')
def at(h,m): return dt.datetime(2026,11,11,h,m,tzinfo=TZ)

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=storage.connect(Path(self.tmp.name)/'x.db'); refresh.setup(self.db)
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def test_bar_boundary_invalidates_but_morning_cache_hits(self):
        self.assertTrue(refresh.cache_valid(at(10,1).isoformat(),at(10,10),900,'15'))
        self.assertFalse(refresh.cache_valid(at(10,1).isoformat(),at(10,15),900,'15'))
        self.assertFalse(refresh.cache_valid(at(10,29).isoformat(),at(10,30),3600,'60'))
        self.assertFalse(refresh.cache_valid(at(14,59).isoformat(),at(15,0),86400,'daily'))
        self.assertTrue(refresh.cache_valid(at(11,31).isoformat(),at(12,30),900,'15'))
        self.assertTrue(refresh.cache_valid(at(15,1).isoformat(),at(17,0),900,'15'))
        self.assertFalse(refresh.cache_valid(at(15,1).isoformat(),at(17,0),900,None))
    def test_failure_preserves_last_success_and_is_visible(self):
        old=at(9,55).isoformat()
        self.db.execute('INSERT INTO refresh_status VALUES (?,?,?,?,?,?)',('x',old,old,None,4,'{}'));self.db.commit()
        def fail(): raise OSError('offline')
        out=refresh.acquire(self.db,'x',at(10,20),10,fail)
        self.assertEqual(out['status'],'failed');self.assertEqual(out['last_success'],old)
        row=self.db.execute('SELECT * FROM refresh_status WHERE key=?',('x',)).fetchone()
        self.assertEqual(row['last_success'],old);self.assertIn('offline',row['last_error'])
    def test_rank_is_board_level_and_compares_same_kind(self):
        t1=at(9,35).isoformat(); t2=at(9,40).isoformat()
        self.db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',[(t1,'industry','a','A',2,None),(t1,'industry','b','B',1,None),(t2,'industry','a','A',1,None),(t2,'industry','b','B',3,None),(t2,'concept','c','C',50,None)])
        v=refresh.rank_output(self.db,'industry',1)
        self.assertEqual(v['boards'][0]['board_code'],'b');self.assertEqual(v['boards'][0]['rank_change'],1)
    def test_batch_collects_only_requested_symbols_and_survives_failure(self):
        cfg={'market_indices':['sh000001'],'sector_mapping':{},'news':{'active_event_days':7}}
        seen=[]
        def collect(db,name,kind,tf,typ=None):
            seen.append((name,kind,tf))
            if tf=='60': raise OSError('offline')
            return 3
        with patch.object(refresh.data_collector,'collect',side_effect=collect):
            result=refresh.refresh(self.db,cfg,'002241',at(10,5))
        self.assertEqual(len(seen),4);self.assertEqual([r['status'] for r in result['results']].count('failed'),1)
        self.assertEqual({x[0] for x in seen},{'002241','sh000001'})
        self.assertFalse(result['calendar_verified'])

class IndicatorTests(unittest.TestCase):
    def test_numeric_windows_and_insufficient(self):
        bars=[{'close':float(i),'high':i+1.,'low':float(i),'amount':100.,'bar_end':f'2026-10-{i:02d}T10:00:00+08:00'} for i in range(1,22)]
        out=analysis.features(bars)
        self.assertAlmostEqual(out['returns_pct']['20'],2000)
        self.assertAlmostEqual(out['moving_averages']['5']['value'],19)
        self.assertAlmostEqual(out['range_amplitude_20_pct'],(22/2-1)*100)
        self.assertIsNone(analysis.features(bars[:4])['moving_averages']['5'])

class BoardCollectorTests(unittest.TestCase):
    def test_provider_rows_are_saved_and_incomplete_rows_ignored(self):
        import sys
        from types import SimpleNamespace
        class Frame:
            def to_dict(self, mode):
                return [{'板块代码':'BK01','板块名称':'板块甲','涨跌幅':2.5,'总市值':999},
                        {'板块代码':'BK02','板块名称':'板块乙','涨跌幅':'--'}]
        fake=SimpleNamespace(stock_board_industry_name_em=lambda:Frame())
        with tempfile.TemporaryDirectory() as tmp:
            db=storage.connect(Path(tmp)/'x.db'); refresh.setup(db)
            with patch.dict(sys.modules,{'akshare':fake}): result=refresh.board_ranking(db,'industry')
            self.assertEqual(result['count'],1)
            row=refresh.rank_output(db,'industry',5)['boards'][0]
            self.assertEqual(row['board_name'],'板块甲');self.assertIsNone(row['amount'])
            db.close()
