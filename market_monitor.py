#!/usr/bin/env python3
"""Timestamp-aware market snapshots and deterministic incremental context (Python 3.9+)."""
import argparse
import datetime as dt
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from market_calendar import ZONES, session_state, trading_minutes_between

ROOT = Path(__file__).resolve().parent
DB = ROOT / "market_data.sqlite3"
TZ = ZoneInfo("Asia/Shanghai")
SINA = "https://hq.sinajs.cn/list="
EAST = "https://push2.eastmoney.com/api/qt/stock/get"
SYMBOLS = {
    "sz002185": "华天科技", "sh601086": "国芳集团", "sz000678": "襄阳轴承",
    "sh603228": "景旺电子", "sz002929": "润建股份", "sh603002": "宏昌电子",
    "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
}
INDICES = ("sh000001", "sz399001", "sz399006")
SECTOR = "BK0448"
OVERSEAS = {"N225": ("日经225", "JP", "100.N225"), "KS11": ("KOSPI", "KR", "100.KS11")}
ALL_SYMBOLS = {**SYMBOLS, SECTOR: "通信设备", **{k: v[0] for k, v in OVERSEAS.items()}}
COMPATIBLE_QUALITY = ("fresh", "historical_reconstructed")
FIELDS = "f43,f44,f45,f46,f48,f57,f58,f60,f170,f124,f86"
KEYS = ("symbol", "name", "trade_date", "quote_time", "captured_at", "price", "previous_close", "open", "high", "low", "turnover", "source", "quality")

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
CREATE TABLE IF NOT EXISTS fetch_runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
 statuses_json TEXT NOT NULL, outcome TEXT
);
"""


def connection(path=None):
    db = sqlite3.connect(str(path or DB))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # Existing checked-in databases were created before the outcome field.
    if "outcome" not in {row[1] for row in db.execute("PRAGMA table_info(fetch_runs)")}:
        db.execute("ALTER TABLE fetch_runs ADD COLUMN outcome TEXT")
    return db


def request(url, encoding="utf-8", retries=1):
    referrer = "https://finance.sina.com.cn/" if "sinajs" in url else "https://quote.eastmoney.com/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referrer})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=6) as response:
                return response.read().decode(encoding)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries:
                raise
            time.sleep(0.25 * (attempt + 1))


def valid_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def optional_number(value, label):
    """Distinguish vendor missing-value sentinels from malformed numeric fields."""
    if value is None or (isinstance(value, str) and value.strip() in ("", "-", "--")):
        return None
    number = valid_number(value)
    if number is None:
        raise ValueError(f"{label}不是有限数字")
    return number


def validate_prices(price, previous_close, high=None, low=None, open_price=None, turnover=None):
    """Reject contradictory OHLC and negative cumulative turnover, not just bad closes."""
    if price is None or previous_close is None or price <= 0 or previous_close <= 0:
        raise ValueError("缺少正数价格或昨收")
    for key, value in (("开盘价", open_price), ("最高价", high), ("最低价", low), ("累计成交额", turnover)):
        if value is not None and value < 0:
            raise ValueError(f"{key}为负数")
    # Zero-valued OHLC fields can be pre-open placeholders; do not interpret them
    # as real intraday extrema or reject them merely because the market is closed.
    if high is not None and low is not None and high > 0 and low > 0:
        if low > high:
            raise ValueError("最高价低于最低价")
        tolerance = max(1e-7, price * 1e-7)
        if not (low - tolerance <= price <= high + tolerance):
            raise ValueError("现价超出日内高低范围")
        if open_price is not None and open_price > 0 and not (low - tolerance <= open_price <= high + tolerance):
            raise ValueError("开盘价超出日内高低范围")


def quote_quality(quote, received, market="CN", verified_timestamp=True):
    if quote is None or not verified_timestamp:
        return "unverified_time"
    age = (received - quote).total_seconds()
    if age < -5:
        return "stale_or_future"
    state, _ = session_state(received, market)
    if state != "trading" and age > 120:
        return "session_paused"
    return "fresh" if age <= 120 else "stale_or_future"


def insert(db, row):
    db.execute("INSERT OR IGNORE INTO snapshots (" + ",".join(KEYS) + ") VALUES (" + ",".join("?" for _ in KEYS) + ")", [row.get(k) for k in KEYS])


def collect_sina(db):
    raw = request(SINA + ",".join(SYMBOLS), "gb18030")
    received = dt.datetime.now(TZ)  # Record time AFTER the network call.
    statuses = {key: "missing" for key in SYMBOLS}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        prefix, payload = line.split('="', 1)
        symbol = prefix.removeprefix("var hq_str_")
        if symbol not in SYMBOLS:
            continue
        try:
            values = payload.split('"', 1)[0].split(",")
            if len(values) < 32:
                raise ValueError("字段不足")
            quote = dt.datetime.strptime(values[30] + " " + values[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            price, previous_close = (optional_number(values[i], name) for i, name in ((3, "现价"), (2, "昨收")))
            opening, high, low = (optional_number(values[i], name) for i, name in ((1, "开盘价"), (4, "最高价"), (5, "最低价")))
            turnover = optional_number(values[9], "成交额")
            validate_prices(price, previous_close, high, low, opening, turnover)
            quality = quote_quality(quote, received)
            insert(db, dict(symbol=symbol, name=values[0] or SYMBOLS[symbol], trade_date=quote.date().isoformat(),
                            quote_time=quote.isoformat(), captured_at=received.isoformat(), price=price,
                            previous_close=previous_close, open=opening or None, high=high or None, low=low or None,
                            turnover=turnover, source="sina", quality=quality))
            statuses[symbol] = quality
        except (ValueError, IndexError, OverflowError) as exc:
            statuses[symbol] = "invalid: " + str(exc)
    return statuses


def east_data(secid):
    raw = request(EAST + "?secid=" + secid + "&fields=" + FIELDS)
    result = json.loads(raw).get("data")
    if not isinstance(result, dict):
        raise ValueError("东财未返回数据")
    return result, dt.datetime.now(TZ)


def provider_timestamp(data, market):
    # f124/f86 are vendor-supplied timestamps. An overseas quote cannot be
    # exchange-time-verified merely because f124 happens to be positive.
    unix = valid_number(data.get("f124"))
    if unix and unix > 0:
        try:
            return dt.datetime.fromtimestamp(unix, ZONES[market]), market == "CN"
        except (ValueError, OverflowError, OSError):
            pass
    f86 = str(data.get("f86") or "")
    if len(f86) in (12, 14) and f86.isdigit():
        fmt = "%Y%m%d%H%M%S" if len(f86) == 14 else "%Y%m%d%H%M"
        try:
            return dt.datetime.strptime(f86, fmt).replace(tzinfo=ZONES[market]), False
        except ValueError:
            pass
    return None, False


def collect_east(db, symbol, secid, market, expected_code=None, expected_name=None):
    data, received = east_data(secid)
    if expected_code and str(data.get("f57")) != expected_code:
        raise ValueError("东财代码不匹配")
    if expected_name and data.get("f58") != expected_name:
        raise ValueError("东财名称不匹配")
    price, prev = (optional_number(data.get(k), k) for k in ("f43", "f60"))
    high, low = (optional_number(data.get(k), k) for k in ("f44", "f45"))
    opening, turnover = (optional_number(data.get(k), k) for k in ("f46", "f48"))
    validate_prices(price, prev, high, low, opening, turnover)
    if market != "CN":
        # f124 is NOT an exchange-verified tick and the numeric f43/f60 scale
        # is NOT validated for overseas secids. Never persist an invented /100
        # price in snapshots or expose it to Claude as a tradable quote.
        return "unverified_contract"
    # BK0448's known feed contract uses hundredths; it is distinct from the
    # unverified, instrument-specific overseas feed contract.
    scale = 100
    quote, verified = provider_timestamp(data, market)
    quality = quote_quality(quote, received, market, verified)
    name = ALL_SYMBOLS[symbol]
    local_now = received.astimezone(ZONES[market])
    insert(db, dict(symbol=symbol, name=name, trade_date=quote.date().isoformat() if quote else local_now.date().isoformat(),
                    quote_time=quote.isoformat() if quote else None, captured_at=received.isoformat(),
                    price=price / scale, previous_close=prev / scale,
                    open=opening / scale if opening else None,
                    high=high / scale if high is not None else None,
                    low=low / scale if low is not None else None,
                    turnover=turnover, source="eastmoney", quality=quality))
    return quality


def select_snapshots(db, day):
    """Prefer a reliable quote by quote_time; never let an unverified read displace it."""
    rows = db.execute("SELECT * FROM snapshots WHERE trade_date=? ORDER BY id DESC", (day,)).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["symbol"], []).append(row)
    result = {}
    for symbol, items in grouped.items():
        reliable = sorted((x for x in items if x["quote_time"] and x["quality"] in COMPATIBLE_QUALITY),
                          key=lambda x: (x["quote_time"], x["id"]), reverse=True)
        latest = reliable[0] if reliable else max(items, key=lambda x: (x["quote_time"] or "", x["id"]))
        prev = next((x for x in reliable[1:] if x["quote_time"] < latest["quote_time"] and
                     x["source"] == latest["source"]), None) if latest["quality"] in COMPATIBLE_QUALITY else None
        result[symbol] = (latest, prev)
    return result


def _float(value, precision=4):
    return round(value, precision) if value is not None and math.isfinite(value) else None


def calculated(latest, previous, market):
    price, close = latest["price"], latest["previous_close"]
    result = {"pct_vs_close": _float(100 * (price / close - 1)), "interval_pct": None,
              "interval_minutes": None, "turnover_delta": None, "turnover_per_min": None,
              "price_position": None}
    high, low = latest["high"], latest["low"]
    if high is not None and low is not None and high > low:
        result["price_position"] = _float((price - low) / (high - low))
    if previous is None:
        return result
    begin, end = dt.datetime.fromisoformat(previous["quote_time"]), dt.datetime.fromisoformat(latest["quote_time"])
    elapsed = (end - begin).total_seconds() / 60
    if elapsed <= 0:
        return result
    result["interval_pct"] = _float(100 * (price / previous["price"] - 1))
    result["interval_minutes"] = _float(elapsed, 2)
    traded = trading_minutes_between(begin, end, market)
    if latest["turnover"] is not None and previous["turnover"] is not None and latest["turnover"] >= previous["turnover"]:
        result["turnover_delta"] = _float(latest["turnover"] - previous["turnover"], 2)
        if traded > 0:
            result["turnover_per_min"] = _float(result["turnover_delta"] / traded, 2)
    return result


def context(db, day, as_of=None):
    as_of = as_of or dt.datetime.now(TZ)
    selected = select_snapshots(db, day)
    items = {}
    for symbol, name in ALL_SYMBOLS.items():
        market = OVERSEAS[symbol][1] if symbol in OVERSEAS else "CN"
        state, calendar_verified = session_state(as_of, market)
        base = {"symbol": symbol, "name": name, "market": market, "market_state": state,
                "calendar_verified": calendar_verified, "quote": None, "metrics": None}
        if symbol in selected:
            latest, previous = selected[symbol]
            if symbol in OVERSEAS:
                # Existing DBs may contain earlier unverified /100 overseas
                # rows. Quarantine their numeric values without deleting data.
                base["quote"] = {"price": None, "previous_close": None,
                                 "quote_time": latest["quote_time"], "captured_at": latest["captured_at"],
                                 "source": latest["source"], "quality_at_capture": "unverified_contract",
                                 "current": False, "reference_only": True,
                                 "timestamp_quality": "vendor_unverified", "price_scale_verified": False,
                                 "source_reliability": "unverified"}
                base["availability_reason"] = "未核实海外行情时间及价格精度，历史数值已隔离"
                items[symbol] = base
                continue
            quote_at = dt.datetime.fromisoformat(latest["quote_time"]) if latest["quote_time"] else None
            age = (as_of - quote_at).total_seconds() if quote_at else None
            age = max(0, age) if age is not None else None
            current = (state == "trading" and latest["quality"] == "fresh" and
                       age is not None and age <= 120 and age >= 0)
            base["quote"] = {"price": latest["price"], "previous_close": latest["previous_close"],
                             "quote_time": latest["quote_time"], "captured_at": latest["captured_at"],
                             "source": latest["source"], "quality_at_capture": latest["quality"],
                             "age_seconds": _float(age, 1), "current": current,
                             "reference_only": not current,
                             "timestamp_quality": "source_timestamp" if quote_at else "unverified",
                             "price_scale_verified": True, "source_reliability": "single_source"}
            base["metrics"] = calculated(latest, previous, market)
            # Keep valid historical interval calculations even after the quote
            # expires. They are historical evidence, NOT a live prediction.
            base["metrics"]["usable_for_live_inference"] = current
        items[symbol] = base
    # A historical report must not display today's fetch status as if it belonged to that day.
    last = db.execute("SELECT * FROM fetch_runs WHERE substr(started_at,1,10)=? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
    # Relative strength is computed only when both index and stock have aligned windows.
    for symbol in SYMBOLS:
        if symbol in INDICES or symbol not in selected:
            continue
        bench = "sh000001" if symbol.startswith("sh") else "sz399001"
        metric = items[symbol]["metrics"]
        metric["relative_strength_pp"] = None
        metric["benchmark"] = bench
        if bench not in selected:
            continue
        stock_latest, stock_prev = selected[symbol]
        index_latest, index_prev = selected[bench]
        if stock_prev is None or index_prev is None:
            continue
        start_gap = abs((dt.datetime.fromisoformat(stock_prev["quote_time"]) - dt.datetime.fromisoformat(index_prev["quote_time"])).total_seconds())
        end_gap = abs((dt.datetime.fromisoformat(stock_latest["quote_time"]) - dt.datetime.fromisoformat(index_latest["quote_time"])).total_seconds())
        if start_gap <= 120 and end_gap <= 120:
            sm, im = metric["interval_pct"], items[bench]["metrics"]["interval_pct"]
            if sm is not None and im is not None:
                metric["relative_strength_pp"] = _float(sm - im)
    fetch_run = (dict(started_at=last["started_at"], ended_at=last["ended_at"],
                      outcome=last["outcome"] or "legacy_unknown",
                      statuses=json.loads(last["statuses_json"])) if last else None)
    return {"date": day, "as_of": as_of.isoformat(), "timezone": "Asia/Shanghai",
            "calendar_warning": "工作日仅按常规交易时段推定；未接入完整节假日/临停日历",
            "market": [items[s] for s in INDICES], "overseas": [items[s] for s in OVERSEAS],
            "watchlist": [items[s] for s in SYMBOLS if s not in INDICES] + [items[SECTOR]],
            "fetch_run": fetch_run,
            "source_note": "海外东财 f124/f86 与价格精度未独立核实：不保存海外猜测点位；旧海外记录的数值已隔离。历史有效区间指标可供回看，但不得冒充实时结论。"}


def report(db, day):
    output = context(db, day)
    print(f"交易日期 {day} | 数据截至 {output['as_of']} | 本程序只计算指标，不生成研判")
    print("名称 | 最新报价(参考) | 较昨收 | 区间变化 | 区间成交额(亿元) | 行情时间 | 状态")
    for item in output["market"] + output["overseas"] + output["watchlist"]:
        quote, metric = item["quote"], item["metrics"]
        if not quote:
            print(f"{item['name']} | 无数据 | — | — | — | — | {item['market_state']}")
            continue
        if metric is None:
            print(f"{item['name']} | 未核实数值 | — | — | — | {quote['quote_time'] or '未验证'} | 仅参考，不可用于研判")
            continue
        fmt = lambda x: "不可比较" if x is None else f"{x:+.2f}%"
        amount = metric["turnover_delta"]
        amount_str = f"{amount / 1e8:.2f}" if amount is not None else "不可比较"
        state = "当前有效" if quote["current"] else "参考/过期"
        print(f"{item['name']} | {quote['price']:.2f} | {fmt(metric['pct_vs_close'])} | "
              f"{fmt(metric['interval_pct'])} | {amount_str} | {quote['quote_time'] or '未验证'} | "
              f"{item['market_state']} / {quote['quality_at_capture']} / {state}")
    print("注意：交易日历未完整校验；成交额为同源、同日的累计值差；海外数据仅作参考，需独立核对。")


def seed(db):
    fixture = json.loads((ROOT / "initial_snapshot.json").read_text(encoding="utf-8"))
    for row in fixture["snapshots"]:
        insert(db, row)
    for row in fixture["observations"]:
        db.execute("INSERT OR IGNORE INTO observations (observed_at,subject,event_time,content,source_url) VALUES (?,?,?,?,?)",
                   (row["observed_at"], row["subject"], row["event_time"], row["content"], row["source_url"]))
    print("初始化历史数据已导入（幂等）。")


def assess_fetch(statuses, market_state):
    """Return (outcome, exit_code); an attempted failed source never succeeds."""
    attempted = {key: status for key, status in statuses.items() if status != "not_attempted"}
    if not attempted:
        return "failed", 2
    good_states = {"fresh", "session_paused", "stale_or_future", "unverified_time",
                   "unverified_contract"}
    failed = [key for key, status in attempted.items() if status not in good_states]
    if len(failed) == len(attempted):
        return "failed", 2
    if failed:
        return "degraded", 1
    if market_state != "trading":
        return "market_closed", 0
    if any(statuses[symbol] != "fresh" for symbol in SYMBOLS):
        return "degraded", 1
    if statuses[SECTOR] != "fresh":
        return "degraded", 1
    # A reported overseas quote with unknown units/time cannot be called a
    # complete market analysis, even if the HTTP request itself succeeded.
    if any(statuses[s] != "not_attempted" and statuses[s] != "fresh" for s in OVERSEAS):
        return "degraded", 1
    return "success", 0


def fetch(db, overseas=True):
    started = dt.datetime.now(TZ)
    statuses = {symbol: "not_attempted" for symbol in ALL_SYMBOLS}
    try:
        statuses.update(collect_sina(db))
    except Exception as exc:
        for symbol in SYMBOLS:
            statuses[symbol] = "error: " + str(exc)
    try:
        statuses[SECTOR] = collect_east(db, SECTOR, "90.BK0448", "CN", expected_code=SECTOR, expected_name="通信设备")
    except Exception as exc:
        statuses[SECTOR] = "error: " + str(exc)
    if overseas:
        for symbol, (_, market, secid) in OVERSEAS.items():
            try:
                statuses[symbol] = collect_east(db, symbol, secid, market, expected_code=symbol)
            except Exception as exc:
                statuses[symbol] = "error: " + str(exc)
    ended = dt.datetime.now(TZ)
    current, _ = session_state(ended, "CN")
    outcome, exit_code = assess_fetch(statuses, current)
    db.execute("INSERT INTO fetch_runs(started_at,ended_at,statuses_json,outcome) VALUES(?,?,?,?)",
               (started.isoformat(), ended.isoformat(), json.dumps(statuses, ensure_ascii=False), outcome))
    db.commit()
    fresh = sum(statuses[s] == "fresh" for s in SYMBOLS)
    print(f"行情采集完成 | A股 fresh={fresh}/{len(SYMBOLS)} | 市场={current} | 采集结果={outcome} | 时刻={ended.isoformat()}")
    for symbol, status in statuses.items():
        if status not in ("fresh", "not_attempted"):
            print(f"{ALL_SYMBOLS[symbol]}: {status}", file=sys.stderr)
    report(db, ended.date().isoformat())
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--no-overseas", action="store_true", help="跳过日韩尝试性采集")
    for command in ("report", "context"):
        p = sub.add_parser(command)
        p.add_argument("--date", default=dt.datetime.now(TZ).date().isoformat())
        if command == "context":
            p.add_argument("--json", action="store_true", help="输出结构化 JSON（默认也为 JSON）")
    sub.add_parser("seed")
    args = parser.parse_args()
    with connection() as db:
        if args.command == "seed":
            seed(db)
        elif args.command == "fetch":
            return fetch(db, overseas=not args.no_overseas)
        elif args.command == "report":
            report(db, args.date)
        else:
            print(json.dumps(context(db, args.date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
