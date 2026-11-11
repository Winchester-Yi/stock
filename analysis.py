"""Deterministic descriptive features; these are not validated trading signals."""
import datetime as dt
import statistics
import storage
import events
import lhb_collector
import update_policy

def features(bars):
    if len(bars)<3: return {'status':'insufficient','count':len(bars)}
    close=[b['close'] for b in bars]; last=bars[-1]
    pivots=[]
    for i in range(1,len(bars)-1):
        a,b,c=bars[i-1:i+2]
        if b['high']>a['high'] and b['high']>c['high'] and b['low']>a['low'] and b['low']>c['low']:
            pivots.append({'type':'high','price':b['high'],'at':b['bar_end'],'confirmed_at':c['bar_end']})
        elif b['low']<a['low'] and b['low']<c['low'] and b['high']<a['high'] and b['high']<c['high']:
            pivots.append({'type':'low','price':b['low'],'at':b['bar_end'],'confirmed_at':c['bar_end']})
    # Window definitions are explicit; insufficient samples remain null.
    returns={str(n): (close[-1]/close[-n-1]-1)*100 if len(close)>n else None for n in (1,4,20)}
    moving={}
    for n in (5,20):
        if len(close)>=n+1:
            avg=statistics.mean(close[-n:]); previous=statistics.mean(close[-n-1:-1])
            moving[str(n)]={'value':avg,'distance_pct':(close[-1]/avg-1)*100,
                            'slope_pct':(avg/previous-1)*100}
        else: moving[str(n)]=None
    recent=bars[-20:]
    amplitude=(max(b['high'] for b in recent)/min(b['low'] for b in recent)-1)*100 if len(recent)==20 else None
    prior=bars[-21:-1]; high=max(b['high'] for b in prior); low=min(b['low'] for b in prior)
    same=[b['amount'] for b in bars[:-1] if b['bar_end'][11:16]==last['bar_end'][11:16] and b['amount'] is not None and b['amount']>0]
    baseline=statistics.median(same) if same else None
    return {'status':'ok','count':len(bars),'last_bar_end':last['bar_end'],'close':last['close'],'return_last_bar_pct':(close[-1]/close[-2]-1)*100,'trend_simple': 'up' if close[-1]>close[-min(6,len(close))] else 'down' if close[-1]<close[-min(6,len(close))] else 'flat','range':{'high':high,'low':low,'lookback_bars':len(prior),'breakout_candidate': 'up' if close[-1]>high else 'down' if close[-1]<low else None},'local_pivots':pivots[-8:],'pivot_method':'three-bar strict; containment excluded; not full Chan','same_time_amount_ratio':last['amount']/baseline if baseline and last['amount'] is not None else None,'same_time_baseline_samples':len(same),'returns_pct':returns,'moving_averages':moving,'range_amplitude_20_pct':amplitude}

def build(db, symbol, config, as_of):
    stamp=as_of.isoformat(); stock={tf:features(storage.bars_at(db,symbol,tf,stamp,config['history'].get(tf,160))) for tf in ('15','60','daily')}
    boards=[]
    for typ,name in config.get('sector_mapping',{}).get(symbol,{}).items():
        for board in ([name] if isinstance(name,str) else name or []):
            key='board:'+typ+':'+board
            f=features(storage.bars_at(db,key,'15',stamp))
            m=db.execute('SELECT MAX(as_of) FROM sector_members WHERE board_id=? AND board_type=? AND as_of<=?',(board,typ,stamp)).fetchone()[0]
            members=db.execute('SELECT * FROM sector_members WHERE board_id=? AND board_type=? AND as_of=?',(board,typ,m)).fetchall() if m else []
            changes=[r['change_pct'] for r in members if r['change_pct'] is not None]
            sf=stock['15']; relative=None
            sb=storage.bars_at(db,symbol,'15',stamp,2); bb=storage.bars_at(db,key,'15',stamp,2)
            if len(sb)==len(bb)==2 and [x['bar_end'] for x in sb]==[x['bar_end'] for x in bb]: relative=(sb[-1]['close']/sb[-2]['close']-bb[-1]['close']/bb[-2]['close'])*100
            age=(as_of-dt.datetime.fromisoformat(m)).total_seconds() if m else None
            limit=update_policy.seconds(config,'breadth',300)
            last=db.execute("SELECT last_error,last_attempt FROM refresh_status WHERE key=?",(f'breadth:{typ}:{board}',)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refresh_status'").fetchone() else None
            error=last['last_error'] if last and last['last_attempt'] and last['last_attempt']<=stamp else None
            usable=bool(m and changes and not error and dt.datetime.fromisoformat(m).date()==as_of.date() and age<=limit)
            catalog=db.execute('SELECT MAX(as_of) FROM sector_constituents WHERE board_id=? AND board_type=? AND as_of<=?',(board,typ,stamp)).fetchone()[0]
            membership_count=db.execute('SELECT count(*) FROM sector_constituents WHERE board_id=? AND board_type=? AND as_of=?',(board,typ,catalog)).fetchone()[0] if catalog else 0
            boards.append({'name':board,'type':typ,'features':f,'relative_return_pp':relative,
                           'breadth':sum(x>0 for x in changes)/len(changes) if changes else None,
                           'breadth_as_of':m,'valid_members':len(changes),'breadth_age_seconds':age,
                           'breadth_status':'requires_quote_time_check' if usable else 'historical_or_missing',
                           'breadth_usable_for_current_session':usable,'breadth_last_error':error,
                           'membership_as_of':catalog,'membership_count':membership_count,
                           'breadth_notice':'采集时间不是行情源报价时间；不得假定与K线结束时间同步'})
    indices={s:features(storage.bars_at(db,s,'15',stamp)) for s in config.get('market_indices',[])}
    for name, f in indices.items():
        sb=storage.bars_at(db,symbol,'15',stamp,2); ib=storage.bars_at(db,name,'15',stamp,2)
        f['stock_relative_return_pp']=(sb[-1]['close']/sb[-2]['close']-ib[-1]['close']/ib[-2]['close'])*100 if len(sb)==len(ib)==2 and [x['bar_end'] for x in sb]==[x['bar_end'] for x in ib] else None
    stock_name=config.get('stock_names',{}).get(symbol)
    recent=events.as_of(db,stamp,(as_of-dt.timedelta(days=config['news']['active_event_days'])).isoformat(),
                       symbol=symbol,aliases=(stock_name,) if stock_name else ())
    warnings=[]
    if not config.get('sector_mapping',{}).get(symbol): warnings.append('板块映射未配置，无法判断板块相对收益与广度共振')
    if not stock_name: warnings.append('未配置股票名称，个股新闻仅按代码匹配，可能漏报')
    for board in boards:
        if not board['breadth_usable_for_current_session']: warnings.append(board['name']+'广度为历史快照或缺失，不用于当前盘中共振判断')
    longhubang=lhb_collector.context(db,symbol,as_of,config.get('update_policy',{}).get('lhb_context_days',30))
    return {'meta':{'as_of':stamp,'symbol':symbol,'warnings':warnings,'data_quality':'insufficient' if stock['15']['status']!='ok' else 'requires_freshness_check','algorithm_version':'simple-v1','notice':'未复权；跨除权日结构不可直接比较；收盘集合竞价包含于最后一根K线'},'stock':stock,'market':indices,'sectors':boards,'events':recent,'longhubang':longhubang,'previous_decision':storage.previous(db,symbol,stamp),'execution':{'position':'unknown','sellable_quantity':None,'price_limit':'unknown','liquidity':'unknown','auto_trade':False}}
