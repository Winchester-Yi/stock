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

    def test_watchlist_matches_current_configuration(self):
        expected = {
            "sz000678": "襄阳轴承", "sz002241": "歌尔股份", "sz002242": "九阳股份",
            "sz002929": "润建股份", "sh600410": "华胜天成", "sh601086": "国芳集团",
            "sh603002": "宏昌电子",
        }
        payload = mm.context(self.db, "2026-11-02", as_of=at("10:15:00"))
        self.assertEqual({item["symbol"]: item["name"] for item in payload["watchlist"]}, expected)
        self.assertEqual(set(mm.SYMBOLS), set(expected) | set(mm.INDICES))

    def test_expired_quote_cannot_override_latest_valid(self):
        self.row("sh000001", "10:00:00", "10:00:01", price=100)
        self.row("sh000001", "09:59:00", "10:15:00", quality="stale_or_future", price=95)
        latest, prior = mm.select_snapshots(self.db, "2026-11-02")["sh000001"]
        self.assertEqual(latest["price"], 100)
        self.assertIsNone(prior)
        data = mm.context(self.db, "2026-11-02", as_of=at("10:15:00"))
        self.assertFalse(data["market"][0]["quote"]["current"])
        self.assertIsNone(data["market"][0]["metrics"]["interval_pct"])

    def test_current_quote_requires_non_future_timestamp_and_fresh_age(self):
        self.row("sz000678", "10:00:05", "10:00:06")
        for clock, current, age in (("10:00:04", False, -1), ("10:00:05", True, 0),
                                    ("10:02:05", True, 120), ("10:02:06", False, 121)):
            with self.subTest(clock=clock):
                item = mm.context(self.db, "2026-11-02", as_of=at(clock))["watchlist"][0]
                self.assertEqual(item["quote"]["age_seconds"], age)
                self.assertEqual(item["quote"]["current"], current)
                self.assertEqual(item["metrics"]["usable_for_live_inference"], current)

    def test_delta_same_source_and_monotone_turnover(self):
        self.row("sz000678", "10:00:00", "10:00:01", price=100, turnover=1000)
        self.row("sz000678", "10:15:00", "10:15:01", price=102, turnover=1500)
        latest, prev = mm.select_snapshots(self.db, "2026-11-02")["sz000678"]
        data = mm.calculated(latest, prev, "CN")
        self.assertEqual(data["interval_pct"], 2.0)
        self.assertEqual(data["turnover_delta"], 500)
        self.assertEqual(data["turnover_per_min"], round(500 / 15, 2))

    def test_source_mismatch_cannot_compare(self):
        self.row("sz000678", "10:00:00", "10:00:01", source="sina")
        self.row("sz000678", "10:15:00", "10:15:01", source="other")
        _, prev = mm.select_snapshots(self.db, "2026-11-02")["sz000678"]
        self.assertIsNone(prev)

    def test_relative_strength_aligned_window(self):
        self.row("sz000678", "10:00:00", "10:00:01", price=100)
        self.row("sz000678", "10:15:00", "10:15:01", price=103)
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
        values[0], values[1], values[2], values[3] = "襄阳轴承", "99", "90", "100"
        values[4], values[5], values[9] = "105", "80", "10000"
        values[30], values[31] = "2026-11-02", "10:00:00"
        raw = 'var hq_str_sz000678="' + ','.join(values) + '";'
        with patch.object(mm, "request", return_value=raw), patch.object(mm.dt, "datetime", wraps=dt.datetime) as mocked:
            # Patch now via an isolated subclass so strptime remains usable.
            mocked.now.return_value = at("10:15:00")
            statuses = mm.collect_sina(self.db)
        self.assertEqual(statuses["sz000678"], "stale_or_future")
        self.assertEqual(sum(v == "fresh" for v in statuses.values()), 0)

    def test_seed_idempotent(self):
        # The test fixture is self-contained; the real repository keeps its own seed JSON.
        fixture = {"snapshots": [{"symbol": "sz000678", "name": "襄阳轴承", "trade_date": "2026-11-02",
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

    def test_historical_metrics_retained_outside_live_hours(self):
        self.row("sz000678", "10:00:00", "10:00:01", price=100, turnover=1000)
        self.row("sz000678", "10:15:00", "10:15:01", price=102, turnover=1300)
        result = mm.context(self.db, "2026-11-02", as_of=at("16:00:00"))["watchlist"][0]
        self.assertFalse(result["quote"]["current"])
        self.assertEqual(result["metrics"]["interval_pct"], 2.0)
        self.assertEqual(result["metrics"]["turnover_delta"], 300)
        self.assertEqual(result["metrics"]["turnover_per_min"], 20)
        self.assertFalse(result["metrics"]["usable_for_live_inference"])

    def test_historical_metrics_retained_when_reviewing_old_date(self):
        self.row("sz000678", "10:00:00", "10:00:01", price=100)
        self.row("sz000678", "10:15:00", "10:15:01", price=101)
        result = mm.context(self.db, "2026-11-02", as_of=at("17:00:00"))["watchlist"][0]
        self.assertEqual(result["metrics"]["interval_pct"], 1.0)

    def test_overseas_positive_f124_does_not_verify_exchange_time(self):
        stamp = int(at("10:00:00").timestamp())
        quote, verified = mm.provider_timestamp({"f124": stamp}, "JP")
        self.assertIsNotNone(quote)
        self.assertFalse(verified)

    def test_overseas_scale_is_not_assumed(self):
        payload = {"f57": "N225", "f43": 6312345, "f60": 6320000,
                   "f44": 6400000, "f45": 6300000, "f46": 6320000,
                   "f48": 999, "f124": int(at("10:00:00").timestamp())}
        with patch.object(mm, "east_data", return_value=(payload, at("10:00:01"))):
            self.assertEqual(mm.collect_east(self.db, "N225", "100.N225", "JP", expected_code="N225"),
                             "unverified_contract")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM snapshots WHERE symbol='N225'").fetchone()[0], 0)

    def test_old_overseas_row_is_quarantined(self):
        self.row("N225", "10:00:00", "10:00:01", source="eastmoney", price=999)
        result = mm.context(self.db, "2026-11-02", as_of=at("10:00:30"))["overseas"][0]
        self.assertIsNone(result["quote"]["price"])
        self.assertFalse(result["quote"]["price_scale_verified"])
        self.assertIsNone(result["metrics"])

    def test_price_consistency_and_bad_optional_fields(self):
        with self.assertRaisesRegex(ValueError, "开盘价超出"):
            mm.validate_prices(100, 99, 102, 98, open_price=110)
        with self.assertRaisesRegex(ValueError, "最高价低于"):
            mm.validate_prices(100, 99, 90, 110)
        with self.assertRaisesRegex(ValueError, "累计成交额为负"):
            mm.validate_prices(100, 99, turnover=-2)
        with self.assertRaisesRegex(ValueError, "不是有限数字"):
            mm.optional_number("NaN", "成交额")
        self.assertIsNone(mm.optional_number("--", "成交额"))

    def test_existing_fetch_runs_migrate_without_losing_rows(self):
        old_path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(old_path) as old:
            old.execute("CREATE TABLE fetch_runs (id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT, statuses_json TEXT)")
            old.execute("INSERT INTO fetch_runs(started_at,ended_at,statuses_json) VALUES(?,?,?)",
                        (at("09:30:00").isoformat(), at("09:30:02").isoformat(), "{}"))
        with mm.connection(old_path) as migrated:
            self.assertIn("outcome", {r[1] for r in migrated.execute("PRAGMA table_info(fetch_runs)")})
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM fetch_runs").fetchone()[0], 1)


class FetchOutcomeTests(unittest.TestCase):
    def valid(self, status="fresh", overseas="not_attempted"):
        return {**{s: status for s in mm.SYMBOLS},
                **{s: overseas for s in mm.OVERSEAS}}

    def test_all_http_failures_during_lunch_are_not_success(self):
        statuses = self.valid("error: timeout", "error: timeout")
        self.assertEqual(mm.assess_fetch(statuses, "lunch_break"), ("failed", 2))

    def test_partial_failure_off_hours_is_degraded(self):
        statuses = self.valid("session_paused")
        statuses["sz000678"] = "error: timeout"
        self.assertEqual(mm.assess_fetch(statuses, "closed"), ("degraded", 1))

    def test_healthy_market_closed_is_not_a_failure(self):
        self.assertEqual(mm.assess_fetch(self.valid("session_paused"), "lunch_break"),
                         ("market_closed", 0))

    def test_overseas_unverified_live_market_is_degraded(self):
        self.assertEqual(mm.assess_fetch(self.valid(overseas="unverified_contract"), "trading"),
                         ("degraded", 1))
        self.assertEqual(mm.assess_fetch(self.valid(), "trading"), ("success", 0))

    def test_all_skipped_is_failed(self):
        self.assertEqual(mm.assess_fetch(self.valid("not_attempted"), "closed"), ("failed", 2))

    def test_fetch_without_overseas_succeeds_without_sector_request(self):
        with tempfile.TemporaryDirectory() as temp, mm.connection(Path(temp) / "test.db") as db:
            with patch.object(mm, "collect_sina", return_value={s: "fresh" for s in mm.SYMBOLS}), \
                 patch.object(mm, "collect_east") as overseas_collector, \
                 patch.object(mm, "session_state", return_value=("trading", False)), \
                 patch.object(mm, "report"):
                self.assertEqual(mm.fetch(db, overseas=False), 0)
            overseas_collector.assert_not_called()
            stored = db.execute("SELECT outcome,statuses_json FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
            statuses = json.loads(stored["statuses_json"])
            self.assertEqual(stored["outcome"], "success")
            self.assertEqual(set(statuses), set(mm.SYMBOLS) | set(mm.OVERSEAS))
            self.assertTrue(all(statuses[s] == "not_attempted" for s in mm.OVERSEAS))

    def test_fetch_overseas_failure_degrades_successful_domestic_fetch(self):
        with tempfile.TemporaryDirectory() as temp, mm.connection(Path(temp) / "test.db") as db:
            with patch.object(mm, "collect_sina", return_value={s: "fresh" for s in mm.SYMBOLS}), \
                 patch.object(mm, "collect_east", side_effect=OSError("blocked")) as overseas_collector, \
                 patch.object(mm, "session_state", return_value=("trading", False)), \
                 patch.object(mm, "report"):
                self.assertEqual(mm.fetch(db), 1)
            self.assertEqual(overseas_collector.call_count, len(mm.OVERSEAS))
            stored = db.execute("SELECT outcome,statuses_json FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
            statuses = json.loads(stored["statuses_json"])
            self.assertEqual(stored["outcome"], "degraded")
            self.assertTrue(all(statuses[s].startswith("error:") for s in mm.OVERSEAS))

    def test_fetch_persists_result_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temp, mm.connection(Path(temp) / "test.db") as db:
            with patch.object(mm, "collect_sina", side_effect=OSError("blocked")), \
                 patch.object(mm, "collect_east") as overseas_collector, \
                 patch.object(mm, "session_state", return_value=("trading", False)), \
                 patch.object(mm, "report"):
                self.assertEqual(mm.fetch(db, overseas=False), 2)
            overseas_collector.assert_not_called()
            stored = db.execute("SELECT outcome,statuses_json FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(stored["outcome"], "failed")
            self.assertTrue(json.loads(stored["statuses_json"])["sz000678"].startswith("error:"))


if __name__ == "__main__":
    unittest.main()
