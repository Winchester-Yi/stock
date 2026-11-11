"""龙虎榜：AKShare first, Eastmoney web data endpoint fallback; both share upstream."""
import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
import update_policy

TZ=ZoneInfo('Asia/Shanghai')
API='https://datacenter-web.eastmoney.com/api/data/v1/get'

def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError): return None

def date_arg(value):
    return dt.datetime.strptime(value,'%Y%m%d').date()

def web_query(report,filter_,sort='SECURITY_CODE',page_size=500):
    rows=[]; page=1
    while True:
        q={'reportName':report,'columns':'ALL','filter':filter_,'pageNumber':page,'pageSize':page_size,'sortTypes':'-1','sortColumns':sort,'source':'WEB','client':'WEB'}
        req=urllib.request.Request(API+'?'+urllib.parse.urlencode(q),headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'})
        with urllib.request.urlopen(req,timeout=15) as response: payload=json.load(response)
        result=payload.get('result')
        if not payload.get('success',True) or not isinstance(result,dict) or not isinstance(result.get('data'),list): raise ValueError('龙虎榜数据源返回空或无效响应，不能判定为无上榜')
        rows.extend(result['data']); pages=int(result.get('pages') or 1)
        if page>=pages: return rows
        if page>=100: raise ValueError('龙虎榜分页超限')
        page+=1

def from_web(day):
    iso=date_arg(day).isoformat()
    raw=web_query('RPT_DAILYBILLBOARD_DETAILSNEW',f"(TRADE_DATE='{iso}')",'SECURITY_CODE',500)
    return [{'symbol':str(r.get('SECURITY_CODE','')).zfill(6),'name':r.get('SECURITY_NAME_ABBR'),'trade_date':str(r.get('TRADE_DATE',''))[:10],'reason':r.get('EXPLANATION'),'net':number(r.get('BILLBOARD_NET_AMT')),'buy':number(r.get('BILLBOARD_BUY_AMT')),'sell':number(r.get('BILLBOARD_SELL_AMT')),'amount':number(r.get('BILLBOARD_DEAL_AMT'))} for r in raw]

def from_akshare(day):
    import akshare as ak
    frame=ak.stock_lhb_detail_em(start_date=day,end_date=day)
    return [{'symbol':str(r.get('代码','')).zfill(6),'name':r.get('名称'),'trade_date':str(r.get('上榜日',''))[:10],'reason':r.get('上榜原因'),'net':number(r.get('龙虎榜净买额')),'buy':number(r.get('龙虎榜买入额')),'sell':number(r.get('龙虎榜卖出额')),'amount':number(r.get('龙虎榜成交额'))} for r in frame.to_dict('records')]

def seats(symbol,day,method):
    if not (len(symbol)==6 and symbol.isdigit()): raise ValueError('invalid symbol')
    iso=date_arg(day).isoformat(); rows=[]
    for flag in ('买入','卖出'):
        if method=='akshare':
            import akshare as ak
            raw=ak.stock_lhb_stock_detail_em(symbol=symbol,date=day,flag=flag).to_dict('records')
            for r in raw: rows.append({'side':flag,'department':r.get('交易营业部名称'),'reason':r.get('类型'),'buy':number(r.get('买入金额')),'sell':number(r.get('卖出金额')),'net':number(r.get('净额'))})
        else:
            side='BUY' if flag=='买入' else 'SELL'
            raw=web_query('RPT_BILLBOARD_DAILYDETAILS'+side,f'''(TRADE_DATE='{iso}')(SECURITY_CODE="{symbol}")''',side)
            for r in raw: rows.append({'side':flag,'department':r.get('OPERATEDEPT_NAME'),'reason':r.get('EXPLANATION'),'buy':number(r.get('BUY')),'sell':number(r.get('SELL')),'net':number(r.get('NET'))})
    for r in rows:
        if not isinstance(r['department'],str) or not r['department'].strip() or not isinstance(r['reason'],str) or not r['reason'].strip(): raise ValueError('龙虎榜席位缺少部门或披露类型')
    return rows

class CollectionUnavailable(RuntimeError):
    def __init__(self, result):
        super().__init__(result.get('last_error') or result['status'])
        self.result=result


def collect(db,day,watchlist,method='auto',include_seats=True,*,force=False,force_seats=False,now=None,config=None):
    """One dated full list is shared by all stocks; seats have separate state."""
    if method not in ('auto','akshare','web'): raise ValueError('invalid disclosure source')
    date=date_arg(day); current=now or dt.datetime.now(TZ)
    if current.tzinfo is None: raise ValueError('disclosure check time must include timezone')
    current=current.astimezone(TZ)
    cfg=(config or {}).get('update_policy',{})
    retry=update_policy.seconds(config or {},'retry_seconds',300,'update_policy')
    provisional=update_policy.seconds(config or {},'disclosure_recheck_seconds',1800,'update_policy')
    check_time=dt.time.fromisoformat(cfg.get('lhb_check_after','16:30'))
    if check_time.tzinfo is not None: raise ValueError('lhb_check_after must be a Shanghai local clock time')
    base={'upstream':'eastmoney','total_records':0,'watchlist_records':0,'seat_rows':0,
          'fallback_errors':[],'seat_errors':[],'seat_cache':[],'captured_at':None}
    if date>current.date() or (date==current.date() and current.time().replace(tzinfo=None)<check_time):
        return {**base,'status':'pending','list_cache':{'status':'pending'},
                'notice':'尚未到配置的盘后查询窗口；不是对实际披露时间的确认'}
    update_policy.setup(db)
    key='lhb:list:'+day+(':'+method if method!='auto' else '')
    def fetch_list():
        errors=[]; used=None
        for source in (('akshare','web') if method=='auto' else (method,)):
            try:
                records=(from_akshare if source=='akshare' else from_web)(day)
                if not records: raise ValueError('上榜列表为空，可能尚未披露，无法确认')
                unique={}
                for r in records:
                    if len(r['symbol'])!=6 or not r['symbol'].isdigit() or r['trade_date']!=date.isoformat() or not isinstance(r['reason'],str) or not r['reason'].strip():
                        raise ValueError('上榜记录缺少代码、日期或原因')
                    ident=(r['symbol'],r['reason'])
                    if ident in unique and unique[ident]!=r: raise ValueError('conflicting disclosure rows')
                    unique[ident]=r
                records=list(unique.values()); used=source; break
            except Exception as exc: errors.append(source+': '+str(exc))
        if used is None: raise RuntimeError('; '.join(errors))
        completed=dt.datetime.now(TZ); stamp=completed.isoformat(); settled=date<completed.date()
        with db:
            for r in records:
                db.execute('INSERT OR IGNORE INTO lhb_records VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (r['symbol'],r['trade_date'],r['reason'],r['name'],r['buy'],r['sell'],r['net'],r['amount'],used,stamp))
            db.execute('INSERT INTO lhb_daily_batches VALUES (?,?,?,?,?)',(date.isoformat(),stamp,used,len(records),int(settled)))
        return {'count':len(records),'provider':used,'captured_at':stamp,'settled':settled,'fallback_errors':errors}
    listing=update_policy.acquire(db,key,current,provisional,fetch_list,force=force,
                                  policy='disclosure',retry_seconds=retry)
    if listing['status'] in ('failed','deferred'): raise CollectionUnavailable(listing)
    meta=listing['metadata']; used=meta['provider']; stamp=meta['captured_at']
    records=[dict(r) for r in db.execute('SELECT * FROM lhb_records WHERE trade_date=? AND source=? AND captured_at=?',
                                        (date.isoformat(),used,stamp))]
    if len(records)!=meta['count']: raise RuntimeError('cached disclosure rows missing; use --force to repair')
    selected=[r for r in records if watchlist is None or r['symbol'] in watchlist]
    caches=[]; errors=[]; seat_count=0
    if include_seats:
        for symbol in sorted({r['symbol'] for r in selected}):
            def fetch_seats(symbol=symbol):
                details=seats(symbol,day,used)
                if not details: raise ValueError('empty seats for listed stock; completeness unknown')
                completed=dt.datetime.now(TZ); captured=completed.isoformat(); settled=date<completed.date()
                with db:
                    for d in details:
                        db.execute('INSERT OR IGNORE INTO lhb_seats VALUES (?,?,?,?,?,?,?,?,?)',
                                   (symbol,date.isoformat(),d['reason'],d['side'],d['department'],d['buy'],d['sell'],d['net'],captured))
                    db.execute('INSERT INTO lhb_seat_batches VALUES (?,?,?,?,?)',(symbol,date.isoformat(),captured,len(details),int(settled)))
                return {'count':len(details),'captured_at':captured,'settled':settled,'provider':used}
            last_seat=db.execute('SELECT MAX(captured_at) FROM lhb_seat_batches WHERE symbol=? AND trade_date=?',(symbol,date.isoformat())).fetchone()[0]
            seat=update_policy.acquire(db,'lhb:seats:'+day+':'+symbol,now or dt.datetime.now(TZ),provisional,
                                       fetch_seats,force=force or force_seats,policy='disclosure',retry_seconds=retry,
                                       refresh_due=not last_seat or last_seat<stamp)
            caches.append(seat)
            if seat['status'] in ('failed','deferred'): errors.append(symbol+': '+seat['last_error'])
            else: seat_count+=seat['count'] or 0
    return {**base,'status':'partial' if errors else listing['status'],'provider':used,
            'total_records':len(records),'watchlist_records':len(selected),'seat_rows':seat_count,
            'captured_at':stamp,'settled':meta['settled'],'fallback_errors':meta['fallback_errors'],
            'seat_errors':errors,'list_cache':listing,'seat_cache':caches,
            'notice':'按交易日共享已采集列表；完整性仍受数据源限制；当日暂定、次日复核后持久复用'}


def context(db,symbol,as_of,lookback_days=30):
    """Read whole captured revisions, including negative matches, without lookahead."""
    if not isinstance(lookback_days,int) or isinstance(lookback_days,bool) or not 1<=lookback_days<=3650: raise ValueError('invalid lhb_context_days')
    stamp=as_of.isoformat(); since=(as_of.date()-dt.timedelta(days=lookback_days)).isoformat()
    batches=db.execute('''SELECT * FROM lhb_daily_batches b WHERE trade_date BETWEEN ? AND ?
        AND captured_at<=? AND captured_at=(SELECT MAX(captured_at) FROM lhb_daily_batches x
        WHERE x.trade_date=b.trade_date AND x.captured_at<=?) ORDER BY trade_date DESC''',
        (since,as_of.date().isoformat(),stamp,stamp)).fetchall()
    records=[]; coverage=[]
    for b in batches:
        rows=[dict(r) for r in db.execute('SELECT * FROM lhb_records WHERE symbol=? AND trade_date=? AND captured_at=? AND source=?',
                                         (symbol,b['trade_date'],b['captured_at'],b['provider']))]
        records.extend(rows)
        coverage.append({**dict(b),'target_records':len(rows),'status':'matched' if rows else 'not_in_collected_list'})
    legacy=[dict(r) for r in db.execute('''SELECT * FROM lhb_records b WHERE symbol=? AND trade_date BETWEEN ? AND ?
        AND captured_at<=? AND NOT EXISTS (SELECT 1 FROM lhb_daily_batches h WHERE h.trade_date=b.trade_date AND h.captured_at<=?)
        AND captured_at=(SELECT MAX(captured_at) FROM lhb_records x WHERE x.symbol=b.symbol AND x.trade_date=b.trade_date
        AND x.reason=b.reason AND x.captured_at<=?) ORDER BY trade_date DESC''',
        (symbol,since,as_of.date().isoformat(),stamp,stamp,stamp))]
    records=sorted(records+legacy,key=lambda r:(r['trade_date'],r['captured_at']),reverse=True)[:20]
    seats_out=[dict(r) for r in db.execute('''SELECT * FROM lhb_seats b WHERE symbol=? AND trade_date BETWEEN ? AND ?
        AND captured_at<=? AND (captured_at=(SELECT MAX(captured_at) FROM lhb_seat_batches h
        WHERE h.symbol=b.symbol AND h.trade_date=b.trade_date AND h.captured_at<=?)
        OR (NOT EXISTS (SELECT 1 FROM lhb_seat_batches h WHERE h.symbol=b.symbol AND h.trade_date=b.trade_date AND h.captured_at<=?)
        AND captured_at=(SELECT MAX(captured_at) FROM lhb_seats x WHERE x.symbol=b.symbol AND x.trade_date=b.trade_date
        AND x.reason=b.reason AND x.side=b.side AND x.department=b.department AND x.captured_at<=?))) ORDER BY trade_date DESC LIMIT 100''',
        (symbol,since,as_of.date().isoformat(),stamp,stamp,stamp,stamp))]
    known_dates={r['trade_date'] for r in records}
    batch_times={b['trade_date']:b['captured_at'] for b in batches}
    # Seat type labels need not equal the full-list reason text. Compare revision times instead.
    seats_out=[r for r in seats_out if r['trade_date'] in known_dates
               and r['captured_at']>=batch_times.get(r['trade_date'],'')]
    latest=max((r['trade_date'] for r in records),default=None)
    issues=[]
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refresh_status'").fetchone():
        for row in db.execute("SELECT key,last_attempt,last_error FROM refresh_status WHERE last_error IS NOT NULL AND last_attempt<=?",(stamp,)):
            parts=row['key'].split(':')
            if len(parts)>=3 and parts[0]=='lhb' and (parts[1]=='list' or (parts[1]=='seats' and parts[-1]==symbol)):
                try: relevant=since<=date_arg(parts[2]).isoformat()<=as_of.date().isoformat()
                except ValueError: relevant=False
                if relevant: issues.append(dict(row))
    status='collection_error' if issues else 'available_background' if records else 'not_in_collected_list' if coverage else 'not_collected'
    return {'records':records,'seats':seats_out,'coverage':coverage,'role':'post_close_background','status':status,
            'refresh_issues':issues,'legacy_records_present':bool(legacy),
            'latest_checked_trade_date':coverage[0]['trade_date'] if coverage else None,
            'latest_trade_date':latest,'age_calendar_days':(as_of.date()-dt.date.fromisoformat(latest)).days if latest else None,
            'lookback_calendar_days':lookback_days,'official_published_at':None,
            'notice':'盘后披露的历史背景，不是实时资金流；日期年龄为自然日；未单独核验官方披露时间或交易日历'}
