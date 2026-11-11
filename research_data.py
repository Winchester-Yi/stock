#!/usr/bin/env python3
"""Agent-callable data operations; never starts an LLM or places orders."""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo
import analysis
import data_collector
import storage
import refresh
import price_basis
import research_batch
import agent_evidence

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Shanghai')

def symbol_arg(value):
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError('stock symbol must be six digits')
    return value

def lhb_date_arg(value):
    try:
        import lhb_collector
        lhb_collector.date_arg(value)
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def as_of_arg(value):
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.tzinfo is None: raise ValueError('timezone required')
        stamp = stamp.astimezone(TZ)
        if stamp > dt.datetime.now(TZ): raise ValueError('future cutoff is not allowed')
        return stamp
    except ValueError as exc:
        raise argparse.ArgumentTypeError('as-of must be an aware ISO timestamp, not in the future: '+str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'market_data.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('collect', help='fetch one stock, index, or board series')
    p.add_argument('--kind', required=True, choices=('stock', 'index', 'board'))
    p.add_argument('--symbol', required=True)
    p.add_argument('--timeframe', required=True, choices=('15', '60', 'daily'))
    p.add_argument('--board-type', choices=('industry', 'concept'))
    p.add_argument('--force', action='store_true', help='ignore update policy and retry cooldown')
    p = sub.add_parser('members', help='fetch one board constituent snapshot')
    p.add_argument('--symbol', required=True)
    p.add_argument('--board-type', required=True, choices=('industry', 'concept'))
    p.add_argument('--force', action='store_true', help='refresh membership and quote snapshots')
    p = sub.add_parser('context', help='calculate existing deterministic research indicators')
    p.add_argument('--symbol', type=symbol_arg, required=True)
    p = sub.add_parser('refresh', help='refresh only due stock, market, and configured board data')
    p.add_argument('--symbol', type=symbol_arg, required=True)
    p.add_argument('--news', action='store_true', help='also update configured news feeds')
    p.add_argument('--lhb-date', help='also collect a disclosed date, YYYYMMDD')
    p.add_argument('--hotspots', action='store_true', help='also collect board-level rankings')
    p.add_argument('--force', action='store_true', help='ignore cache')
    p = sub.add_parser('hotspots', help='read the latest captured board ranking')
    p.add_argument('--type', choices=('industry', 'concept'), default='industry')
    p.add_argument('--limit', type=int, choices=range(1,101), default=10, metavar='1..100')
    p = sub.add_parser('bars', help='return captured, completed raw bars')
    p.add_argument('--symbol', required=True, help='stock/index code or board:TYPE:NAME')
    p.add_argument('--timeframe', required=True, choices=('15', '60', 'daily'))
    p.add_argument('--limit', type=int, default=80, choices=range(1, 501), metavar='1..500')
    for name in ('context','bars','hotspots'):
        sub.choices[name].add_argument('--as-of', type=as_of_arg, help='available-at cutoff with timezone; no network')
    p = sub.add_parser('research', help='batch refresh and compact, time-bounded research evidence; never calls a model')
    p.add_argument('--symbols', nargs='+', type=symbol_arg, help='defaults to configured watchlist')
    p.add_argument('--as-of', type=as_of_arg, help='historical read only; automatically disables acquisition')
    p.add_argument('--no-refresh', action='store_true', help='read existing data without any provider calls')
    p.add_argument('--format', choices=('agent','json'), default='agent', help='qualified evidence brief (default) or machine snapshot JSON')
    p.add_argument('--pretty', action='store_true', help='indent JSON; implies --format json')
    p.add_argument('--snapshot-out', type=Path, help='also save this invocation snapshot JSON to a new file; never overwrites')
    p.add_argument('--news', action='store_true')
    p.add_argument('--hotspots', action='store_true', help='include board rankings (stored only with --as-of/--no-refresh)')
    p.add_argument('--lhb-date', type=lhb_date_arg)
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('basis-check', help='append operator-attested price-basis coverage; does not verify source automatically')
    p.add_argument('--symbol', required=True, type=symbol_arg)
    p.add_argument('--start', required=True)
    p.add_argument('--end', required=True)
    p.add_argument('--source-url', required=True)
    p.add_argument('--ex-date', action='append', default=[], help='all known effective ex-dates within the declared interval')
    p.add_argument('--confirm', required=True, action='store_true', help='attest the entire interval was checked against the cited source')
    args = parser.parse_args(argv)
    if args.command == 'research' and (args.as_of or args.no_refresh) and (args.force or args.news or args.lhb_date):
        parser.error('read-only research cannot request --force, --news or --lhb-date; stored news/LHB remain visible')
    if args.command == 'research' and args.snapshot_out:
        if args.snapshot_out.exists() or args.snapshot_out.is_symlink():
            parser.error('--snapshot-out must name a new file; existing targets are never overwritten')
        if not args.snapshot_out.parent.is_dir():
            parser.error('--snapshot-out parent directory must already exist')
    if args.command == 'collect':
        if args.kind == 'stock': symbol_arg(args.symbol)
        if args.kind == 'board' and not args.board_type: parser.error('board requires --board-type')
        if args.kind != 'board' and args.board_type: parser.error('--board-type only applies to boards')
        if args.kind != 'stock' and args.timeframe == 'daily': parser.error('daily is currently supported only for stocks')
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    with storage.connect(args.db) as db:
        if args.command == 'research':
            symbols = list(dict.fromkeys(args.symbols or cfg['watchlist']))
            for symbol in symbols: symbol_arg(symbol)
            refresh.setup(db)
            started = time.monotonic()
            updates = [] if args.as_of or args.no_refresh else research_batch.acquire(
                db,cfg,symbols,include_news=args.news,include_hotspots=args.hotspots,
                lhb_date=args.lhb_date,force=args.force)
            acquisition_seconds = time.monotonic()-started
            cutoff = args.as_of or dt.datetime.now(TZ)
            value = research_batch.snapshot(db,cfg,symbols,cutoff,acquisitions=updates,include_hotspots=args.hotspots)
            value['meta'].update(generated_at=dt.datetime.now(TZ).isoformat(),
                                 mode='historical' if args.as_of else 'cached' if args.no_refresh else 'refresh',
                                 acquisition_seconds=round(acquisition_seconds,3),
                                 total_seconds=round(time.monotonic()-started,3))
        elif args.command == 'basis-check':
            value = price_basis.record(db,args.symbol,args.start,args.end,args.ex_date,args.source_url,dt.datetime.now(TZ))
        elif args.command == 'refresh':
            value = refresh.refresh(db,cfg,args.symbol,include_news=args.news,lhb_date=args.lhb_date,include_hotspots=args.hotspots,force=args.force)
        elif args.command == 'hotspots':
            refresh.setup(db)
            value = refresh.rank_output(db,args.type,args.limit,as_of=args.as_of)
        elif args.command == 'collect':
            result = refresh.collect_bars(db,cfg,args.symbol,args.kind,args.timeframe,args.board_type,force=args.force)
            value = {'kind':args.kind,'symbol':args.symbol,'timeframe':args.timeframe,**result}
        elif args.command == 'members':
            results = refresh.collect_board_members(db,cfg,args.symbol,args.board_type,force=args.force)
            value = {'symbol':args.symbol,'board_type':args.board_type,'count':results[0].get('count'),'results':results}
        elif args.command == 'context':
            value = analysis.build(db, args.symbol, cfg, args.as_of or dt.datetime.now(TZ))
        else:
            now = (args.as_of or dt.datetime.now(TZ)).isoformat()
            rows = storage.bars_at(db, args.symbol, args.timeframe, now, args.limit)
            value = {'as_of': now, 'symbol': args.symbol, 'timeframe': args.timeframe, 'count': len(rows), 'bars': rows}
    if args.command == 'research':
        if args.snapshot_out:
            snapshot_json = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
            with args.snapshot_out.open('x', encoding='utf-8') as out:
                out.write(snapshot_json + '\n')
        if args.format == 'agent' and not args.pretty:
            brief = agent_evidence.build(value)
            print(brief.text, end='')
            return 1 if brief.status == 'incomplete' else 0
        print(json.dumps(value, ensure_ascii=False, indent=2 if args.pretty else None, allow_nan=False))
        return 1 if value['status']=='incomplete' else 0
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    updates=value.get('results',[value])
    if any(r.get('status') in ('failed','deferred') for r in updates): return 1
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
