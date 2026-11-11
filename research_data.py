#!/usr/bin/env python3
"""Agent-callable data operations; never starts an LLM or places orders."""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
import analysis
import data_collector
import storage
import refresh

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo('Asia/Shanghai')

def symbol_arg(value):
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError('stock symbol must be six digits')
    return value

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT / 'market_data.sqlite3'))
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('collect', help='fetch one stock, index, or board series')
    p.add_argument('--kind', required=True, choices=('stock', 'index', 'board'))
    p.add_argument('--symbol', required=True)
    p.add_argument('--timeframe', required=True, choices=('15', '60', 'daily'))
    p.add_argument('--board-type', choices=('industry', 'concept'))
    p = sub.add_parser('members', help='fetch one board constituent snapshot')
    p.add_argument('--symbol', required=True)
    p.add_argument('--board-type', required=True, choices=('industry', 'concept'))
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
    args = parser.parse_args(argv)
    if args.command == 'collect':
        if args.kind == 'stock': symbol_arg(args.symbol)
        if args.kind == 'board' and not args.board_type: parser.error('board requires --board-type')
        if args.kind != 'board' and args.board_type: parser.error('--board-type only applies to boards')
        if args.kind != 'stock' and args.timeframe == 'daily': parser.error('daily is currently supported only for stocks')
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    with storage.connect(args.db) as db:
        if args.command == 'refresh':
            value = refresh.refresh(db,cfg,args.symbol,include_news=args.news,lhb_date=args.lhb_date,include_hotspots=args.hotspots,force=args.force)
        elif args.command == 'hotspots':
            refresh.setup(db)
            value = refresh.rank_output(db,args.type,args.limit)
        elif args.command == 'collect':
            count = data_collector.collect(db, args.symbol, args.kind, args.timeframe, args.board_type)
            value = {'kind': args.kind, 'symbol': args.symbol, 'timeframe': args.timeframe, 'count': count}
        elif args.command == 'members':
            count = data_collector.collect_members(db, args.symbol, args.board_type)
            value = {'symbol': args.symbol, 'board_type': args.board_type, 'count': count}
        elif args.command == 'context':
            value = analysis.build(db, args.symbol, cfg, dt.datetime.now(TZ))
        else:
            now = dt.datetime.now(TZ).isoformat()
            rows = storage.bars_at(db, args.symbol, args.timeframe, now, args.limit)
            value = {'as_of': now, 'symbol': args.symbol, 'timeframe': args.timeframe, 'count': len(rows), 'bars': rows}
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    if args.command == 'refresh' and any(r['status']=='failed' for r in value['results']): return 1
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
