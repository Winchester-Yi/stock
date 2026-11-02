#!/usr/bin/env python3
"""Collect timestamped market snapshots and compare them with the previous snapshot."""
import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = ROOT / "market_data.sqlite3"
TZ = ZoneInfo("Asia/Shanghai")
SINA = "https://hq.sinajs.cn/list="
EAST = "https://push2.eastmoney.com/api/qt/stock/get?secid=90.BK0448&fields=f43,f44,f45,f46,f48,f57,f58,f60,f170,f124"
SYMBOLS = {
    "sz002185": "华天科技", "sh601086": "国芳集团", "sz000678": "襄阳轴承",
    "sh603228": "景旺电子", "sz002929": "润建股份", "sh603002": "宏昌电子",
    "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
  trade_date TEXT NOT NULL, quote_time TEXT, captured_at TEXT NOT NULL,
  price REAL NOT NULL, previous_close REAL NOT NULL, open REAL, high REAL, low REAL,
  turnover REAL, source TEXT NOT NULL, quality TEXT NOT NULL,
  UNIQUE(symbol, quote_time, source)
);
CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(symbol, trade_date, quote_time);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, subject TEXT NOT NULL,
  event_time TEXT, content TEXT NOT NULL, source_url TEXT NOT NULL,
  UNIQUE(subject, event_time, content, source_url)
);
"""


def connection():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def request(url, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode(encoding)


def valid_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def insert(db, row):
    keys = ("symbol", "name", "trade_date", "quote_time", "captured_at", "price", "previous_close", "open", "high", "low", "turnover", "source", "quality")
    db.execute("INSERT OR IGNORE INTO snapshots (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")", [row.get(k) for k in keys])


def collect_sina(db, captured):
    text = request(SINA + ",".join(SYMBOLS), "gb18030")
    found = set()
    for line in text.splitlines():
        if '="' not in line:
            continue
        symbol = line.split('="', 1)[0].removeprefix("var hq_str_")
        if symbol not in SYMBOLS:
            continue
        values = line.split('="', 1)[1].split('"', 1)[0].split(",")
        if len(values) < 32:
            print(f"数据不足: {symbol}", file=sys.stderr)
            continue
        try:
            quote_dt = dt.datetime.strptime(values[30] + " " + values[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except ValueError:
            print(f"行情时间无效: {symbol}", file=sys.stderr)
            continue
        price, prev = valid_number(values[3]), valid_number(values[2])
        if not price or not prev or price <= 0 or prev <= 0:
            print(f"行情价格无效: {symbol}", file=sys.stderr)
            continue
        age = (captured - quote_dt).total_seconds()
        quality = "fresh" if 0 <= age <= 120 else "stale_or_future"
        insert(db, dict(symbol=symbol, name=values[0], trade_date=values[30], quote_time=quote_dt.isoformat(), captured_at=captured.isoformat(), price=price, previous_close=prev, open=valid_number(values[1]), high=valid_number(values[4]), low=valid_number(values[5]), turnover=valid_number(values[9]), source="sina", quality=quality))
        found.add(symbol)
    for symbol in SYMBOLS.keys() - found:
        print(f"未获取有效行情: {symbol}", file=sys.stderr)
    return len(found)


def collect_sector(db, captured):
    data = json.loads(request(EAST)).get("data") or {}
    if data.get("f58") != "通信设备":
        raise ValueError("板块代码与名称不匹配")
    price, prev = valid_number(data.get("f43")), valid_number(data.get("f60"))
    if not price or not prev or price <= 0 or prev <= 0:
        raise ValueError("板块价格无效")
    timestamp = valid_number(data.get("f124"))
    quote = dt.datetime.fromtimestamp(timestamp, TZ) if timestamp and timestamp > 0 else None
    quality = "fresh" if quote and 0 <= (captured - quote).total_seconds() <= 120 else ("stale_or_future" if quote else "unverified_time")
    # A missing timestamp is never substituted with the capture time.
    insert(db, dict(symbol="BK0448", name="通信设备", trade_date=quote.date().isoformat() if quote else captured.date().isoformat(), quote_time=quote.isoformat() if quote else None, captured_at=captured.isoformat(), price=price / 100, previous_close=prev / 100, open=valid_number(data.get("f46")) / 100 if valid_number(data.get("f46")) is not None else None, high=valid_number(data.get("f44")) / 100 if valid_number(data.get("f44")) is not None else None, low=valid_number(data.get("f45")) / 100 if valid_number(data.get("f45")) is not None else None, turnover=valid_number(data.get("f48")), source="eastmoney", quality=quality))
    return quality


def report(db, day):
    rows = db.execute("SELECT * FROM snapshots WHERE trade_date=? ORDER BY captured_at DESC, id DESC", (day,)).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["symbol"], []).append(row)
    print(f"交易日期 {day} | 最近记录与同日上一条有效行情比较")
    print("名称 | 最新价 | 较昨收 | 较上一有效快照 | 行情时间 | 质量")
    for symbol in [*SYMBOLS, "BK0448"]:
        items = grouped.get(symbol, [])
        if not items:
            print(f"{SYMBOLS.get(symbol, '通信设备')} | 无数据")
            continue
        latest = items[0]
        prev = next((x for x in items[1:] if latest["quality"] == "fresh" and x["quality"] in ("fresh", "historical_reconstructed") and x["quote_time"] and x["quote_time"] < latest["quote_time"]), None)
        change = (latest["price"] / latest["previous_close"] - 1) * 100
        delta = f"{(latest['price'] / prev['price'] - 1) * 100:+.2f}% ({prev['quote_time'][11:19]}→{latest['quote_time'][11:19]})" if prev else "不可比较"
        print(f"{latest['name']} | {latest['price']:.2f} | {change:+.2f}% | {delta} | {latest['quote_time'] or '未提供'} | {latest['quality']}")
    print("提示：fresh 是采集时效检查，不代表目前仍实时；未提供时间的板块数据不可用于增量比较。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    p = sub.add_parser("report")
    p.add_argument("--date", default=dt.datetime.now(TZ).date().isoformat())
    sub.add_parser("seed", help="导入本次对话中记录的历史快照")
    args = parser.parse_args()
    with connection() as db:
        if args.command == "report":
            report(db, args.date)
            return
        if args.command == "seed":
            fixture = json.loads((ROOT / "initial_snapshot.json").read_text(encoding="utf-8"))
            for row in fixture["snapshots"]:
                insert(db, row)
            for row in fixture["observations"]:
                db.execute("INSERT OR IGNORE INTO observations (observed_at,subject,event_time,content,source_url) VALUES (?,?,?,?,?)", (row["observed_at"], row["subject"], row["event_time"], row["content"], row["source_url"]))
            print("历史数据已导入（重复导入会跳过）；采集时间为近似值，标记为 historical_reconstructed。")
            return
        now = dt.datetime.now(TZ)
        successes = 0
        try:
            successes = collect_sina(db, now)
            print(f"新浪有效行情: {successes}/{len(SYMBOLS)}")
        except Exception as exc:
            print(f"新浪采集失败: {exc}", file=sys.stderr)
        try:
            print(f"通信设备板块质量: {collect_sector(db, now)}")
        except Exception as exc:
            print(f"板块采集失败: {exc}", file=sys.stderr)
        print(f"采集时间: {now.isoformat()}；日韩指数需另行查证有时间戳的来源。")
        report(db, now.date().isoformat())
        if successes < len(SYMBOLS):
            db.commit()
            sys.exit(1)


if __name__ == "__main__":
    main()
