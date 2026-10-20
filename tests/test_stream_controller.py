"""Controller/feed integration with acknowledged synthetic streams, no sockets."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

from app.main import Controller
from app.marketdata import market_day, previous_session


class Broker:
    def __init__(self):
        self.submissions = []
        self.closed = False
        self.before_submit = None
        self.rows = [
            {"market": "KR", "symbol": "000001", "name": "New KR", "exchange": "KRX", "kind": "STOCK", "listing_date": previous_session("KR").isoformat()},
            {"market": "US", "symbol": "NEW", "name": "New US ETF", "exchange": "ND", "kind": "ETF", "listing_date": previous_session("US").isoformat()},
        ]

    async def universe(self, markets):
        return [row.copy() for row in self.rows if row["market"] in markets]

    async def history(self, instrument, today):
        high = "10000" if instrument["market"] == "KR" else "100"
        close = "9900" if instrument["market"] == "KR" else "99"
        return {"bars": [{"date": previous_session(instrument["market"]).isoformat(), "high": high, "close": close, "adjusted": True}], "history_complete": True}

    async def quote(self, instrument):
        return {"price": "10300" if instrument["market"] == "KR" else "103", "timestamp_verified": True, "market_timestamp": datetime.now(timezone.utc).isoformat()}

    async def orderable_cash(self, instrument, price):
        return {"available_cash": "10000000" if instrument["market"] == "KR" else "10000", "max_quantity": 100000}

    async def submit_order(self, instrument, side, quantity, limit_price):
        if self.before_submit:
            self.before_submit()
        self.submissions.append({"instrument": instrument, "side": side, "quantity": quantity, "limit_price": limit_price})
        return {"order_id": str(100 + len(self.submissions))}

    async def close(self):
        self.closed = True


class SyntheticStream:
    def __init__(self, broker, market, instruments, on_quote, on_gap, on_status):
        self.broker = broker
        self.market = market
        self.instruments = instruments
        self.on_quote = on_quote
        self.on_gap = on_gap
        self.on_status = on_status
        self.ready = False
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self):
        self.started.set()
        await self.stopped.wait()

    async def acknowledge(self):
        self.ready = True
        await self.on_status({"state": "ready", "requested": len(self.instruments), "admitted": len(self.instruments)})

    async def emit(self, price, timestamp=None, instrument=None):
        await self.on_quote(instrument or self.instruments[0], {
            "price": str(price), "timestamp_verified": True,
            "market_timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        })

    async def gap(self, message="synthetic disconnect"):
        self.ready = False
        await self.on_gap(message)

    async def stop(self):
        self.ready = False
        self.stopped.set()


class ControllerStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="kiwoom-stream-controller-")
        self.broker = Broker()
        self.controller = Controller(self.directory.name, broker=self.broker)
        self.stream_patch = patch("app.stream.KiwoomStream", SyntheticStream)
        self.stream_patch.start()
        self.session_patch = patch("app.main.session_open", return_value=True)
        self.session_patch.start()

    async def asyncTearDown(self):
        try:
            await self.controller.close()
        finally:
            self.session_patch.stop()
            self.stream_patch.stop()
            self.directory.cleanup()

    def add_verified(self, market="KR"):
        row = next(row for row in self.broker.rows if row["market"] == market)
        c = self.controller
        item = c.add(row["market"], row["symbol"], row["name"], source="kiwoom", exchange=row["exchange"], listing_date=row["listing_date"])
        c.apply_history(item, {
            "all_time_high": "10000" if market == "KR" else "100",
            "history_through": previous_session(market), "history_verified": True,
        }, provenance="provider")
        return item

    def enable_live(self):
        c = self.controller
        c.mode = "live"
        c.running = True
        c.scan_enabled = True
        c.portfolio.cash = {"KRW": Decimal("10000000"), "USD": Decimal("10000")}

    async def test_ready_state_is_normalized_and_resets_price_and_time_baseline(self):
        c = self.controller
        item = self.add_verified()
        self.enable_live()
        item.previous_price = Decimal("10000")
        item.current_price = Decimal("10100")
        c.last_ticks[item.key] = datetime.now(timezone.utc)
        await c.start_feeds()
        stream = c.feeds["KR"]
        await stream.acknowledge()
        self.assertEqual(c.feed_status["KR"]["status"], "ready")
        self.assertEqual(c.feed_status["KR"]["requested"], 1)
        self.assertEqual(c.feed_status["KR"]["admitted"], 1)
        self.assertIsNone(item.previous_price)
        self.assertIsNone(item.current_price)
        self.assertNotIn(item.key, c.last_ticks)
        # An old below-threshold sample cannot be paired with the first quote
        # after admission to manufacture an entry.
        await stream.emit("10300")
        self.assertEqual(item.current_price, Decimal("10300"))
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(c.portfolio.orders, {})

    async def test_stream_gap_stops_engine_and_scanner_and_clears_all_baselines(self):
        c = self.controller
        kr = self.add_verified("KR")
        us = self.add_verified("US")
        self.enable_live()
        await c.start_feeds()
        for stream in c.feeds.values():
            await stream.acknowledge()
        await c.feeds["KR"].emit("10100")
        await c.feeds["US"].emit("101")
        c.scanner["status"] = "running"
        await c.feeds["KR"].gap("KR synthetic disconnect")
        self.assertFalse(c.running)
        self.assertFalse(c.scan_enabled)
        self.assertEqual(c.scanner["status"], "idle")
        self.assertIn("시세 공백", c.latest_error)
        self.assertTrue(all(item.current_price is None and item.previous_price is None for item in (kr, us)))
        await c.feeds["US"].emit("103")
        self.assertEqual(c.portfolio.orders, {})
        self.assertEqual(self.broker.submissions, [])
        persisted = c.store.load()
        self.assertIsNone(persisted["instruments"][kr.key]["current_price"])
        self.assertIsNone(persisted["instruments"][us.key]["current_price"])

    async def test_live_entry_waits_for_every_requested_market_feed(self):
        c = self.controller
        kr = self.add_verified("KR")
        self.add_verified("US")
        self.enable_live()
        await c.start_feeds()
        await c.feeds["KR"].acknowledge()
        self.assertFalse(c.feeds["US"].ready)
        await c.feeds["KR"].emit("10100")
        await c.feeds["KR"].emit("10200")
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(c.portfolio.orders, {})
        await c.feeds["US"].acknowledge()
        await c.feeds["KR"].emit("10100")
        await c.feeds["KR"].emit("10200")
        self.assertEqual(len(self.broker.submissions), 1)
        order = next(iter(c.portfolio.orders.values()))
        self.assertEqual(order.key, kr.key)
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.quantity, 292)
        self.assertEqual(order.filled_quantity, 0)
        self.assertEqual(c.portfolio.positions, {})

    async def test_reversed_quote_is_ignored_and_only_order_intent_is_saved(self):
        c = self.controller
        item = self.add_verified()
        self.enable_live()
        await c.start_feeds()
        stream = c.feeds["KR"]
        await stream.acknowledge()
        now = datetime.now(timezone.utc)
        with patch.object(c, "save", wraps=c.save) as saved:
            await stream.emit("10100", now)
            self.assertEqual(saved.call_count, 0)
            await stream.emit("10200", now - timedelta(seconds=1))
            self.assertEqual(item.current_price, Decimal("10100"))
            self.assertEqual(c.last_ticks[item.key], now)
            self.assertEqual(self.broker.submissions, [])
            self.assertEqual(saved.call_count, 0)
            before_network = []

            def capture_intent():
                snapshot = c.store.load()
                before_network.append(snapshot["portfolios"]["live"]["orders"])

            self.broker.before_submit = capture_intent
            await stream.emit("10200", now + timedelta(milliseconds=1))
            self.assertGreaterEqual(saved.call_count, 2)
        self.assertEqual(len(before_network), 1)
        pending_intent = next(iter(before_network[0].values()))
        self.assertEqual(pending_intent["status"], "unknown")
        self.assertEqual(pending_intent["filled_quantity"], 0)
        self.assertEqual(pending_intent["quantity"], 292)
        self.assertEqual(pending_intent["side"], "BUY")
        self.assertEqual(len(self.broker.submissions), 1)
        persisted_order = next(iter(c.store.load()["portfolios"]["live"]["orders"].values()))
        self.assertEqual(persisted_order["status"], "pending")
        self.assertEqual(persisted_order["filled_quantity"], 0)

    async def test_full_scan_verifies_new_listing_history_then_subscribes_without_first_quote_buy(self):
        c = self.controller
        c.running = True
        c.scan_enabled = True
        await c.scan("ALL")
        self.assertEqual(c.scanner["status"], "completed", c.scanner)
        self.assertEqual(c.scanner["total"], 2)
        self.assertEqual(c.scanner["processed"], 2)
        self.assertEqual(c.scanner["candidates"], 2)
        self.assertEqual(c.scanner["failed"], 0)
        self.assertEqual(set(c.feeds), {"KR", "US"})
        self.assertEqual(set(c.instruments), {"KR:000001", "US:NEW"})
        self.assertEqual(c.last_scan_days, {market: market_day(market).isoformat() for market in ("KR", "US")})
        for market, stream in c.feeds.items():
            item = c.instruments[f"{market}:{stream.instruments[0]['symbol']}"]
            self.assertTrue(item.history_verified)
            self.assertEqual(item.history_through, previous_session(market))
            self.assertEqual(c.metadata[item.key]["history_source"], "provider")
            await stream.acknowledge()
            self.assertIsNone(item.current_price)
            await stream.emit("10300" if market == "KR" else "103")
        self.assertEqual(c.portfolio.orders, {})
        self.assertEqual(c.portfolio.positions, {})
        self.assertEqual(self.broker.submissions, [])


if __name__ == "__main__":
    unittest.main()
