"""Portfolio marks use exact ledger amounts and explicit quote provenance."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from app.strategy import Decision, Instrument, Portfolio, Position, Settings, Strategy
from app.valuation import value_portfolio


D = Decimal
NOW = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)


class ValuationTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("KR", "005930", "삼성전자")
        self.position = Position("KR", "005930", "예전 이름", 3, D("100"), D("110"))
        self.portfolio = Portfolio(cash={"KRW": "1000", "USD": "10.25"}, positions={self.position.key: self.position})
        self.instruments = {self.instrument.key: self.instrument}
        self.metadata = {}
        self.ticks = {}

    def quote(self, *, price="110", age=10, source="kiwoom", verified=True, active=True):
        stamp = NOW - timedelta(seconds=age)
        self.metadata[self.position.key] = {
            "source": "kiwoom",
            "last_quote_at": stamp.isoformat(),
            "last_quote_price": price,
            "last_quote_source": source,
            "last_quote_verified": verified,
        }
        if active:
            self.ticks[self.position.key] = stamp

    def value(self, **kwargs):
        return value_portfolio(self.portfolio, self.instruments, self.metadata, self.ticks,
                               now=NOW, **kwargs)

    def test_current_provider_mark_latest_name_and_exact_values(self):
        self.quote()
        state = self.value(real_market=True)
        row = state["positions"][0]
        self.assertEqual(row["name"], "삼성전자")
        self.assertEqual(row["quantity"], 3)
        self.assertEqual(row["currency"], "KRW")
        self.assertEqual(row["current_price"], "110")
        self.assertEqual(row["market_value"], "330")
        self.assertEqual(row["cost_basis"], "300")
        self.assertEqual(row["unrealized_pnl"], "30")
        self.assertEqual(D(row["return_percent"]), D("10"))
        self.assertEqual(D(row["stop_price"]), D("95"))
        self.assertEqual(row["quote_asof"], self.metadata[self.position.key]["last_quote_at"])
        self.assertEqual(row["quote_age_seconds"], 10)
        self.assertEqual(row["valuation_status"], "fresh")
        self.assertEqual(row["price_source"], "kiwoom")
        bucket = state["valuation"]["KRW"]
        self.assertEqual(bucket["market_value"], "330")
        self.assertEqual(bucket["total_assets"], "1330")
        self.assertEqual(bucket["priced_positions"], 1)
        self.assertEqual(bucket["stale_positions"], 0)
        json.dumps(state, allow_nan=False)
        self.assertEqual(self.position.name, "예전 이름")

    def test_each_currency_has_its_own_total_without_conversion(self):
        self.quote()
        us = Position("US", "AAPL", "Apple", 2, D("120.25"), D("125.55"))
        self.portfolio.positions[us.key] = us
        self.metadata[us.key] = {"source": "kiwoom"}
        state = self.value(real_market=True)
        self.assertEqual(set(state["valuation"]), {"KRW", "USD"})
        self.assertEqual(state["valuation"]["KRW"]["total_assets"], "1330")
        usd = state["valuation"]["USD"]
        self.assertEqual(usd["market_value"], "251.10")
        self.assertEqual(usd["cost_basis"], "240.50")
        self.assertEqual(usd["unrealized_pnl"], "10.60")
        self.assertEqual(usd["total_assets"], "261.35")
        self.assertEqual(usd["valuation_status"], "stale")

    def test_actual_fees_and_pending_reserves_are_not_deducted_twice(self):
        portfolio = Portfolio(cash={"KRW": "1000", "USD": "50"})
        settings = Settings(fee_reserve_percent=D("1"))
        engine = Strategy(settings, portfolio)
        filled = engine.submit_order(self.instrument,
                                     Decision("KR", "005930", "BUY", 3, D("100"), "test"), now=NOW)
        engine.apply_order_update(filled.id, 3, "100", "filled", cumulative_fee="3")
        other = Instrument("KR", "000660", "SK하이닉스")
        engine.submit_order(other, Decision("KR", "000660", "BUY", 2, D("100"), "test"), now=NOW)
        self.quote()
        state = value_portfolio(portfolio, self.instruments, self.metadata, self.ticks,
                                settings=settings, now=NOW, real_market=True)
        bucket = state["valuation"]["KRW"]
        self.assertEqual(D(bucket["cash_balance"]), D("697"))
        self.assertEqual(D(bucket["available_cash"]), D("495"))
        self.assertEqual(D(bucket["cost_basis"]), D("300"))
        self.assertEqual(D(bucket["unrealized_pnl"]), D("30"))
        self.assertEqual(D(bucket["total_assets"]), D("1027"))

    def test_stopped_detector_keeps_last_verified_quote(self):
        self.quote(price="111.25")
        self.instrument.current_price = None
        self.instrument.previous_price = None
        state = self.value(real_market=True)
        self.assertEqual(state["positions"][0]["current_price"], "111.25")
        self.assertEqual(state["positions"][0]["market_value"], "333.75")

    def test_restart_keeps_recent_stored_quote_but_cannot_call_it_fresh(self):
        self.quote(active=False)
        state = self.value(real_market=True)
        self.assertEqual(state["positions"][0]["quote_age_seconds"], 10)
        self.assertEqual(state["positions"][0]["valuation_status"], "stale")
        self.assertEqual(state["valuation"]["KRW"]["stale_positions"], 1)
        self.assertEqual(state["valuation"]["KRW"]["total_assets"], "1330")

    def test_freshness_limits_and_provider_sources(self):
        for age, expected in [(-6, "stale"), (-5, "fresh"), (0, "fresh"), (90, "fresh"), (91, "stale")]:
            for source in ("kiwoom", "kiwoom_ws"):
                with self.subTest(age=age, source=source):
                    self.quote(age=age, source=source)
                    row = self.value(real_market=True)["positions"][0]
                    self.assertEqual(row["valuation_status"], expected)
                    self.assertEqual(row["price_source"], source)

    def test_recent_metadata_does_not_override_stale_active_tick(self):
        self.quote(age=1)
        self.ticks[self.position.key] = NOW - timedelta(seconds=91)
        self.assertEqual(self.value(real_market=True)["positions"][0]["valuation_status"], "stale")

    def test_provider_source_and_verified_flag_are_both_required(self):
        for source, verified in [("manual", True), ("demo", True), ("kiwoom", False), ("kiwoom", "true")]:
            with self.subTest(source=source, verified=verified):
                self.quote(source=source, verified=verified, price="9999")
                row = self.value(real_market=True)["positions"][0]
                self.assertEqual(row["valuation_status"], "stale")
                self.assertEqual(row["current_price"], "110")

    def test_recent_timestamp_cannot_make_an_invalid_quote_price_fresh(self):
        for price in (None, "bad", "0", "NaN"):
            with self.subTest(price=price):
                self.quote(price=price)
                row = self.value(real_market=True)["positions"][0]
                self.assertEqual(row["current_price"], "110")
                self.assertEqual(row["price_source"], "stored")
                self.assertEqual(row["valuation_status"], "stale")

    def test_untimestamped_mark_is_stale_and_has_no_age(self):
        row = self.value(real_market=True)["positions"][0]
        self.assertEqual(row["current_price"], "110")
        self.assertEqual(row["valuation_status"], "stale")
        self.assertEqual(row["price_source"], "stored")
        self.assertIsNone(row["quote_asof"])
        self.assertIsNone(row["quote_age_seconds"])

    def test_real_market_does_not_use_unverified_instrument_mark(self):
        self.instrument.current_price = D("9999")
        self.metadata[self.position.key] = {"source": "demo"}
        row = self.value(real_market=True)["positions"][0]
        self.assertEqual(row["current_price"], "110")
        self.assertEqual(row["valuation_status"], "stale")

    def test_naive_or_malformed_active_timestamp_cannot_be_fresh(self):
        for stamp in (NOW.replace(tzinfo=None), "broken", None):
            with self.subTest(stamp=stamp):
                self.quote()
                self.ticks[self.position.key] = stamp
                self.assertEqual(self.value(real_market=True)["positions"][0]["valuation_status"], "stale")

    def test_demo_and_manual_marks_are_explicit_simulations(self):
        for source in ("demo", "manual"):
            with self.subTest(source=source):
                self.metadata[self.position.key] = {"source": source}
                self.instrument.current_price = D("120")
                state = self.value()
                self.assertEqual(state["positions"][0]["valuation_status"], "simulation")
                self.assertEqual(state["positions"][0]["price_source"], source)
                self.assertEqual(state["valuation"]["KRW"]["valuation_status"], "simulation")
                self.assertEqual(state["valuation"]["KRW"]["market_value"], "360")

    def test_missing_mark_makes_total_unknown_and_preserves_known_subset(self):
        self.quote()
        unknown = Position("KR", "000660", "SK하이닉스", 2, D("200"), D("200"))
        unknown.current_price = None
        self.portfolio.positions[unknown.key] = unknown
        state = self.value(real_market=True)
        row = next(row for row in state["positions"] if row["key"] == unknown.key)
        self.assertEqual(row["valuation_status"], "unavailable")
        self.assertEqual(row["cost_basis"], "400")
        for field in ("current_price", "market_value", "unrealized_pnl", "return_percent"):
            self.assertIsNone(row[field])
        bucket = state["valuation"]["KRW"]
        self.assertEqual(bucket["known_market_value"], "330")
        self.assertEqual(bucket["cost_basis"], "700")
        self.assertEqual(bucket["unpriced_positions"], 1)
        self.assertEqual(bucket["priced_positions"], 1)
        self.assertEqual(bucket["valuation_status"], "unavailable")
        for field in ("market_value", "unrealized_pnl", "total_assets", "return_percent"):
            self.assertIsNone(bucket[field])

    def test_nonpositive_nonfinite_and_invalid_marks_are_unavailable(self):
        for price in (None, D("0"), D("-1"), D("NaN"), D("Infinity"), True, "broken"):
            with self.subTest(price=price):
                self.position.current_price = price
                state = self.value(real_market=True)
                self.assertIsNone(state["positions"][0]["market_value"])
                self.assertEqual(state["valuation"]["KRW"]["valuation_status"], "unavailable")
                json.dumps(state, allow_nan=False)

    def test_empty_currency_has_zero_amounts_and_cash_as_total_assets(self):
        bucket = self.value()["valuation"]["USD"]
        for field in ("market_value", "known_market_value", "cost_basis", "unrealized_pnl", "return_percent"):
            self.assertEqual(bucket[field], "0")
        self.assertEqual(bucket["total_assets"], "10.25")
        self.assertEqual(bucket["priced_positions"], 0)
        self.assertEqual(bucket["unpriced_positions"], 0)
        self.assertEqual(bucket["valuation_status"], "fresh")

    def test_zero_quantity_is_not_a_holding_or_stale_position(self):
        self.position.quantity = 0
        state = self.value(real_market=True)
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["valuation"]["KRW"]["total_assets"], "1000")
        self.assertEqual(state["valuation"]["KRW"]["stale_positions"], 0)

    def test_precision_is_preserved_beyond_float_and_decimal_default_context(self):
        self.position.quantity = 987654321
        self.position.average_price = D("900000000000000000.12345678901234567890123456789")
        self.position.current_price = D("900000000000000000.22345678901234567890123456789")
        self.portfolio.cash["KRW"] = D("0.00000000000000000000000000001")
        with localcontext() as context:
            context.prec = 120
            expected_market_value = self.position.current_price * self.position.quantity
            expected_cost = self.position.average_price * self.position.quantity
            expected_total = expected_market_value + self.portfolio.cash["KRW"]
            expected_pnl = expected_market_value - expected_cost
        with localcontext() as context:
            context.prec = 10
            state = self.value(real_market=True)
        row = state["positions"][0]
        self.assertEqual(D(row["market_value"]), expected_market_value)
        self.assertEqual(D(row["cost_basis"]), expected_cost)
        self.assertEqual(D(row["unrealized_pnl"]), expected_pnl)
        self.assertEqual(D(state["valuation"]["KRW"]["total_assets"]), expected_total)


if __name__ == "__main__":
    unittest.main()
