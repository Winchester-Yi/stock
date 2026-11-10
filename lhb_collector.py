"""龙虎榜：AKShare first, Eastmoney web data endpoint fallback; both share upstream."""
import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

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

def collect(db,day,watchlist,method='auto',include_seats=True):
    date_arg(day); errors=[]; used=None; records=None
    for source in (('akshare','web') if method=='auto' else (method,)):
        try:
            records=(from_akshare if source=='akshare' else from_web)(day)
            if not records: raise ValueError('上榜列表为空，可能尚未披露，无法确认')
            for r in records:
                if len(r['symbol'])!=6 or not r['symbol'].isdigit() or r['trade_date']!=date_arg(day).isoformat() or not isinstance(r['reason'],str) or not r['reason'].strip(): raise ValueError('上榜记录缺少代码、日期或原因')
            used=source; break
        except Exception as exc: errors.append(source+': '+str(exc))
    if used is None: raise RuntimeError('; '.join(errors))
    now=dt.datetime.now(TZ).isoformat(); selected=[r for r in records if watchlist is None or r['symbol'] in watchlist]; seat_errors=[]; count=0
    with db:
        for r in selected:
            db.execute('INSERT OR IGNORE INTO lhb_records VALUES (?,?,?,?,?,?,?,?,?,?)',(r['symbol'],r['trade_date'],r['reason'],r['name'],r['buy'],r['sell'],r['net'],r['amount'],used,now))
        if include_seats:
            for symbol in sorted({r['symbol'] for r in selected}):
                try:
                    details=seats(symbol,day,used)
                    for d in details:
                        db.execute('INSERT OR IGNORE INTO lhb_seats VALUES (?,?,?,?,?,?,?,?,?)',(symbol,date_arg(day).isoformat(),d['reason'],d['side'],d['department'],d['buy'],d['sell'],d['net'],now)); count+=1
                except Exception as exc: seat_errors.append(symbol+': '+str(exc))
    return {'provider':used,'upstream':'eastmoney','total_records':len(records),'watchlist_records':len(selected),'seat_rows':count,'fallback_errors':errors,'seat_errors':seat_errors,'captured_at':now}
