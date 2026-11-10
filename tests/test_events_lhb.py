import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import events
import lhb_collector as lhb
import storage

class EventAndLhbTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=storage.connect(Path(self.tmp.name)/'test.sqlite3')
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def article(self,key,title,seen,category='macro',published=None):
        self.db.execute('INSERT INTO news_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',(key,title,category,published,seen,seen,'unverified','https://example.com/'+key,'source',None,'query'))
    def test_grouping_asof_and_distinct(self):
        a='2026-11-10T09:00:00+08:00'; b='2026-11-10T10:00:00+08:00'
        self.article('a','Federal Reserve holds interest rates steady - Agency A',a)
        self.article('b','Federal Reserve holds interest rates steady - Agency B',b)
        self.article('c','Federal Reserve cuts interest rates sharply',b)
        self.assertEqual(events.aggregate(self.db)['new_events'],2)
        old=events.as_of(self.db,a,'2026-11-01T00:00:00+08:00')
        self.assertEqual(len(old),1); self.assertEqual(len(old[0]['sources']),1)
        now=events.as_of(self.db,b,'2026-11-01T00:00:00+08:00')
        self.assertEqual(sorted(len(x['sources']) for x in now),[1,2])
        self.assertEqual(events.aggregate(self.db)['new_events'],0)
    def test_different_category_does_not_merge(self):
        t='2026-11-10T09:00:00+08:00'; self.article('a','Federal Reserve holds interest rates steady',t,'macro'); self.article('b','Federal Reserve holds interest rates steady',t,'other')
        self.assertEqual(events.aggregate(self.db)['new_events'],2)
    def test_event_enrichment_checks_citations_and_asof(self):
        now=dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
        self.article('a','Federal Reserve holds interest rates steady',now.isoformat(),'macro')
        events.aggregate(self.db)
        event=events.as_of(self.db,now.isoformat(),'2000-01-01T00:00:00+08:00')[0]
        extraction={'events':[{'event_id':event['event_id'],'claims':[{'text':'Rates held steady','source_ids':['a']}],'impacts':[],'uncertainties':['not verified']}]}
        with patch.object(events.subprocess,'run') as run:
            run.return_value.stdout=json.dumps({'result':json.dumps(extraction)})
            self.assertEqual(events.enrich(self.db,{'news':{'active_event_days':7},'claude':{'command':'claude'}},now)['assessed'],1)
        old=events.as_of(self.db,now.isoformat(),'2000-01-01T00:00:00+08:00')
        self.assertIsNone(old[0]['assessment'])
        later=(now+dt.timedelta(seconds=2)).isoformat()
        self.assertEqual(events.as_of(self.db,later,'2000-01-01T00:00:00+08:00')[0]['assessment']['claims'][0]['source_ids'],['a'])
    def test_enrichment_subprocess_contract(self):
        now=dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
        self.article('a','Federal Reserve holds interest rates steady',now.isoformat(),'macro'); events.aggregate(self.db)
        script=Path(self.tmp.name)/'fake_claude'
        script.write_text("#!/usr/bin/env python3\nimport json,sys\ns=sys.stdin.read(); x=json.loads(s[s.index('\\n')+1:]); out={'events':[{'event_id':e['event_id'],'claims':[],'impacts':[],'uncertainties':['unverified']} for e in x]}; print(json.dumps({'result':json.dumps(out)}))\n")
        script.chmod(0o755)
        result=events.enrich(self.db,{'news':{'active_event_days':7},'claude':{'command':str(script)}},now)
        self.assertEqual(result['assessed'],1)
    def test_enrichment_rejects_unknown_evidence(self):
        now=dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
        self.article('a','Federal Reserve holds interest rates steady',now.isoformat(),'macro'); events.aggregate(self.db)
        event=events.as_of(self.db,now.isoformat(),'2000-01-01T00:00:00+08:00')[0]
        obj={'events':[{'event_id':event['event_id'],'claims':[{'text':'x','source_ids':['missing']}],'impacts':[],'uncertainties':[]}]}
        with patch.object(events.subprocess,'run') as run:
            run.return_value.stdout=json.dumps({'result':json.dumps(obj)})
            with self.assertRaises(ValueError): events.enrich(self.db,{'news':{'active_event_days':7},'claude':{'command':'claude'}},now)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event_assessments').fetchone()[0],0)
    def test_lhb_distinct_reason_and_fallback(self):
        rows=[dict(symbol='000530',name='测试',trade_date='2026-11-10',reason=x,buy=10,sell=7,net=3,amount=17) for x in ('原因一','原因二')]
        details=[dict(reason='原因一',side='买入',department='机构专用',buy=10,sell=7,net=3)]
        with patch.object(lhb,'from_akshare',side_effect=OSError('timeout')),patch.object(lhb,'from_web',return_value=rows),patch.object(lhb,'seats',return_value=details):
            result=lhb.collect(self.db,'20261110',['000530'])
        self.assertEqual(result['provider'],'web'); self.assertEqual(result['watchlist_records'],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM lhb_records').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM lhb_seats').fetchone()[0],1)
    def test_empty_not_asserted_as_no_disclosure(self):
        with patch.object(lhb,'from_akshare',return_value=[]),patch.object(lhb,'from_web',return_value=[]):
            with self.assertRaises(RuntimeError): lhb.collect(self.db,'20261110',['000530'])
    def test_malformed_disclosure_is_rejected(self):
        with patch.object(lhb,'from_web',return_value=[dict(symbol='000530',name='x',trade_date='2026-11-10',reason='',buy=0,sell=0,net=0,amount=0)]):
            with self.assertRaises(RuntimeError): lhb.collect(self.db,'20261110',['000530'],method='web')

if __name__=='__main__': unittest.main()
