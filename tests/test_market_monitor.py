import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import market_monitor as mm
from market_calendar import session_state, trading_minutes_between

CST = ZoneInfo("Asia/Shanghai")


def at(clock):
    return dt.datetime.fromisoformat("2026-11-02T" + clock + "+08:00")


class MarketCalendarTests(unittest.TestCase):
    def test_japan_lunch_and_korea_trading(self):
        self.assertEqual(session_state(at("11:21:00"), "JP")[0], "lunch_break")
        self.assertEqual(session_state(at("11:21:00"), "KR")[0], "trading")
        self.assertEqual(session_state(at("14:31:00"), "JP")[0], "closed")

    def test_trading_minutes_excludes_lunch(self):
        self.assertAlmostEqual(trading_minutes_between(at("11:20:00"), at("13:10:00"), "CN"), 20)

    def test_weekdays_not_verified_holiday_calendar(self):
        state, verified = session_state(at("09:45:00"), "CN")
        self.assertEqual(state, "trading")
        self.assertFalse(verified)
        self.assertEqual(session_state(at("09:45:00"), "CN", {"2026-11-02"})[0], "holiday")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = mm.connection(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def row(self, symbol, clock, captured, quality="fresh", price=100, turnover=1000, source="sina"):
        quote = at(clock) if clock else None
        mm.insert(self.db, dict(symbol=symbol, name="测试", trade_date="2026-11-02",
                quote_time=quote.isoformat() if quote else None, captured_at=at(captured).isoformat(),
                price=price, previous_close=90, open=99, high=120, low=80,
                turnover=turnover, source=source, quality=quality))

    def test_expired_quote_cannot_override_latest_valid(self):
        self.row("sh000001", "10:00:00", "10:00:01", price=100)
        self.row("sh000001", "09:59:00", "10:15:00", quality="stale_or_future", price=95)
        latest, prior = mm.select_snapshots(self.db, "2026-11-02")["sh000001"]
        self.assertEqual(latest["price"], 100)
        self.assertIsNone(prior)
        data = mm.context(self.db, "2026-11-02", as_of=at("10:15:00"))
        self.assertFalse(data["market"][0]["quote"]["current"])
        self.assertIsNone(data["market"][0]["metrics"]["interval_pct"])

    def test_delta_same_source_and_monotone_turnover(self):
        self.row("sz002185", "10:00:00", "10:00:01", price=100, turnover=1000)
        self.row("sz002185", "10:15:00", "10:15:01", price=102, turnover=1500)
        latest, prev = mm.select_snapshots(self.db, "2026-11-02")["sz002185"]
        data = mm.calculated(latest, prev, "CN")
        self.assertEqual(data["interval_pct"], 2.0)
        self.assertEqual(data["turnover_delta"], 500)
        self.assertEqual(data["turnover_per_min"], round(500 / 15, 2))

    def test_source_mismatch_cannot_compare(self):
        self.row("sz002185", "10:00:00", "10:00:01", source="sina")
        self.row("sz002185", "10:15:00", "10:15:01", source="other")
        _, prev = mm.select_snapshots(self.db, "2026-11-02")["sz002185"]
        self.assertIsNone(prev)

    def test_relative_strength_aligned_window(self):
        self.row("sz002185", "10:00:00", "10:00:01", price=100)
        self.row("sz002185", "10:15:00", "10:15:01", price=103)
        self.row("sz399001", "10:00:00", "10:00:01", price=100)
        self.row("sz399001", "10:15:00", "10:15:01", price=101)
        payload = mm.context(self.db, "2026-11-02", as_of=at("10:15:20"))
        self.assertEqual(payload["watchlist"][0]["metrics"]["relative_strength_pp"], 2.0)

    def test_f86_vendor_time_not_trusted(self):
        quote, verified = mm.provider_timestamp({"f124": 0, "f86": "202611021230"}, "JP")
        self.assertFalse(verified)
        self.assertEqual(mm.quote_quality(quote, at("11:30:00"), "JP", verified), "unverified_time")

    def test_off_session_not_false_failure(self):
        self.assertEqual(mm.quote_quality(at("10:30:00"), at("11:21:00"), "JP"), "session_paused")

    def test_sina_stale_not_counted_as_fresh(self):
        values = ["" for _ in range(32)]
        values[0], values[1], values[2], values[3] = "华天科技", "99", "90", "100"
        values[4], values[5], values[9] = "105", "80", "10000"
        values[30], values[31] = "2026-11-02", "10:00:00"
        raw = 'var hq_str_sz002185="' + ','.join(values) + '";'
        with patch.object(mm, "request", return_value=raw), patch.object(mm.dt, "datetime", wraps=dt.datetime) as mocked:
            # Patch now via an isolated subclass so strptime remains usable.
            mocked.now.return_value = at("10:15:00")
            statuses = mm.collect_sina(self.db)
        self.assertEqual(statuses["sz002185"], "stale_or_future")
        self.assertEqual(sum(v == "fresh" for v in statuses.values()), 0)

    def test_seed_idempotent(self):
        # The test fixture is self-contained; the real repository keeps its own seed JSON.
        fixture = {"snapshots": [{"symbol": "sz002185", "name": "华天科技", "trade_date": "2026-11-02",
                    "quote_time": at("10:00:00").isoformat(), "captured_at": at("10:00:01").isoformat(),
                    "price": 10, "previous_close": 9, "source": "sina", "quality": "historical_reconstructed"}],
                   "observations": []}
        root = Path(self.temp.name)
        (root / "initial_snapshot.json").write_text(json.dumps(fixture), encoding="utf-8")
        with patch.object(mm, "ROOT", root):
            mm.seed(self.db)
            mm.seed(self.db)
        count = self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
