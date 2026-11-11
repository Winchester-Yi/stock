"""On-demand cache-aware acquisition, without scheduling or LLM calls."""
import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
import data_collector
import events
import lhb_collector
import market_calendar
import news_collector
import storage
import update_policy

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

# Retain the existing public names for callers and older tests.
last_boundary = update_policy.last_boundary
cache_valid = update_policy.cache_valid
acquire = update_policy.acquire

class BoardPageConflict(ValueError):
    """Conflicting observations of one code in a provider's paged list."""


def _stable_board_rows(board_type):
    """Same Eastmoney upstream, but paginate by stable board code, not returns."""
    params={'pz':'100','po':'0','np':'1','fltt':'2','invt':'2','fid':'f12',
            'fs':'m:90 t:'+('2' if board_type=='industry' else '3')+' f:!50',
            'fields':'f12,f14,f3'}
    out=[]; seen=set(); total=None
    for page in range(1,101):
        params['pn']=str(page)
        url='https://17.push2.eastmoney.com/api/qt/clist/get?'+urllib.parse.urlencode(params)
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})
        with urllib.request.urlopen(req,timeout=15) as response: payload=json.load(response)
        data=payload.get('data')
        if not isinstance(data,dict) or not isinstance(data.get('diff'),list): raise ValueError('invalid stable board page')
        expected=data.get('total')
        if not isinstance(expected,int) or isinstance(expected,bool) or expected<=0: raise ValueError('invalid board total')
        if total is not None and total!=expected: raise ValueError('board coverage changed during pagination')
        total=expected
        if not data['diff']: raise ValueError('incomplete stable board pages')
        for r in data['diff']:
            code=r.get('f12')
            if not isinstance(code,str) or not code.strip(): raise ValueError('board code missing')
            if code in seen: raise ValueError('duplicate code in stable board pages: '+code)
            seen.add(code)
            out.append({'板块代码':code,'板块名称':r.get('f14'),'涨跌幅':r.get('f3')})
        if len(out)==total: return out
        if len(out)>total: raise ValueError('board total mismatch')
    raise ValueError('board pagination limit exceeded')


def _parse_board_rows(rows):
    unique={}; duplicates=0
    for r in rows:
        code=str(r.get('板块代码','')).strip(); name=str(r.get('板块名称','')).strip()
        try: change=float(r.get('涨跌幅'))
        except (TypeError,ValueError): continue
        if not code or not name or code=='None' or name=='None' or not math.isfinite(change): continue
        row=(code,name,change,None)
        if code in unique:
            if unique[code] != row: raise BoardPageConflict('conflicting board rows across provider pages: '+code)
            duplicates+=1
        else: unique[code]=row
    if not unique: raise ValueError('board ranking returned no valid rows')
    return list(unique.values()),duplicates


def board_ranking(db, board_type):
    import akshare as ak
    fn=ak.stock_board_industry_name_em if board_type=='industry' else ak.stock_board_concept_name_em
    fallback=False
    try:
        parsed,duplicates=_parse_board_rows(fn().to_dict('records'))
    except BoardPageConflict:
        # Do not pick a winner from conflicting live-return-sorted pages.
        parsed,duplicates=_parse_board_rows(_stable_board_rows(board_type))
        fallback=True
    stamp=dt.datetime.now(TZ).isoformat()
    with db:
        db.executemany('INSERT INTO board_rank_snapshots VALUES (?,?,?,?,?,?)',
                       [(stamp,board_type,*r) for r in parsed])
    return {'count':len(parsed),'captured_at':stamp,'source':'eastmoney-web' if fallback else 'akshare/eastmoney',
            'ranking_status':'available' if len({r[2] for r in parsed}) > 1 else 'unusable',
            'duplicate_rows':duplicates,'pagination_fallback':fallback,
            'coverage':'provider board list; completeness and timeliness unverified',
            'amount_note':'turnover not supplied by this endpoint'}

def rank_output(db,board_type,limit,as_of=None):
    at=as_of if isinstance(as_of,dt.datetime) else dt.datetime.fromisoformat(as_of) if as_of else dt.datetime.now(TZ)
    if at.tzinfo is None: raise ValueError('as_of must include timezone')
    cutoff=at.astimezone(TZ).isoformat()
    stamps=[r[0] for r in db.execute('SELECT DISTINCT captured_at FROM board_rank_snapshots WHERE board_type=? AND captured_at<=? ORDER BY captured_at DESC LIMIT 2',(board_type,cutoff))]
    attempt=update_policy.state_at(db,'ranking:'+board_type,cutoff)
    error=attempt.get('last_error')
    if not stamps: return {'board_type':board_type,'status':'missing','last_error':error,'comparison_available':False,'boards':[]}
    def snapshot(stamp):
        return [dict(r) for r in db.execute('SELECT * FROM board_rank_snapshots WHERE board_type=? AND captured_at=? ORDER BY change_pct DESC,board_code',(board_type,stamp))]
    current=snapshot(stamps[0]); previous=snapshot(stamps[1]) if len(stamps)>1 else []
    usable=not error and len({r['change_pct'] for r in current}) > 1
    prior_usable=len({r['change_pct'] for r in previous}) > 1
    same_coverage={r['board_code'] for r in current} == {r['board_code'] for r in previous}
    comparable=usable and prior_usable and same_coverage
    def ranks(rows):
        # Competition ranks: tied changes share rank (1, 1, 3), never code order.
        by_change={}
        for i,r in enumerate(rows): by_change.setdefault(r['change_pct'],i+1)
        return {r['board_code']:by_change[r['change_pct']] for r in rows}
    current_ranks=ranks(current) if usable else {}
    previous_ranks=ranks(previous) if comparable else {}
    boards=[]
    for r in current[:limit]:
        rank=current_ranks.get(r['board_code']); old=previous_ranks.get(r['board_code'])
        boards.append({**r,'rank':rank,'prior_rank':old,
                       'rank_change':old-rank if old is not None and rank is not None else None})
    status='stale' if error else 'available' if usable else 'unusable'
    reason='最近采集失败，旧快照仅供参考' if error else None
    if not error and not usable:
        reason='快照涨跌幅没有横截面差异，无法识别强弱；不推断为盘前重置或接口异常'
    return {'board_type':board_type,'status':status,'last_error':error,'reason':reason,
            'captured_at':stamps[0],'previous_at':stamps[1] if len(stamps)>1 else None,
            'comparison_available':comparable,
            'comparison_note':'仅有效且板块代码集合一致的两次快照比较排名；并列同名次',
            'scope':'数据源板块榜单；覆盖和实时性未核实，不等于全市场个股','boards':boards}

def collect_bars(db,cfg,name,kind,timeframe,board_type=None,*,force=False,now=None):
    setup(db)
    key='bars:'+':'.join((kind,board_type or '-',name,timeframe))
    def fetch():
        count=data_collector.collect(db,name,kind,timeframe,board_type)
        bar_key=f'board:{board_type}:{name}' if kind=='board' else name
        bars=storage.bars_at(db,bar_key,timeframe,dt.datetime.now(TZ).isoformat(),1)
        return {'count':count,'latest_bar_end':bars[-1]['bar_end'] if bars else None,
                'captured_at':bars[-1]['captured_at'] if bars else None,
                'source':bars[-1]['source'] if bars else None}
    return acquire(db,key,now or dt.datetime.now(TZ),update_policy.seconds(cfg,timeframe,{'15':900,'60':3600,'daily':86400}[timeframe]),
                   fetch,timeframe,force,policy='daily' if timeframe=='daily' else 'completed_bar',
                   retry_seconds=update_policy.seconds(cfg,'retry_seconds',300,'update_policy'))


def collect_board_members(db,cfg,name,board_type,*,force=False,now=None):
    setup(db); check=now or dt.datetime.now(TZ)
    catalog_ttl=update_policy.seconds(cfg,'constituents',86400)
    last=db.execute('SELECT MAX(as_of) FROM sector_constituents WHERE board_id=? AND board_type=?',(name,board_type)).fetchone()[0]
    last_time=update_policy.timestamp(last)
    catalog_due=not last_time or last_time>check or (check-last_time).total_seconds()>=catalog_ttl
    def fetch():
        count=data_collector.collect_members(db,name,board_type,membership_ttl_seconds=catalog_ttl,force_membership=force)
        quote=db.execute('SELECT MAX(as_of) FROM sector_members WHERE board_id=? AND board_type=?',(name,board_type)).fetchone()[0]
        catalog=db.execute('SELECT MAX(as_of) FROM sector_constituents WHERE board_id=? AND board_type=?',(name,board_type)).fetchone()[0]
        return {'count':count,'snapshot_as_of':quote,'membership_as_of':catalog,
                'notice':'上游在一次响应中同时提供名单与报价；报价时间未单独核验'}
    result=acquire(db,f'breadth:{board_type}:{name}',check,update_policy.seconds(cfg,'breadth',300),fetch,
                   force=force,refresh_due=catalog_due,policy='session_snapshot',
                   retry_seconds=update_policy.seconds(cfg,'retry_seconds',300,'update_policy'))
    catalog=db.execute('SELECT MAX(as_of) FROM sector_constituents WHERE board_id=? AND board_type=?',(name,board_type)).fetchone()[0]
    state='updated' if catalog!=last else 'cached'
    if not catalog: state='missing'
    elif result['status'] in ('failed','deferred') and catalog_due: state='stale'
    return [result,{'key':f'constituents:{board_type}:{name}','status':state,
                    'policy':'ttl','last_success':catalog,'ttl_seconds':catalog_ttl}]


def collect_news(db,cfg,*,force=False,now=None):
    setup(db)
    def fetch():
        n,errors=news_collector.collect(db,cfg); grouped=events.aggregate(db)
        if errors: raise RuntimeError('部分新闻源失败: '+'; '.join(errors))
        return {'count':n,'aggregation':grouped}
    return acquire(db,update_policy.news_key(cfg['news']['queries']),now or dt.datetime.now(TZ),
                   update_policy.seconds(cfg,'news',1800),fetch,force=force,
                   retry_seconds=update_policy.seconds(cfg,'retry_seconds',300,'update_policy'))


def refresh(db,cfg,symbol,now=None,include_news=False,lhb_date=None,include_hotspots=False,force=False,clock=None):
    setup(db)
    if lhb_date: lhb_collector.date_arg(lhb_date)
    if clock is None: clock=lambda: now or dt.datetime.now(TZ)
    results=[]
    for tf in ('15','60','daily'):
        results.append(collect_bars(db,cfg,symbol,'stock',tf,force=force,now=clock()))
    for index in cfg.get('market_indices',[]):
        results.append(collect_bars(db,cfg,index,'index','15',force=force,now=clock()))
    for typ,names in cfg.get('sector_mapping',{}).get(symbol,{}).items():
        if typ not in ('industry','concept'): continue
        for name in ([names] if isinstance(names,str) else names or []):
            results.append(collect_bars(db,cfg,name,'board','15',typ,force=force,now=clock()))
            results.extend(collect_board_members(db,cfg,name,typ,force=force,now=clock()))
    if include_news: results.append(collect_news(db,cfg,force=force,now=clock()))
    if lhb_date:
        try:
            result=lhb_collector.collect(db,lhb_date,[symbol],force=force,now=clock(),config=cfg)
            results.append({'key':'lhb:'+lhb_date+':'+symbol,
                            'status':'failed' if result['seat_errors'] else result['status'],
                            'policy':'disclosure','count':result['watchlist_records'],'metadata':result,
                            'last_error':'; '.join(result['seat_errors']) or None})
        except lhb_collector.CollectionUnavailable as exc: results.append(exc.result)
        except Exception as exc:
            results.append({'key':'lhb:'+lhb_date+':'+symbol,'status':'failed','last_error':f'{type(exc).__name__}: {exc}'})
    if include_hotspots:
        for typ in ('industry','concept'):
            results.append(acquire(db,'ranking:'+typ,clock(),update_policy.seconds(cfg,'ranking',300),
                lambda typ=typ:board_ranking(db,typ),force=force,policy='session_snapshot',
                retry_seconds=update_policy.seconds(cfg,'retry_seconds',300,'update_policy')))
    completed=clock()
    state,verified=market_calendar.session_state(completed,'CN')
    return {'symbol':symbol,'as_of':completed.isoformat(),'session':state,'calendar_verified':verified,
            'policy_version':'lifecycle-v1',
            'notice':'按数据生命周期按需刷新；日历边界仅为工作日时刻表；缓存不等于实时；未自动调度',
            'results':results}
