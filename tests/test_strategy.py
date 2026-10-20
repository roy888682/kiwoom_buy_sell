import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.strategy import Decision, Instrument, Portfolio, Position, Settings, Strategy, serialize

D = Decimal
DAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 20, 1, tzinfo=timezone.utc)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.portfolio = Portfolio(cash={"KRW": "10000000", "USD": "10000"})
        self.strategy = Strategy(self.settings, self.portfolio)
        self.stock = self.instrument()

    def instrument(self, symbol="005930", market="KR", ath="10000", **kwargs):
        return Instrument(market, symbol, "시험종목", D(ath), DAY, True, **kwargs)

    def cross(self, stock=None, below="10100", above="10200", now=NOW):
        stock = stock or self.stock
        self.strategy.evaluate_tick(stock, below, DAY, now=now)
        return self.strategy.evaluate_tick(stock, above, DAY, now=now)

    def fill(self, order, price=None, fee="0", now=NOW):
        return self.strategy.apply_order_update(order.id, order.quantity, price or order.limit_price, status="filled", cumulative_fee=fee, now=now)

    def test_first_quote_only_primes_even_above_threshold(self):
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "11000", DAY))
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "12000", DAY))
        self.assertEqual(self.stock.previous_price, D("11000"))

    def test_positive_crossing_equal_threshold_and_sizing(self):
        proposal = self.cross()
        self.assertEqual(proposal.side, "BUY")
        self.assertEqual(proposal.limit_price, D("10200"))
        self.assertEqual(proposal.quantity, 292)
        self.assertLessEqual(proposal.quantity * D("10200") * D("1.005"), D("3000000"))
        self.assertGreater((proposal.quantity + 1) * D("10200") * D("1.005"), D("3000000"))
        self.assertEqual(self.stock.threshold(self.settings), D("10200"))

    def test_negative_rate_requires_crossing_not_merely_near_high(self):
        self.settings.breakout_percent = D("-2")
        self.assertEqual(self.stock.threshold(self.settings), D("9800"))
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "9900", DAY))
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "9850", DAY))
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "9799", DAY))
        proposal = self.strategy.evaluate_tick(self.stock, "9800", DAY)
        self.assertEqual(proposal.side, "BUY")

    def test_downward_crossing_does_not_buy(self):
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "10300", DAY))
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "10000", DAY))

    def test_stale_unverified_and_missing_history_block_buy(self):
        for stock in [self.instrument("1"), self.instrument("2"), self.instrument("3")]:
            if stock.symbol == "1":
                stock.history_verified = False
            elif stock.symbol == "2":
                stock.history_through = date(2026, 10, 16)
            else:
                stock.all_time_high = None
            self.assertIsNone(self.cross(stock))

    def test_submit_never_fills_and_reserves_from_remaining_cash(self):
        order = self.strategy.submit_order(self.stock, self.cross(), now=NOW)
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.filled_quantity, 0)
        self.assertEqual(self.portfolio.positions, {})
        self.assertEqual(self.portfolio.cash["KRW"], D("10000000"))
        self.assertEqual(self.portfolio.available_cash("KRW"), D("7006708.000"))
        other = self.instrument("000660")
        second = self.cross(other)
        self.assertEqual(second.quantity, 205)

    def test_pending_buy_is_not_duplicated_on_repeated_cross(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.assertIsNone(self.cross())
        with self.assertRaises(ValueError):
            self.strategy.submit_order(self.stock, Decision("KR", "005930", "BUY", 1, D("10200"), "breakout"))
        self.assertEqual(order.status, "pending")

    def test_sequential_fill_uses_remaining_cash_and_actual_fee(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.fill(order, fee="14892")
        self.assertEqual(self.portfolio.cash["KRW"], D("7006708"))
        self.assertEqual(self.portfolio.available_cash("KRW"), D("7006708"))
        self.assertEqual(self.cross(self.instrument("000660")).quantity, 205)

    def test_currency_budgets_are_independent(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        usd_stock = self.instrument("SPY", "US", "100")
        usd_decision = self.cross(usd_stock, "101", "102")
        self.assertEqual(usd_decision.quantity, 29)
        self.assertEqual(self.portfolio.available_cash("USD"), D("10000"))
        self.strategy.submit_order(usd_stock, usd_decision)
        self.assertEqual(self.portfolio.available_cash("USD"), D("7027.210"))
        self.assertEqual(order.currency, "KRW")

    def test_partial_fills_and_repeated_poll_are_idempotent(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.strategy.apply_order_update(order.id, 100, "10190", "partial", "100")
        self.assertEqual(self.portfolio.positions[self.stock.key].quantity, 100)
        first_cash = self.portfolio.cash["KRW"]
        self.strategy.apply_order_update(order.id, 100, "10190", "partial", "100")
        self.assertEqual(self.portfolio.cash["KRW"], first_cash)
        self.assertEqual(self.portfolio.positions[self.stock.key].quantity, 100)
        self.strategy.apply_order_update(order.id, 200, "10195", "partial", "200")
        position = self.portfolio.positions[self.stock.key]
        self.assertEqual(position.quantity, 200)
        self.assertEqual(position.average_price, D("10195"))
        self.assertEqual(self.portfolio.cash["KRW"], D("7960800"))
        self.assertEqual(order.reserved_cash, D("943092.000"))

    def test_cancellation_releases_only_unfilled_reserve(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.strategy.apply_order_update(order.id, 100, "10200", "partial", "100")
        self.strategy.apply_order_update(order.id, 100, "10200", "canceled", "100")
        self.assertEqual(order.reserved_cash, 0)
        self.assertEqual(self.portfolio.cash["KRW"], D("8979900"))
        self.assertEqual(self.portfolio.available_cash("KRW"), D("8979900"))
        self.assertEqual(self.portfolio.positions[self.stock.key].quantity, 100)

    def test_rejected_order_releases_reserves(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.strategy.apply_order_update(order.id, 0, 0, "rejected")
        self.assertEqual(self.portfolio.available_cash("KRW"), D("10000000"))
        self.assertFalse(self.portfolio.occupied_keys())

    def test_unknown_submission_keeps_reservation_and_deduplication(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.strategy.apply_order_update(order.id, 0, 0, "unknown")
        self.assertGreater(order.reserved_cash, 0)
        self.assertIsNone(self.cross())

    def test_late_confirmed_fill_after_cancel_updates_once(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.strategy.apply_order_update(order.id, 0, 0, "canceled")
        self.strategy.apply_order_update(order.id, 10, "10200", "partial")
        self.assertEqual(order.status, "canceled")
        self.assertEqual(self.portfolio.positions[self.stock.key].quantity, 10)
        self.strategy.apply_order_update(order.id, 10, "10200", "partial")
        self.assertEqual(self.portfolio.positions[self.stock.key].quantity, 10)

    def test_inconsistent_fill_updates_are_rejected_without_mutation(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        with self.assertRaises(ValueError):
            self.strategy.apply_order_update(order.id, order.quantity + 1, "10200", "filled")
        with self.assertRaises(ValueError):
            self.strategy.apply_order_update(order.id, 10, "10200", "filled")
        self.assertEqual(order.filled_quantity, 0)
        self.assertEqual(self.portfolio.cash["KRW"], D("10000000"))
        self.strategy.apply_order_update(order.id, 100, "10200", "partial", "100")
        for args in [(99, "10200", "partial", "100"), (100, "10201", "partial", "100"), (100, "10200", "partial", "99")]:
            with self.assertRaises(ValueError):
                self.strategy.apply_order_update(order.id, *args)
        self.assertEqual(self.portfolio.positions[self.stock.key].quantity, 100)

    def test_stop_is_based_on_real_average_fill_not_quote(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.fill(order, "10100")
        stop = self.portfolio.positions[self.stock.key].stop_price(self.settings)
        self.assertEqual(stop, D("9595"))
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "9600", DAY))
        exit_order = self.strategy.evaluate_tick(self.stock, "9595", DAY)
        self.assertEqual(exit_order.side, "SELL")
        self.assertEqual(exit_order.quantity, order.quantity)
        self.assertEqual(exit_order.reason, "stop_loss")

    def test_stop_works_without_verified_history_and_without_initial_quote(self):
        self.portfolio.positions[self.stock.key] = Position("KR", "005930", "시험종목", 10, "10200", "10200")
        self.stock.history_verified = False
        self.stock.all_time_high = None
        exit_order = self.strategy.evaluate_tick(self.stock, "9690", DAY)
        self.assertEqual(exit_order.quantity, 10)
        self.assertEqual(exit_order.side, "SELL")

    def test_stop_does_not_duplicate_pending_sell_and_does_not_pyramid(self):
        self.settings.allow_pyramiding = True
        self.settings.breakout_percent = D("-20")
        self.portfolio.positions[self.stock.key] = Position("KR", "005930", "시험종목", 10, "10200", "10200")
        decision = self.strategy.evaluate_tick(self.stock, "9000", DAY)
        order = self.strategy.submit_order(self.stock, decision)
        self.assertEqual(order.side, "SELL")
        self.assertIsNone(self.strategy.evaluate_tick(self.stock, "9100", DAY))
        self.assertEqual(len(self.portfolio.orders), 1)

    def test_cooldown_starts_at_stop_fill_not_submission(self):
        self.portfolio.positions[self.stock.key] = Position("KR", "005930", "시험종목", 10, "10200", "10200")
        decision = self.strategy.evaluate_tick(self.stock, "9690", DAY, NOW)
        order = self.strategy.submit_order(self.stock, decision, now=NOW)
        self.assertNotIn(self.stock.key, self.portfolio.stopped_at)
        fill_time = NOW + timedelta(minutes=10)
        self.fill(order, "9680", now=fill_time)
        self.assertEqual(self.portfolio.stopped_at[self.stock.key], fill_time)
        self.assertIsNone(self.cross(now=fill_time + timedelta(minutes=59)))
        self.assertIsNotNone(self.cross(now=fill_time + timedelta(minutes=60)))

    def test_max_positions_counts_pending_entries(self):
        self.settings.max_positions = 1
        order = self.strategy.submit_order(self.stock, self.cross())
        self.assertIsNone(self.cross(self.instrument("000660")))
        self.strategy.apply_order_update(order.id, 0, 0, "rejected")
        self.assertIsNotNone(self.cross(self.instrument("000660")))

    def test_pyramiding_default_off_and_actual_weighted_fill_when_allowed(self):
        order = self.strategy.submit_order(self.stock, self.cross())
        self.fill(order)
        self.assertIsNone(self.cross())
        self.settings.allow_pyramiding = True
        decision = self.cross()
        self.assertIsNotNone(decision)
        second = self.strategy.submit_order(self.stock, decision)
        self.fill(second, "10190")
        position = self.portfolio.positions[self.stock.key]
        self.assertEqual(position.quantity, order.quantity + second.quantity)
        self.assertEqual(position.average_price, (order.quantity * D("10200") + second.quantity * D("10190")) / position.quantity)

    def test_too_little_cash_and_full_fee_budget_do_not_overbuy(self):
        self.portfolio.cash["KRW"] = D("100")
        self.assertIsNone(self.cross())
        self.portfolio.cash["KRW"] = D("10200")
        self.settings.allocation_percent = D("100")
        self.assertIsNone(self.cross())
        self.settings.fee_reserve_percent = D("0")
        self.assertEqual(self.cross().quantity, 1)

    def test_snapshot_serialization_keeps_decimal_exact(self):
        order = self.strategy.submit_order(self.stock, self.cross(), now=NOW)
        state = serialize({"settings": self.settings, "stock": self.stock, "portfolio": self.portfolio})
        self.assertEqual(state["stock"]["history_through"], "2026-10-19")
        self.assertEqual(state["portfolio"]["orders"][order.id]["limit_price"], "10200")
        self.assertEqual(state["portfolio"]["orders"][order.id]["created_at"], NOW.isoformat())

    def test_settings_and_prices_reject_invalid_values(self):
        for values in [dict(breakout_percent="-100"), dict(allocation_percent="0"), dict(allocation_percent="101"), dict(stop_loss_percent="100"), dict(stop_loss_percent="0"), dict(fee_reserve_percent="-1"), dict(breakout_percent="NaN"), dict(breakout_percent="Infinity"), dict(breakout_percent="1e999999"), dict(breakout_percent="1e-999999"), dict(allocation_percent=True), dict(max_positions=0), dict(max_positions=True), dict(max_positions=1.5), dict(reentry_cooldown_minutes=-1), dict(poll_interval_seconds=0), dict(allow_pyramiding="false")]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    Settings(**values)
        for price in ["0", "-1", "NaN", "Infinity", True]:
            with self.assertRaises(ValueError):
                self.strategy.evaluate_tick(self.stock, price, DAY)


if __name__ == "__main__":
    unittest.main()
