"""Additive SQLite storage for stock research. Times are timezone-aware ISO strings."""
import json
import sqlite3
from pathlib import Path
import market_monitor

SCHEMA = '''
CREATE TABLE IF NOT EXISTS bars (symbol TEXT NOT NULL, timeframe TEXT NOT NULL, bar_end TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL, amount REAL, adjust TEXT NOT NULL, source TEXT NOT NULL, captured_at TEXT NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(symbol,timeframe,bar_end,adjust,source,captured_at));
CREATE INDEX IF NOT EXISTS bars_lookup ON bars(symbol,timeframe,bar_end,captured_at);
CREATE TABLE IF NOT EXISTS sector_members (as_of TEXT NOT NULL, board_id TEXT NOT NULL, board_type TEXT NOT NULL, symbol TEXT NOT NULL, change_pct REAL, PRIMARY KEY(as_of,board_id,board_type,symbol));
CREATE TABLE IF NOT EXISTS news_events (event_key TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT, published_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, status TEXT NOT NULL, url TEXT NOT NULL, source TEXT, summary TEXT, query TEXT);
CREATE TABLE IF NOT EXISTS analysis_runs (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, as_of TEXT NOT NULL, context_hash TEXT NOT NULL, context_json TEXT NOT NULL, decision_json TEXT NOT NULL, report_path TEXT, evaluated_json TEXT, created_at TEXT NOT NULL, UNIQUE(symbol,as_of,context_hash));
CREATE INDEX IF NOT EXISTS events_seen ON news_events(first_seen_at);
CREATE INDEX IF NOT EXISTS analysis_symbol ON analysis_runs(symbol,as_of);
CREATE TABLE IF NOT EXISTS lhb_records (symbol TEXT NOT NULL, trade_date TEXT NOT NULL, reason TEXT NOT NULL, name TEXT, buy REAL, sell REAL, net REAL, amount REAL, source TEXT NOT NULL, captured_at TEXT NOT NULL, PRIMARY KEY(symbol,trade_date,reason,source,captured_at));
CREATE TABLE IF NOT EXISTS lhb_seats (symbol TEXT NOT NULL, trade_date TEXT NOT NULL, reason TEXT NOT NULL, side TEXT NOT NULL, department TEXT NOT NULL, buy REAL, sell REAL, net REAL, captured_at TEXT NOT NULL, PRIMARY KEY(symbol,trade_date,reason,side,department,captured_at));
CREATE TABLE IF NOT EXISTS sector_constituents (as_of TEXT NOT NULL, board_id TEXT NOT NULL, board_type TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, PRIMARY KEY(as_of,board_id,board_type,symbol));
CREATE TABLE IF NOT EXISTS lhb_daily_batches (trade_date TEXT NOT NULL, captured_at TEXT NOT NULL, provider TEXT NOT NULL, row_count INTEGER NOT NULL, settled INTEGER NOT NULL, PRIMARY KEY(trade_date,captured_at,provider));
CREATE TABLE IF NOT EXISTS lhb_seat_batches (symbol TEXT NOT NULL, trade_date TEXT NOT NULL, captured_at TEXT NOT NULL, row_count INTEGER NOT NULL, settled INTEGER NOT NULL, PRIMARY KEY(symbol,trade_date,captured_at));
CREATE INDEX IF NOT EXISTS lhb_daily_snapshot ON lhb_records(trade_date,captured_at,source,symbol);
CREATE INDEX IF NOT EXISTS lhb_seat_snapshot ON lhb_seats(symbol,trade_date,captured_at);
CREATE INDEX IF NOT EXISTS sector_quote_snapshot ON sector_members(board_id,board_type,as_of);
CREATE INDEX IF NOT EXISTS sector_catalog_snapshot ON sector_constituents(board_id,board_type,as_of);
CREATE TABLE IF NOT EXISTS event_groups (event_id TEXT PRIMARY KEY, category TEXT NOT NULL, created_at TEXT NOT NULL, algorithm_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_articles (event_id TEXT NOT NULL, article_key TEXT NOT NULL UNIQUE, linked_at TEXT NOT NULL, PRIMARY KEY(event_id,article_key));
CREATE INDEX IF NOT EXISTS event_articles_asof ON event_articles(event_id,linked_at);
CREATE TABLE IF NOT EXISTS event_assessments (event_id TEXT NOT NULL, assessed_at TEXT NOT NULL, assessment_json TEXT NOT NULL, PRIMARY KEY(event_id,assessed_at));
'''

def connect(path, backup=False):
    path = Path(path)
    if backup and path.exists() and path.stat().st_size:
        with sqlite3.connect(str(path)) as src, sqlite3.connect(str(path) + '.bak') as dst:
            src.backup(dst)
    db = market_monitor.connection(path)
    db.executescript(SCHEMA)
    return db

def save_bars(db, rows):
    cols = ('symbol','timeframe','bar_end','open','high','low','close','volume','amount','adjust','source','captured_at','quality')
    db.executemany('INSERT OR IGNORE INTO bars ('+','.join(cols)+') VALUES ('+','.join('?' for _ in cols)+')', [[r.get(k) for k in cols] for r in rows])
    db.commit()

def bars_at(db, symbol, timeframe, as_of, limit=160):
    # Each revision has its own capture time; never leak future corrections.
    rows = db.execute('''SELECT * FROM bars b WHERE symbol=? AND timeframe=? AND adjust='' AND source IN ('akshare','eastmoney-web') AND quality='valid' AND bar_end<=? AND captured_at<=? AND captured_at=(SELECT MAX(captured_at) FROM bars x WHERE x.symbol=b.symbol AND x.timeframe=b.timeframe AND x.bar_end=b.bar_end AND x.adjust=b.adjust AND x.source=b.source AND x.captured_at<=?) ORDER BY bar_end DESC LIMIT ?''', (symbol,timeframe,as_of,as_of,as_of,limit)).fetchall()
    return [dict(x) for x in reversed(rows)]

def events_at(db, as_of, since, limit=30):
    return [dict(r) for r in db.execute('SELECT * FROM news_events WHERE first_seen_at<=? AND (published_at IS NULL OR published_at<=?) AND first_seen_at>=? ORDER BY first_seen_at DESC LIMIT ?', (as_of,as_of,since,limit))]

def previous(db, symbol, as_of):
    r = db.execute('SELECT decision_json FROM analysis_runs WHERE symbol=? AND as_of<? ORDER BY as_of DESC,id DESC LIMIT 1',(symbol,as_of)).fetchone()
    return json.loads(r[0]) if r else None
