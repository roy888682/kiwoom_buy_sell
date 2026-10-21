"""Real-market virtual ledger integration; every quote here is test input.

The broker deliberately fails any account/order call. No network is opened.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import Controller, create_app
from app.marketdata import market_day, previous_session
from app.strategy import Position


class QuotesOnlyBroker:
    def __init__(self):
        self.calls = []
        self.rows = [
            {"market": "KR", "symbol": "005930", "name": "삼성전자", "exchange": "KRX", "kind": "STOCK"},
            {"market": "KR", "symbol": "069500", "name": "KODEX 200", "exchange": "KRX", "kind": "ETF"},
            {"market": "US", "symbol": "SPY", "name": "SPDR S&P 500 ETF", "exchange": "AM", "kind": "ETF"},
        ]

    async def universe(self, markets):
        self.calls.append(("universe", tuple(markets)))
        return [{**r, "listing_date": previous_session(r["market"]).isoformat()}
                for r in self.rows if r["market"] in markets]

    async def history(self, instrument, today):
        self.calls.append(("history", instrument["symbol"]))
        high, close = ("10000", "9900") if instrument["market"] == "KR" else ("100", "99")
        return {"bars": [{"date": previous_session(instrument["market"]).isoformat(),
                          "high": high, "close": close, "adjusted": True}], "history_complete": True}

    async def quote(self, instrument):
        self.calls.append(("quote", instrument["symbol"]))
        if not instrument.get("exchange"):
            raise AssertionError("Discovery must resolve the exchange first")
        return {"price": "10300" if instrument["market"] == "KR" else "103", **quote_time()}

    async def forbidden(self, *args, **kwargs):
        self.calls.append(("FORBIDDEN", args))
        raise AssertionError("Real-market paper trading must not read accounts or submit orders")

    cash = holdings = open_orders = orderable_cash = submit_order = cancel_order = forbidden

    async def close(self):
        pass


def quote_time(stamp=None):
    return {"timestamp_verified": True,
            "market_timestamp": (stamp or datetime.now(timezone.utc)).isoformat()}


class Feed:
    def __init__(self, broker, market, instruments, on_quote, on_gap, on_status):
        self.market, self.instruments = market, instruments
        self.on_quote, self.on_gap, self.on_status = on_quote, on_gap, on_status
        self.ready = False
        self.closed = asyncio.Event()

    async def run(self):
        await self.closed.wait()

    async def acknowledge(self):
        self.ready = True
        await self.on_status({"state": "ready", "requested": len(self.instruments), "admitted": len(self.instruments)})

    async def emit(self, symbol, price, stamp=None):
        instrument = next(i for i in self.instruments if i["symbol"] == symbol)
        await self.on_quote(instrument, {"price": str(price), **quote_time(stamp)})

    async def gap(self):
        self.ready = False
        await self.on_gap("test disconnect")

    async def stop(self):
        self.ready = False
        self.closed.set()


class MarketPaperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="market-paper-")
        self.broker = QuotesOnlyBroker()
        self.c = Controller(self.directory.name, broker=self.broker)
        self.streams = patch("app.stream.KiwoomStream", Feed)
        self.sessions = patch("app.main.session_open", return_value=True)
        self.streams.start()
        self.sessions.start()

    async def asyncTearDown(self):
        try:
            await self.c.close()
            self.assertFalse(any(c[0] == "FORBIDDEN" for c in self.broker.calls))
        finally:
            self.sessions.stop()
            self.streams.stop()
            self.directory.cleanup()

    async def ready(self, market="ALL", recovery=False):
        await self.c.start("paper", paper_source="market", market=market, recovery_enabled=recovery)
        await self.wait_feeds()

    async def wait_feeds(self):
        for _ in range(500):
            if self.c.scanner["status"] in {"completed", "error"}:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.c.scanner["status"], "completed", self.c.latest_error)
        self.assertTrue(self.c.feeds)
        for feed in self.c.feeds.values():
            await feed.acknowledge()

    async def buy(self, market="KR", symbol="005930"):
        feed = self.c.feeds[market]
        await feed.emit(symbol, "10100" if market == "KR" else "101")
        await feed.emit(symbol, "10200" if market == "KR" else "102")

    async def test_real_names_whole_shares_remaining_cash_and_stop_without_broker_orders(self):
        await self.ready("KR")
        c, feed = self.c, self.c.feeds["KR"]
        await feed.emit("005930", "10300")
        self.assertEqual(c.portfolio.orders, {})  # first above-threshold quote is a baseline
        await self.buy()
        first = c.portfolio.positions["KR:005930"]
        self.assertEqual((first.name, first.quantity, first.average_price), ("삼성전자", 292, Decimal("10200")))
        self.assertEqual(c.portfolio.available_cash("KRW"), Decimal("7006708"))
        await feed.emit("005930", "10400")
        state = c.state()
        self.assertEqual(state["positions"][0]["market_value"], "3036800")
        self.assertEqual(Decimal(state["valuation"]["KRW"]["total_assets"]), Decimal("10043508"))
        self.assertEqual(state["positions"][0]["valuation_status"], "fresh")
        await self.buy(symbol="069500")
        self.assertEqual(c.portfolio.positions["KR:069500"].quantity, 205)
        await feed.emit("005930", "9690")
        self.assertNotIn("KR:005930", c.portfolio.positions)
        last_order = list(c.portfolio.orders.values())[-1]
        self.assertEqual((last_order.side, last_order.quantity, last_order.reason), ("SELL", 292, "stop_loss"))
        self.assertEqual(c.portfolio.cash["USD"], Decimal("10000"))
        self.assertTrue(all(order.status == "filled" for order in c.portfolio.orders.values()))

    async def test_us_selection_discovery_and_fills_use_only_virtual_dollars(self):
        await self.ready("US")
        self.assertEqual(set(self.c.feeds), {"US"})
        self.assertEqual(set(self.c.instruments), {"US:SPY"})
        self.assertIn(("universe", ("US",)), self.broker.calls)
        await self.buy("US", "SPY")
        self.assertEqual(self.c.portfolio.positions["US:SPY"].quantity, 29)
        self.assertEqual(self.c.portfolio.cash["USD"], Decimal("7027.21"))
        self.assertEqual(self.c.portfolio.cash["KRW"], Decimal("10000000"))

    async def test_watchlist_discovers_missing_us_exchange_before_quote(self):
        self.c.settings.universe_mode = "watchlist"
        self.c.add("US", "SPY")
        self.c.add("KR", "005930")
        await self.ready("US")
        self.assertEqual(self.broker.calls[0], ("universe", ("US",)))
        self.assertEqual(set(self.c.feeds), {"US"})
        self.assertEqual(self.c.metadata["US:SPY"]["exchange"], "AM")
        self.assertFalse(any(call == ("quote", "005930") for call in self.broker.calls))

    async def test_market_selection_blocks_new_foreign_entries_but_keeps_existing_stop_protection(self):
        await self.ready("KR")
        self.c.settings.allow_pyramiding = True
        item = self.c.add("US", "SPY", "SPDR S&P 500 ETF", source="kiwoom", exchange="AM")
        self.c.apply_history(item, {"all_time_high": "100", "history_through": previous_session("US"),
                                   "history_verified": True}, provenance="provider")
        self.c.portfolio.positions[item.key] = Position("US", "SPY", item.name, 3, Decimal("100"), Decimal("100"))
        await self.c.start_feeds()
        self.assertEqual(set(self.c.feeds), {"KR", "US"})
        for feed in self.c.feeds.values():
            await feed.acknowledge()
        await self.buy("US", "SPY")
        self.assertEqual(self.c.portfolio.orders, {})
        await self.c.feeds["US"].emit("SPY", "95")
        self.assertNotIn(item.key, self.c.portfolio.positions)
        sell = next(iter(self.c.portfolio.orders.values()))
        self.assertEqual((sell.side, sell.quantity, sell.reason), ("SELL", 3, "stop_loss"))

    async def test_entry_requires_verified_current_history_and_acknowledged_feeds(self):
        await self.ready("KR")
        c, meta = self.c, self.c.metadata["KR:005930"]
        good_day = market_day("KR").isoformat()
        for provenance, adjustment, ready in (("demo", good_day, True),
                                              ("provider", "1999-01-01", True),
                                              ("provider", good_day, False)):
            with self.subTest(provenance=provenance, adjustment=adjustment, ready=ready):
                meta["history_source"], meta["adjustment_as_of"] = provenance, adjustment
                c.feeds["KR"].ready = ready
                await self.buy()
                self.assertEqual(c.portfolio.orders, {})
        meta["history_source"], meta["adjustment_as_of"] = "provider", good_day
        c.feeds["KR"].ready = True
        await self.buy()
        self.assertEqual(len(c.portfolio.orders), 1)

    async def test_demo_history_is_replaced_by_provider_history_before_trading(self):
        item = self.c.add("KR", "005930", "Incorrect example name", source="demo")
        self.c.apply_history(item, {"all_time_high": "1", "history_through": previous_session("KR"),
                                   "history_verified": True}, provenance="demo")
        await self.ready("KR")
        self.assertIn(("history", "005930"), self.broker.calls)
        self.assertEqual(item.all_time_high, Decimal("10000"))
        self.assertEqual(item.name, "삼성전자")
        self.assertEqual(self.c.metadata[item.key]["history_source"], "provider")
        self.assertEqual(self.c.metadata[item.key]["source"], "kiwoom")
        self.assertEqual(self.c.portfolio.orders, {})

    async def test_rest_price_cannot_consume_websocket_crossing(self):
        await self.ready("KR")
        await self.c.feeds["KR"].emit("005930", "10100")
        await self.c.tick("KR:005930", "10200", source="kiwoom", quote=quote_time())
        self.assertEqual(self.c.instruments["KR:005930"].current_price, Decimal("10100"))
        self.assertEqual(self.c.portfolio.orders, {})
        await self.c.feeds["KR"].emit("005930", "10200")
        self.assertEqual(len(self.c.portfolio.orders), 1)

    async def test_closed_session_and_stale_samples_do_not_trade_or_regress_valuation(self):
        await self.ready("KR")
        feed, item = self.c.feeds["KR"], self.c.instruments["KR:005930"]
        now = datetime.now(timezone.utc)
        await feed.emit("005930", "10100", now)
        await feed.emit("005930", "10200", now - timedelta(minutes=5))
        self.assertIsNone(item.current_price)
        self.assertEqual(self.c.metadata[item.key]["last_quote_price"], "10100")
        await feed.emit("005930", "10300")
        self.assertEqual(self.c.portfolio.orders, {})
        with patch("app.main.session_open", return_value=False):
            await feed.emit("005930", "10100")
            await feed.emit("005930", "10200")
        self.assertIsNone(item.current_price)
        self.assertEqual(self.c.portfolio.orders, {})

    async def test_restarted_durable_mark_rejects_old_quotes_and_requires_new_crossing(self):
        await self.ready("KR")
        await self.buy()
        await self.c.stop()
        stamp = datetime.now(timezone.utc)
        await self.c.tick("KR:005930", "10400", source="kiwoom", quote=quote_time(stamp))
        self.c.save()
        await self.c.close()
        self.c = Controller(self.directory.name, broker=self.broker)
        self.assertEqual(self.c.state()["positions"][0]["valuation_status"], "stale")
        await self.c.tick("KR:005930", "5000", source="kiwoom", quote=quote_time(stamp-timedelta(seconds=1)))
        self.assertEqual(self.c.portfolio.positions["KR:005930"].current_price, Decimal("10400"))
        self.assertEqual(self.c.metadata["KR:005930"]["last_quote_price"], "10400")
        self.assertEqual(len(self.c.portfolio.orders), 1)

    async def test_manual_prices_cannot_overwrite_provider_prices_even_in_demo(self):
        await self.ready("KR")
        await self.c.stop()
        self.c.reset_demo()
        with self.assertRaisesRegex(ValueError, "덮어쓸"):
            await self.c.tick("KR:005930", "1")
        self.assertNotEqual(self.c.instruments["KR:005930"].current_price, Decimal("1"))

    async def test_demo_reset_and_market_reset_preserve_each_others_ledgers(self):
        await self.ready("KR")
        await self.buy()
        await self.c.stop()
        market_cash = self.c.portfolios["market_paper"].cash.copy()
        self.c.reset_demo()
        self.assertEqual(self.c.portfolios["market_paper"].cash, market_cash)
        self.assertEqual(len(self.c.portfolios["market_paper"].orders), 1)
        self.c.portfolios["paper"].cash["KRW"] = Decimal("123456")
        self.c.portfolios["live"].cash["KRW"] = Decimal("765432")
        self.c.reset_market_paper("5000000", "0", "모의투자 초기화")
        self.assertEqual(self.c.portfolio.orders, {})
        self.assertEqual(self.c.portfolio.cash, {"KRW": Decimal("5000000"), "USD": Decimal("0")})
        self.assertEqual(self.c.portfolios["paper"].cash["KRW"], Decimal("123456"))
        self.assertEqual(self.c.portfolios["live"].cash["KRW"], Decimal("765432"))

    async def test_feed_loss_waits_before_recovery_and_new_first_tick_is_only_a_baseline(self):
        await self.ready("KR", recovery=True)
        await self.c.feeds["KR"].emit("005930", "10100")
        await self.c.feeds["KR"].gap()
        await self.c.worker_task
        self.assertFalse(self.c.running)
        self.assertTrue(self.c.paper_requested)
        self.assertEqual(self.c.paper_state()["recovery_status"], "waiting")
        self.assertFalse(await self.c.recover_paper_once())
        self.c.paper_retry_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.assertTrue(await self.c.recover_paper_once())
        await self.wait_feeds()
        await self.c.feeds["KR"].emit("005930", "10300")
        self.assertEqual(self.c.portfolio.orders, {})
        await self.c.stop()
        self.assertFalse(self.c.paper_requested)
        self.assertFalse(await self.c.recover_paper_once())

    async def test_manual_stop_cancels_recovery_queued_on_state_lock(self):
        self.c.paper_requested = True
        self.c.worker = AsyncMock()
        original_start, entered = self.c.start, asyncio.Event()

        async def observed_start(*args, **kwargs):
            entered.set()
            return await original_start(*args, **kwargs)

        self.c.start = observed_start
        await self.c.lock.acquire()
        recovery = asyncio.create_task(self.c.recover_paper_once())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            await self.c.stop()
        finally:
            self.c.lock.release()
        self.assertFalse(await recovery)
        self.assertFalse(self.c.running)
        self.assertFalse(self.c.paper_requested)
        self.c.worker.assert_not_called()

    async def test_restart_resumes_only_requested_market_paper_and_manual_stop_clears_intent(self):
        self.c.worker = AsyncMock()
        await self.c.start("paper", paper_source="market", market="KR", recovery_enabled=True)
        await self.c.close()
        self.c = Controller(self.directory.name, broker=self.broker)
        self.c.worker = AsyncMock()
        self.assertFalse(self.c.running)
        self.assertTrue(self.c.paper_requested)
        self.assertTrue(await self.c.recover_paper_once())
        await self.c.stop()
        await self.c.close()
        self.c = Controller(self.directory.name, broker=self.broker)
        self.assertFalse(self.c.paper_requested)
        self.assertFalse(await self.c.recover_paper_once())
        self.c.mode = "live"
        self.c.paper_requested = True  # malformed old saved intent must not arm real orders
        self.assertFalse(await self.c.recover_paper_once())


class MarketPaperHttpTests(unittest.TestCase):
    def test_first_start_requires_keys_and_manual_history_cannot_change_real_symbol(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "KIWOOM_APP_KEY": "", "KIWOOM_SECRET_KEY": "", "ALLOW_LIVE_TRADING": "false"}):
            with TestClient(create_app(directory)) as client:
                started = client.post("/api/engine/start", json={"mode": "paper", "paper_source": "market"})
                self.assertEqual(started.status_code, 400)
                self.assertIn("앱 키", started.json()["detail"])
                c = client.app.state.controller
                c.add("KR", "005930", "삼성전자", source="kiwoom")
                self.assertEqual(client.post("/api/watchlist/KR:005930/tick", json={"price": "10"}).status_code, 400)
                self.assertEqual(client.post("/api/demo/reset", json={}).status_code, 200)
                changed = client.post("/api/watchlist/KR:005930/history", json={
                    "all_time_high": "10", "history_through": previous_session("KR").isoformat(),
                    "confirmed_adjusted_complete": True})
                self.assertEqual(changed.status_code, 400)
                self.assertIn("실제 종목", changed.json()["detail"])
                self.assertEqual(c.metadata["KR:005930"]["source"], "kiwoom")


if __name__ == "__main__":
    unittest.main()
