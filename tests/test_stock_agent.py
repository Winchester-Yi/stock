import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import analysis
import data_collector as dc
import stock_agent as sa
import storage
import update_policy

TZ=ZoneInfo('Asia/Shanghai')
def at(s): return dt.datetime.fromisoformat('2026-11-02T'+s+'+08:00')
class Frame:
    def __init__(self, rows): self.rows=rows
    def to_dict(self, kind): return self.rows

def row(t='10:15:00',close=10):
    return {'时间':'2026-11-02 '+t,'开盘':10,'最高':11,'最低':9,'收盘':close,'成交量':5,'成交额':5000}

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=storage.connect(Path(self.tmp.name)/'test.db')
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def test_closed_session_bars_and_units(self):
        bars=dc.normalize(Frame([row('10:15:00'),row('10:30:00')]),'002241','15',at('10:20:00'))
        self.assertEqual(len(bars),1); self.assertEqual(bars[0]['volume'],500)
        with self.assertRaises(ValueError): dc.normalize(Frame([row('12:00:00')]),'002241','15',at('13:00:00'))
    def test_invalid_price_and_revision_availability(self):
        with self.assertRaises(ValueError): dc.normalize(Frame([row(close=12)]),'002241','15',at('10:20:00'))
        first=dc.normalize(Frame([row()]),'002241','15',at('10:20:00')); storage.save_bars(self.db,first); storage.save_bars(self.db,first)
        newer=dc.normalize(Frame([row(close=10.5)]),'002241','15',at('11:00:00')); storage.save_bars(self.db,newer)
        self.assertEqual(len(storage.bars_at(self.db,'002241','15',at('10:30:00').isoformat())),1)
        self.assertEqual(storage.bars_at(self.db,'002241','15',at('10:30:00').isoformat())[0]['close'],10)
        self.assertEqual(storage.bars_at(self.db,'002241','15',at('11:01:00').isoformat())[0]['close'],10.5)
    def test_news_first_seen_prevents_lookahead(self):
        self.db.execute('INSERT INTO news_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',('x','title','macro',at('09:00:00').isoformat(),at('11:00:00').isoformat(),at('11:00:00').isoformat(),'unverified','https://example.com','src',None,'query'))
        self.assertFalse(storage.events_at(self.db,at('10:00:00').isoformat(),at('09:00:00').isoformat()))
        self.assertEqual(len(storage.events_at(self.db,at('11:01:00').isoformat(),at('09:00:00').isoformat())),1)
    def test_member_snapshot_as_of(self):
        self.db.executemany('INSERT INTO sector_members VALUES (?,?,?,?,?)',[(at('10:00:00').isoformat(),'板块','industry','000001',1),(at('11:00:00').isoformat(),'板块','industry','000001',-1)])
        cfg={'history':{},'market_indices':[],'sector_mapping':{'002241':{'industry':'板块'}},'news':{'active_event_days':7}}
        ctx=analysis.build(self.db,'002241',cfg,at('10:30:00'))
        self.assertEqual(ctx['sectors'][0]['breadth'],1)
    def test_bad_claude_result_is_rejected(self):
        with self.assertRaises(ValueError): sa.validate_decision({'symbol':'002241'},'002241',at('10:30:00').isoformat())
        with patch.object(sa.subprocess,'run') as run:
            run.return_value.stdout=json.dumps({'result':'not json'})
            with self.assertRaises(ValueError): sa.invoke({'meta':{'symbol':'002241','as_of':at('10:30:00').isoformat()}, 'agent_input':{'format':'agent-evidence-v1','text':'测试证据','evidence_ids':[]}},{'claude':{'command':'claude'}})
            run.assert_called_once()
    def test_missing_or_failed_data_cannot_call_claude(self):
        cfg={'history':{},'market_indices':[],'sector_mapping':{},'news':{'active_event_days':7}}
        with patch.object(dc,'collect',side_effect=OSError('offline')), patch.object(sa,'invoke') as invoke:
            with self.assertRaises(RuntimeError): sa.run(self.db,cfg,'002241')
            invoke.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM analysis_runs').fetchone()[0],0)
    def test_raw_context_fallback_and_invented_evidence_ids_are_rejected(self):
        meta={'symbol':'002241','as_of':at('10:30:00').isoformat()}
        cfg={'claude':{'command':'claude'}}
        with patch.object(sa.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'brief required'):
                sa.invoke({'meta':meta},cfg)
            run.assert_not_called()
            value={**meta,'direction':'UNCERTAIN','action':'WAIT',
                   'scenarios':[{'name':'unknown','evidence':'test','invalidation':'test'}],
                   'evidence_ids':['S:invented:15'],'uncertainties':[],
                   'reasoning':'test','trigger':'wait','risk':'test'}
            run.return_value.stdout=json.dumps({'result':json.dumps(value)})
            with self.assertRaisesRegex(ValueError,'unknown evidence ID'):
                sa.invoke({'meta':meta,'agent_input':{'format':'agent-evidence-v1',
                          'text':'test','evidence_ids':['S:002241:15']}},cfg)
            run.assert_called_once()

    def test_pivots_need_following_bar(self):
        bars=dc.normalize(Frame([row('09:45:00'),row('10:00:00'),row('10:15:00')]),'002241','15',at('10:20:00'))
        self.assertEqual(analysis.features(bars)['local_pivots'],[])

if __name__=='__main__': unittest.main()

class WorkflowIntegrationTests(unittest.TestCase):
    def test_run_and_evaluate_with_simulated_provider_and_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); db=storage.connect(root/'workflow.db')
            import shutil
            shutil.copytree(sa.ROOT/'docs',root/'docs')
            now=dt.datetime(2026,11,11,10,46,tzinfo=TZ); end=now.replace(minute=45)
            class Clock(dt.datetime):
                @classmethod
                def now(cls,tz=None): return cls.fromisoformat(now.isoformat())
            def collect(db,symbol,kind,timeframe,board_type=None):
                delta=dt.timedelta(days=1) if timeframe=='daily' else dt.timedelta(minutes=int(timeframe))
                rows=[]
                tf_end=update_policy.completed_boundary(now,timeframe)
                for i in range(5):
                    t=tf_end-(4-i)*delta
                    rows.append(dict(symbol=symbol,timeframe=timeframe,bar_end=t.isoformat(),open=10,high=11,low=9,close=10+i/10,volume=100,amount=1000,adjust='',source='akshare',captured_at=now.isoformat(),quality='valid'))
                storage.save_bars(db,rows)
                return len(rows)
            class Result: pass
            def fake_claude(args,input,**kwargs):
                assert '15 分钟个股研究与决策' in input
                assert '新闻采集、事件聚合与提取' not in input
                assert '输入数据JSON:' not in input
                brief=input.split('输入证据简报：\n',1)[1]
                assert '# Agent 研究证据简报' in brief
                assert '跨日价格口径未核验' in brief
                assert 'scenarios' not in brief
                meta={'symbol':input.split('决策标的：',1)[1].splitlines()[0], 'as_of':input.split('决策时点：',1)[1].splitlines()[0]}
                response={'symbol':meta['symbol'],'as_of':meta['as_of'],'direction':'UNCERTAIN','action':'WAIT','scenarios':[{'name':'range','evidence':'test','invalidation':'test'}],'evidence_ids':[],'uncertainties':['test'],'reasoning':'test','trigger':'wait','risk':'test'}
                result=Result(); result.stdout=json.dumps({'result':json.dumps(response)}); return result
            cfg={'history':{},'market_indices':[],'sector_mapping':{},'news':{'active_event_days':7},'claude':{'command':'claude'}}
            with patch.object(sa,'ROOT',root),patch.object(dc,'collect',side_effect=collect),patch.object(sa.subprocess,'run',side_effect=fake_claude),patch.object(sa.dt,'datetime',Clock):
                sa.run(db,cfg,'002241')
            run=db.execute('SELECT * FROM analysis_runs').fetchone(); self.assertEqual(json.loads(run['decision_json'])['action'],'WAIT')
            self.assertTrue(Path(run['report_path']).exists())
            self.assertEqual(json.loads(run['context_json'])['agent_input']['format'],'agent-evidence-v1')
            rows=[]
            for i in range(1,5):
                rows.append(dict(symbol='002241',timeframe='15',bar_end=(end+dt.timedelta(minutes=15*i)).isoformat(),open=10.4,high=11,low=10,close=10.5,volume=100,amount=1000,adjust='',source='akshare',captured_at=(now+dt.timedelta(minutes=15*i)).isoformat(),quality='valid'))
            storage.save_bars(db,rows); sa.evaluate(db)
            self.assertIsNotNone(db.execute('SELECT evaluated_json FROM analysis_runs').fetchone()[0]); db.close()
