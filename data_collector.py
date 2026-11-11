"""AKShare adapter. Endpoint contracts are validated; no partial bar is persisted."""
import datetime as dt
import math
import json
import urllib.parse
import urllib.request
import urllib.error
import time
from zoneinfo import ZoneInfo
import storage

TZ = ZoneInfo('Asia/Shanghai')

def num(value, optional=False):
    if value is None or str(value).strip() in ('', '-', '--', 'nan', 'None'):
        if optional: return None
        raise ValueError('missing numeric field')
    value = float(value)
    if not math.isfinite(value): raise ValueError('non-finite numeric field')
    return value

def end_time(value, timeframe):
    t = dt.datetime.fromisoformat(str(value)).replace(tzinfo=TZ)
    if timeframe == 'daily': return t.replace(hour=15,minute=0,second=0,microsecond=0)
    # Eastmoney minute bars are end-labelled; reject timestamps outside expected session boundaries.
    minute = t.hour*60+t.minute
    valid = {15: set(range(585,691,15)) | set(range(795,901,15)), 60: {630,690,840,900}}[int(timeframe)]
    if minute not in valid or t.second or t.weekday() >= 5: raise ValueError('unexpected bar end/session')
    return t

def normalize(frame, symbol, timeframe, captured):
    out=[]
    for row in frame.to_dict('records'):
        end=end_time(row.get('时间', row.get('日期时间', row.get('日期'))), timeframe)
        if end > captured: continue
        opening, high, low, close = [num(row.get(k)) for k in ('开盘','最高','最低','收盘')]
        vol=num(row.get('成交量'),True); amount=num(row.get('成交额'),True)
        if min(opening,high,low,close)<=0 or not low<=min(opening,close)<=max(opening,close)<=high or (vol is not None and vol<0) or (amount is not None and amount<0): raise ValueError('invalid OHLCV')
        # AKShare Eastmoney stock/index volume is lots; board volume units vary and are not assumed.
        if vol is not None and len(symbol)==6 and symbol.isdigit(): vol *= 100
        else: vol=None
        out.append(dict(symbol=symbol,timeframe=timeframe,bar_end=end.isoformat(),open=opening,high=high,low=low,close=close,volume=vol,amount=amount,adjust='',source='akshare',captured_at=captured.isoformat(),quality='valid'))
    return out

def collect(db, symbol, kind, timeframe, board_type=None):
    import akshare as ak
    start='1979-09-01 09:30:00'; end='2222-01-01 15:00:00'
    if kind=='stock' and timeframe=='daily':
        frame=ak.stock_zh_a_hist(symbol=symbol, period='daily', start_date='19790901', end_date=dt.datetime.now(TZ).strftime('%Y%m%d'), adjust='')
    elif kind=='stock':
        frame=ak.stock_zh_a_hist_min_em(symbol=symbol, start_date=start, end_date=end, period=timeframe, adjust='')
    elif kind=='index':
        # AKShare's index symbol discovery traverses the whole market; the exchange
        # prefix here is explicit, so the public web endpoint needs no discovery.
        if not (symbol.startswith(('sh','sz')) and len(symbol)==8 and symbol[2:].isdigit()): raise ValueError('index requires sh/sz prefix and six digits')
        params={'secid':('1.' if symbol.startswith('sh') else '0.')+symbol[2:],'fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61','klt':timeframe,'fqt':'0','beg':'0','end':'20500000'}
        url='https://push2his.eastmoney.com/api/qt/stock/kline/get?'+urllib.parse.urlencode(params)
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req,timeout=15) as response: payload=json.load(response)
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt==2: raise
                time.sleep(0.5*(attempt+1))
        data=payload.get('data')
        if not isinstance(data,dict) or str(data.get('code'))!=symbol[2:] or not isinstance(data.get('klines'),list): raise ValueError('index data missing or code mismatch')
        import pandas as pd
        parsed=[]
        for item in data['klines']:
            cells=item.split(',')
            if len(cells)<7: raise ValueError('index bar has insufficient fields')
            parsed.append(dict(zip(('时间','开盘','收盘','最高','最低','成交量','成交额'),cells[:7])))
        frame=pd.DataFrame(parsed)
    else:
        fn = ak.stock_board_industry_hist_min_em if board_type=='industry' else ak.stock_board_concept_hist_min_em
        frame=fn(symbol=symbol, period=timeframe)
    captured=dt.datetime.now(TZ)
    key = 'board:'+board_type+':'+symbol if kind=='board' else symbol
    rows=normalize(frame,key,timeframe,captured)
    if kind=='index':
        for row in rows: row['source']='eastmoney-web'
    if not rows: raise ValueError('no completed valid bars returned')
    storage.save_bars(db,rows)
    return len(rows)

def collect_members(db, board, board_type, *, membership_ttl_seconds=86400, force_membership=False):
    """Provider bundles names and quotes; persist their different lifecycles separately."""
    import akshare as ak
    fn=ak.stock_board_industry_cons_em if board_type=='industry' else ak.stock_board_concept_cons_em
    frame=fn(symbol=board); values={}
    for r in frame.to_dict('records'):
        code=str(r.get('代码','')).zfill(6)
        if len(code)!=6 or not code.isascii() or not code.isdigit(): continue
        value=(str(r.get('名称') or '').strip() or None,num(r.get('涨跌幅'),True))
        if code in values and values[code]!=value: raise ValueError('conflicting constituent rows: '+code)
        values[code]=value
    if not values: raise ValueError('empty sector constituents')
    now=dt.datetime.now(TZ); stamp=now.isoformat()
    last=db.execute('SELECT MAX(as_of) FROM sector_constituents WHERE board_id=? AND board_type=?',(board,board_type)).fetchone()[0]
    previous={r['symbol']:r['name'] for r in db.execute('SELECT symbol,name FROM sector_constituents WHERE board_id=? AND board_type=? AND as_of=?',(board,board_type,last))} if last else {}
    changed=previous!={code:v[0] for code,v in values.items()}
    due=force_membership or not last or changed or dt.datetime.fromisoformat(last)>now or (now-dt.datetime.fromisoformat(last)).total_seconds()>=membership_ttl_seconds
    with db:
        if due:
            db.executemany('INSERT OR IGNORE INTO sector_constituents VALUES (?,?,?,?,?)',
                           [(stamp,board,board_type,code,v[0]) for code,v in values.items()])
        db.executemany('INSERT OR IGNORE INTO sector_members VALUES (?,?,?,?,?)',
                       [(stamp,board,board_type,code,v[1]) for code,v in values.items()])
    return len(values)
