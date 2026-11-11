"""On-demand cache-aware acquisition, without scheduling or LLM calls."""
import datetime as dt
import json
import math
from zoneinfo import ZoneInfo
import data_collector
import events
import lhb_collector
import market_calendar
import news_collector
import storage

TZ = ZoneInfo('Asia/Shanghai')

def setup(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS refresh_status (
      key TEXT PRIMARY KEY, last_success TEXT, last_attempt TEXT, last_error TEXT,
      last_count INTEGER, metadata TEXT);
    CREATE TABLE IF NOT EXISTS board_rank_snapshots (
      captured_at TEXT NOT NULL, board_type TEXT NOT NULL, board_code TEXT NOT NULL,
      board_name TEXT NOT NULL, change_pct REAL NOT NULL, amount REAL,
      PRIMARY KEY(captured_at,board_type,board_code));
    CREATE INDEX IF NOT EXISTS board_rank_lookup ON board_rank_snapshots(board_type,captured_at);''')

def last_boundary(now, timeframe):
    """Most recent scheduled end, never assert a weekday is an exchange trading day."""
    if now.weekday() >= 5:
        return None
    dates = {'15': [(9,45),(10,0),(10,15),(10,30),(10,45),(11,0),(11,15),(11,30),
                    (13,15),(13,30),(13,45),(14,0),(14,15),(14,30),(14,45),(15,0)],
             '60': [(10,30),(11,30),(14,0),(15,0)], 'daily': [(15,0)]}
    ends = [now.replace(hour=h,minute=m,second=0,microsecond=0) for h,m in dates[timeframe]]
    return max((x for x in ends if x <= now),default=None)

def cache_valid(success, now, ttl, timeframe=None):
    if not success: return False
    try: stamp=dt.datetime.fromisoformat(success).astimezone(TZ)
    except (ValueError,TypeError): return False
    age=(now-stamp).total_seconds()
    if age < 0: return False
    if timeframe:
        boundary=last_boundary(now,timeframe)
        if stamp.date()!=now.date() or (boundary and stamp < boundary): return False
        session,_=market_calendar.session_state(now,'CN')
        # Once a completed interval has been fetched, lunch/after-close polling
        # need not refetch the same scheduled bar. This does not certify a holiday.
        if boundary and session in ('lunch_break','closed') and age < 43200:
            return True
    return age < ttl

def acquire(db,key,now,ttl,fn,timeframe=None,force=False):
    row=db.execute('SELECT * FROM refresh_status WHERE key=?',(key,)).fetchone()
    previous=dict(row) if row else {}
    if not force and cache_valid(previous.get('last_success'),now,ttl,timeframe):
        # A recent failure is not erased just because older data remains cached.
        return {'key':key,'status':'cached','last_success':previous['last_success'],
                'last_attempt':previous.get('last_attempt'),'last_error':previous.get('last_error'),
                'count':previous.get('last_count'),'metadata':json.loads(previous.get('metadata') or '{}')}
    try:
        metadata=fn()
        if isinstance(metadata,int): metadata={'count':metadata}
        if not isinstance(metadata,dict): raise ValueError('invalid collector result')
        completed=dt.datetime.now(TZ).isoformat()
        count=metadata.get('count')
        db.execute('''INSERT INTO refresh_status VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
          last_success=excluded.last_success,last_attempt=excluded.last_attempt,last_error=NULL,
          last_count=excluded.last_count,metadata=excluded.metadata''',
          (key,completed,completed,None,count,json.dumps(metadata,ensure_ascii=False,allow_nan=False)))
        db.commit()
        return {'key':key,'status':'updated','last_success':completed,'last_attempt':completed,'count':count,'metadata':metadata}
    except Exception as exc:
        completed=dt.datetime.now(TZ).isoformat(); error=f'{type(exc).__name__}: {exc}'
        db.execute('''INSERT INTO refresh_status(key,last_attempt,last_error) VALUES (?,?,?)
          ON CONFLICT(key) DO UPDATE SET last_attempt=excluded.last_attempt,last_error=excluded.last_error''',
          (key,completed,error)); db.commit()
        return {'key':key,'status':'failed','last_success':previous.get('last_success'),
                'last_attempt':completed,'last_error':error,'stale_available':bool(previous.get('last_success'))}

def board_ranking(db, board_type):
    import akshare as ak
    fn=ak.stock_board_industry_name_em if board_type=='industry' else ak.stock_board_concept_name_em
    rows=fn().to_dict('records'); parsed=[]
    for r in rows:
        code=str(r.get('板块代码','')).strip(); name=str(r.get('板块名称','')).strip()
        try: change=float(r.get('涨跌幅'))
        except (TypeError,ValueError): continue
        if not code or not name or code=='None' or name=='None' or not math.isfinite(change): continue
        parsed.append((code,name,change,None))
    if not parsed: raise ValueError('board ranking returned no valid rows')
    stamp=dt.datetime.now(TZ).isoformat()
    with db:
        db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                       [(stamp,board_type,*r) for r in parsed])
    return {'count':len(parsed),'captured_at':stamp,'source':'akshare/eastmoney',
            'coverage':'provider board list; completeness and timeliness unverified',
            'amount_note':'turnover not supplied by this endpoint'}

def rank_output(db,board_type,limit):
    stamps=[r[0] for r in db.execute('SELECT DISTINCT captured_at FROM board_rank_snapshots WHERE board_type=? ORDER BY captured_at DESC LIMIT 2',(board_type,))]
    if not stamps: return {'board_type':board_type,'status':'missing','boards':[]}
    def snapshot(stamp):
        return [dict(r) for r in db.execute('SELECT * FROM board_rank_snapshots WHERE board_type=? AND captured_at=? ORDER BY change_pct DESC,board_code',(board_type,stamp))]
    current=snapshot(stamps[0]); previous=snapshot(stamps[1]) if len(stamps)>1 else []
    prev={r['board_code']:i+1 for i,r in enumerate(previous)}
    boards=[]
    for i,r in enumerate(current[:limit]):
        old=prev.get(r['board_code'])
        boards.append({**r,'rank':i+1,'prior_rank':old,'rank_change':old-(i+1) if old else None})
    return {'board_type':board_type,'status':'available','captured_at':stamps[0],
            'previous_at':stamps[1] if len(stamps)>1 else None,
            'scope':'数据源板块榜单；覆盖和实时性未核实，不等于全市场个股','boards':boards}

def refresh(db,cfg,symbol,now=None,include_news=False,lhb_date=None,include_hotspots=False,force=False):
    setup(db); now=now or dt.datetime.now(TZ); results=[]
    state,verified=market_calendar.session_state(now,'CN')
    ttls=cfg.get('refresh_ttl_seconds',{})
    def ttl(name,default):
        value=ttls.get(name,default)
        if not isinstance(value,int) or isinstance(value,bool) or not 1 <= value <= 604800: raise ValueError('invalid TTL: '+name)
        return value
    def collect(kind,name,tf,typ=None):
        key='bars:'+':'.join((kind,typ or '-',name,tf))
        # Bounded cache avoids repeated requests but never proves market freshness.
        age=ttl(tf,{'15':900,'60':3600,'daily':86400}[tf])
        def fetch():
            count=data_collector.collect(db,name,kind,tf,typ)
            bar_key=f'board:{typ}:{name}' if kind=='board' else name
            bars=storage.bars_at(db,bar_key,tf,dt.datetime.now(TZ).isoformat(),1)
            return {'count':count,'latest_bar_end':bars[-1]['bar_end'] if bars else None,
                    'captured_at':bars[-1]['captured_at'] if bars else None,
                    'source':bars[-1]['source'] if bars else None}
        results.append(acquire(db,key,now,age,fetch,tf,force))
    for tf in ('15','60','daily'): collect('stock',symbol,tf)
    for index in cfg.get('market_indices',[]): collect('index',index,'15')
    for typ,names in cfg.get('sector_mapping',{}).get(symbol,{}).items():
        if typ not in ('industry','concept'): continue
        for name in ([names] if isinstance(names,str) else names or []):
            collect('board',name,'15',typ)
            results.append(acquire(db,f'members:{typ}:{name}',now,ttl('members',86400),
                lambda name=name,typ=typ:data_collector.collect_members(db,name,typ),force=force))
    if include_news:
        def news():
            n,errors=news_collector.collect(db,cfg); grouped=events.aggregate(db)
            if errors: raise RuntimeError('部分新闻源失败: '+'; '.join(errors))
            return {'count':n,'aggregation':grouped}
        results.append(acquire(db,'news',now,ttl('news',1800),news,force=force))
    if lhb_date:
        def lhb():
            result=lhb_collector.collect(db,lhb_date,[symbol])
            if result['seat_errors']: raise RuntimeError('席位采集部分失败: '+'; '.join(result['seat_errors']))
            return {'count':result['watchlist_records'],**result}
        results.append(acquire(db,'lhb:'+lhb_date+':'+symbol,now,ttl('lhb',86400),lhb,force=force))
    if include_hotspots:
        for typ in ('industry','concept'):
            results.append(acquire(db,'ranking:'+typ,now,ttl('ranking',300),lambda typ=typ:board_ranking(db,typ),force=force))
    return {'symbol':symbol,'as_of':dt.datetime.now(TZ).isoformat(),'session':state,
            'calendar_verified':verified,'notice':'工作日不保证开市；缓存命中不等于行情实时；未自动调度',
            'results':results}
