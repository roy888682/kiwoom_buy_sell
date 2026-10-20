"""Local HTTP flows; all state is isolated and all broker I/O is synthetic."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.kiwoom import OrderOutcomeUnknown
from app.main import create_app
from app.marketdata import market_day, previous_session


class UnknownBroker:
    """A broker whose order response is lost; never opens a network client."""
    def __init__(self):
        self.submissions = 0
        self.cash_requests = 0
        self.closed = False

    async def open_orders(self, market):
        return []

    async def holdings(self, market):
        return []

    async def universe(self, markets):
        return []

    async def cash(self, market):
        return {"currency": "KRW" if market == "KR" else "USD", "available_cash": "10000000" if market == "KR" else "10000"}

    async def orderable_cash(self, instrument, price):
        self.cash_requests += 1
        return {"available_cash": "10000000" if instrument["market"] == "KR" else "10000", "max_quantity": 100000}

    async def submit_order(self, instrument, side, quantity, limit_price):
        self.submissions += 1
        raise OrderOutcomeUnknown("synthetic lost order response")

    async def close(self):
        self.closed = True


class ReadyFeed:
    ready = True

    async def stop(self):
        self.ready = False


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="kiwoom-app-test-")
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {
            "KIWOOM_APP_KEY": "", "KIWOOM_SECRET_KEY": "",
            "ALLOW_LIVE_TRADING": "false", "KIWOOM_ENV": "mock",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def client(self, broker=None):
        return TestClient(create_app(self.directory.name, broker=broker))

    def post(self, client, path, body=None):
        return client.post(path, json=body or {})

    def reset_and_start(self, client):
        self.assertEqual(self.post(client, "/api/demo/reset").status_code, 200)
        started = self.post(client, "/api/engine/start", {"mode": "paper"})
        self.assertEqual(started.status_code, 200, started.text)
        return started.json()

    def wait_scan(self, client):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            scanner = client.get("/api/state").json()["scanner"]
            if scanner["status"] not in {"running", "idle"}:
                return scanner
            time.sleep(0.01)
        self.fail("Scanner did not reach a terminal outcome within 5 seconds")

    def test_paper_scenario_fills_both_currencies_and_stop_uses_real_average(self):
        with self.client() as client:
            started = self.reset_and_start(client)
            self.assertTrue(started["running"])
            self.assertEqual(started["orders"], [])
            result = self.post(client, "/api/demo/scenario")
            self.assertEqual(result.status_code, 200, result.text)
            state = result.json()
            self.assertEqual(len(state["orders"]), 2)
            positions = {p["key"]: p for p in state["positions"]}
            self.assertEqual(set(positions), {"KR:000000", "US:DEMO"})
            self.assertEqual(positions["KR:000000"]["quantity"], 292)
            self.assertEqual(Decimal(positions["KR:000000"]["average_price"]), Decimal("10200"))
            self.assertEqual(Decimal(positions["KR:000000"]["stop_price"]), Decimal("9690"))
            self.assertEqual(positions["US:DEMO"]["quantity"], 29)
            self.assertEqual(Decimal(state["cash"]["KRW"]), Decimal("7006708"))
            self.assertEqual(Decimal(state["cash"]["USD"]), Decimal("7027.21"))
            self.assertTrue(all(order["status"] == "filled" and order["filled_quantity"] == order["quantity"] for order in state["orders"]))

            stopped_position = self.post(client, "/api/watchlist/KR:000000/tick", {"price": "9690"})
            self.assertEqual(stopped_position.status_code, 200, stopped_position.text)
            state = stopped_position.json()
            self.assertEqual([p["key"] for p in state["positions"]], ["US:DEMO"])
            self.assertEqual(state["orders"][0]["side"], "SELL")
            self.assertEqual(state["orders"][0]["reason"], "stop_loss")
            self.assertEqual(state["orders"][0]["quantity"], 292)
            self.assertEqual(Decimal(state["cash"]["KRW"]), Decimal("9822040.6"))
            # A rebound crosses the threshold, but the filled stop starts cooldown.
            self.post(client, "/api/watchlist/KR:000000/tick", {"price": "10100"})
            rebound = self.post(client, "/api/watchlist/KR:000000/tick", {"price": "10200"}).json()
            self.assertEqual(len(rebound["orders"]), 3)
            self.assertNotIn("KR:000000", {p["key"] for p in rebound["positions"]})
            self.assertEqual(Decimal(rebound["cash"]["USD"]), Decimal("7027.21"))

    def test_repeated_demo_crossings_do_not_pyramid_or_duplicate(self):
        with self.client() as client:
            self.reset_and_start(client)
            first = self.post(client, "/api/demo/scenario").json()
            second = self.post(client, "/api/demo/scenario").json()
            self.assertEqual(len(second["orders"]), 2)
            self.assertEqual(first["cash"], second["cash"])
            self.assertEqual(first["positions"], second["positions"])

    def test_settings_and_fills_survive_restart_but_running_and_samples_do_not(self):
        with self.client() as client:
            updated = client.put("/api/settings", json={"breakout_percent": "-2", "max_positions": 8, "reentry_cooldown_minutes": 120})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.reset_and_start(client)
            result = self.post(client, "/api/demo/scenario")
            self.assertEqual(len(result.json()["orders"]), 2)
            expected_cash = result.json()["cash"]
            expected_quantities = {p["key"]: p["quantity"] for p in result.json()["positions"]}
        with self.client() as client:
            state = client.get("/api/state").json()
            self.assertFalse(state["running"])
            self.assertFalse(state["armed"])
            self.assertEqual(state["settings"]["breakout_percent"], "-2")
            self.assertEqual(state["settings"]["max_positions"], 8)
            self.assertEqual(state["settings"]["reentry_cooldown_minutes"], 120)
            self.assertEqual(state["cash"], expected_cash)
            self.assertEqual({p["key"]: p["quantity"] for p in state["positions"]}, expected_quantities)
            self.assertTrue(all(item["current_price"] is None and item["previous_price"] is None for item in state["watchlist"]))
            self.assertEqual(len(state["orders"]), 2)

    def test_setting_mutation_demo_reset_and_history_are_blocked_during_trading(self):
        with self.client() as client:
            self.reset_and_start(client)
            self.assertEqual(client.put("/api/settings", json={"stop_loss_percent": "8"}).status_code, 400)
            self.assertEqual(self.post(client, "/api/demo/reset").status_code, 400)
            history = {"all_time_high": "10000", "history_through": previous_session("KR").isoformat(), "confirmed_adjusted_complete": True}
            self.assertEqual(self.post(client, "/api/watchlist/KR:000000/history", history).status_code, 400)
            self.assertEqual(self.post(client, "/api/engine/stop").status_code, 200)
            self.assertEqual(client.put("/api/settings", json={"stop_loss_percent": "8"}).status_code, 200)

    def test_first_price_above_threshold_does_not_buy(self):
        with self.client() as client:
            self.reset_and_start(client)
            first = self.post(client, "/api/watchlist/KR:000000/tick", {"price": "11000"}).json()
            second = self.post(client, "/api/watchlist/KR:000000/tick", {"price": "12000"}).json()
            self.assertEqual(first["orders"], [])
            self.assertEqual(second["orders"], [])
            self.post(client, "/api/watchlist/KR:000000/tick", {"price": "10100"})
            crossed = self.post(client, "/api/watchlist/KR:000000/tick", {"price": "10200"}).json()
            self.assertEqual(len(crossed["orders"]), 1)

    def test_input_validation_and_unknown_instrument(self):
        with self.client() as client:
            self.post(client, "/api/demo/reset")
            for setting in ({"allocation_percent": "101"}, {"breakout_percent": "-100"}, {"breakout_percent": "1e999999"}, {"max_positions": 0}, {"unknown": True}):
                with self.subTest(setting=setting):
                    self.assertEqual(client.put("/api/settings", json=setting).status_code, 400)
            for price in ("0", "-1", "NaN", "Infinity"):
                with self.subTest(price=price):
                    result = self.post(client, "/api/watchlist/KR:000000/tick", {"price": price})
                    self.assertEqual(result.status_code, 400, result.text)
            self.assertEqual(self.post(client, "/api/watchlist", {"market": "KR", "symbol": "bad"}).status_code, 400)
            self.assertEqual(self.post(client, "/api/watchlist/US:MISSING/tick", {"price": "12"}).status_code, 404)
            self.assertEqual(self.post(client, "/api/engine/start", {"mode": "paper", "surprise": True}).status_code, 422)
            self.assertEqual(client.put("/api/settings", content="{not-json", headers={"content-type": "application/json"}).status_code, 400)

    def test_security_headers_origin_host_and_form_posts(self):
        with self.client() as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            self.assertEqual(health.headers["cache-control"], "no-store")
            self.assertEqual(health.headers["x-content-type-options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", health.headers["content-security-policy"])
            denied = client.post("/api/demo/reset", json={}, headers={"origin": "https://evil.example"})
            self.assertEqual(denied.status_code, 403)
            same_origin = client.post("/api/demo/reset", json={}, headers={"origin": "http://testserver"})
            self.assertEqual(same_origin.status_code, 200)
            self.assertEqual(client.post("/api/engine/stop", data={"mode": "live"}).status_code, 415)
            self.assertEqual(client.get("/api/state", headers={"host": "evil.example"}).status_code, 400)

    def test_csv_scan_reports_only_imported_coverage_and_never_trades(self):
        with self.client() as client:
            day = previous_session("KR").isoformat()
            csv = "market,symbol,name,date,high,close,adjusted,complete_from_listing,listing_date\n" + "\n".join(
                f"KR,{symbol},신규 테스트,{day},10000,9900,true,true,{day}" for symbol in ("000001", "000002"))
            imported = client.post("/api/universe/import?market=KR", content=csv, headers={"content-type": "text/csv"})
            self.assertEqual(imported.status_code, 200, imported.text)
            self.assertEqual(len(imported.json()["watchlist"]), 2)
            self.assertTrue(all(i["history_verified"] for i in imported.json()["watchlist"]))
            started = self.post(client, "/api/scanner/start", {"market": "KR"})
            self.assertEqual(started.status_code, 200, started.text)
            scanner = self.wait_scan(client)
            self.assertEqual(scanner["status"], "completed", scanner)
            self.assertEqual(scanner["processed"], 2)
            self.assertEqual(scanner["total"], 2)
            self.assertEqual(scanner["candidates"], 2)
            self.assertIn("CSV", scanner["coverage_message"])
            self.assertIn("보장", scanner["coverage_message"])
            state = client.get("/api/state").json()
            self.assertFalse(state["running"])
            self.assertEqual(state["orders"], [])
            self.assertEqual(state["positions"], [])

    def test_scan_without_universe_diagnoses_missing_credentials(self):
        with self.client() as client:
            response = self.post(client, "/api/scanner/start", {"market": "ALL"})
            self.assertEqual(response.status_code, 200)
            scanner = self.wait_scan(client)
            self.assertEqual(scanner["status"], "error")
            self.assertIn("키움", scanner["last_error"])
            self.assertEqual(scanner["processed"], 0)
            self.assertEqual(client.get("/api/state").json()["orders"], [])

    def test_bad_csv_does_not_mutate_universe(self):
        with self.client() as client:
            for text in ("invalid,csv\nno,data", "market,symbol,name,date,high,close,adjusted,complete_from_listing\nKR,000001,Test,2026-10-19,NaN,99,true,true"):
                result = client.post("/api/universe/import?market=KR", content=text, headers={"content-type": "text/csv"})
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(client.get("/api/state").json()["watchlist"], [])

    def test_live_arming_needs_environment_confirmation_and_actual_credentials(self):
        with self.client() as client:
            for payload in ({"mode": "live"}, {"mode": "live", "confirmation": "실거래 시작"}):
                result = self.post(client, "/api/engine/start", payload)
                self.assertEqual(result.status_code, 400)
            with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "mock"}):
                result = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(result.status_code, 400)
                self.assertIn("KIWOOM_ENV", result.json()["detail"])
            with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "live"}):
                result = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(result.status_code, 400)
                self.assertIn("앱 키", result.json()["detail"])
            state = client.get("/api/state").json()
            self.assertFalse(state["armed"])
            self.assertFalse(state["running"])
            self.assertFalse(state["broker"]["configured"])
            self.assertEqual(state["orders"], [])

    def test_unknown_live_order_survives_restart_and_prevents_rearming(self):
        broker = UnknownBroker()
        with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "live"}):
            with self.client(broker=broker) as client:
                c = client.app.state.controller
                item = c.add("KR", "000001", "Synthetic", source="kiwoom", exchange="KRX")
                started = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(started.status_code, 200, started.text)
                c.apply_history(item, {"all_time_high": "10000", "history_through": previous_session("KR"), "history_verified": True}, provenance="provider")
                c.feeds["KR"] = ReadyFeed()
                quote = {"timestamp_verified": True, "market_timestamp": datetime.now(timezone.utc).isoformat()}
                with patch("app.main.session_open", return_value=True):
                    client.portal.call(partial(c.tick, item.key, "10100", source="kiwoom_ws", quote=quote))
                    with self.assertRaises(OrderOutcomeUnknown):
                        client.portal.call(partial(c.tick, item.key, "10200", source="kiwoom_ws", quote=quote))
                state = client.get("/api/state").json()
                self.assertEqual(broker.submissions, 1)
                self.assertFalse(state["running"])
                self.assertEqual(len(state["orders"]), 1)
                self.assertEqual(state["orders"][0]["status"], "unknown")
                self.assertEqual(state["orders"][0]["filled_quantity"], 0)
                self.assertEqual(state["positions"], [])
                self.assertLess(Decimal(state["cash"]["KRW"]), Decimal("10000000"))
                order_id = state["orders"][0]["id"]
                self.assertGreater(c.portfolio.orders[order_id].reserved_cash, 0)
                refused = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(refused.status_code, 400)
            self.assertTrue(broker.closed)
            next_broker = UnknownBroker()
            with self.client(broker=next_broker) as client:
                state = client.get("/api/state").json()
                self.assertFalse(state["running"])
                self.assertFalse(state["armed"])
                self.assertEqual(state["orders"][0]["status"], "unknown")
                self.assertEqual(next_broker.submissions, 0)
                refused = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(refused.status_code, 400)
                self.assertIn("응답 불명", refused.json()["detail"])

    def test_readding_manual_history_cannot_make_it_live_eligible(self):
        broker = UnknownBroker()
        with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "live"}):
            with self.client(broker=broker) as client:
                self.post(client, "/api/watchlist", {"market": "KR", "symbol": "000001", "name": "Synthetic"})
                history = {"all_time_high": "10000", "history_through": previous_session("KR").isoformat(), "confirmed_adjusted_complete": True}
                self.assertEqual(self.post(client, "/api/watchlist/KR:000001/history", history).status_code, 200)
                self.assertEqual(self.post(client, "/api/watchlist", {"market": "KR", "symbol": "000001", "name": "Renamed"}).status_code, 200)
                c = client.app.state.controller
                self.assertEqual(c.metadata["KR:000001"]["history_source"], "demo")
                self.assertEqual(c.metadata["KR:000001"]["source"], "demo")
                started = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(started.status_code, 200, started.text)
                c.feeds["KR"] = ReadyFeed()
                quote = {"timestamp_verified": True, "market_timestamp": datetime.now(timezone.utc).isoformat()}
                with patch("app.main.session_open", return_value=True):
                    client.portal.call(partial(c.tick, "KR:000001", "10100", source="kiwoom_ws", quote=quote))
                    client.portal.call(partial(c.tick, "KR:000001", "10200", source="kiwoom_ws", quote=quote))
                state = client.get("/api/state").json()
                self.assertEqual(state["orders"], [])
                self.assertEqual(broker.submissions, 0)

    def test_start_is_blocked_until_stopping_worker_finishes(self):
        with self.client() as client:
            c = client.app.state.controller
            original_worker = c.worker

            async def configure():
                entered = asyncio.Event()
                release = asyncio.Event()

                async def blocked_worker():
                    entered.set()
                    await release.wait()

                c.worker = blocked_worker
                return entered, release

            entered, release = client.portal.call(configure)
            self.assertEqual(self.post(client, "/api/engine/start", {"mode": "paper"}).status_code, 200)
            client.portal.call(entered.wait)
            original_task = c.worker_task
            stopping = client.portal.start_task_soon(c.stop)
            try:
                async def wait_stopping():
                    while not c.stopping:
                        await asyncio.sleep(0)

                client.portal.call(wait_stopping)
                self.assertFalse(stopping.done())
                refused = self.post(client, "/api/engine/start", {"mode": "paper"})
                self.assertEqual(refused.status_code, 400)
                self.assertIn("정지", refused.json()["detail"])
                self.assertIs(c.worker_task, original_task)
                self.assertFalse(c.running)
            finally:
                client.portal.call(release.set)
                stopping.result(timeout=5)
                c.worker = original_worker
            self.assertTrue(original_task.done())
            self.assertFalse(c.stopping)
            restarted = self.post(client, "/api/engine/start", {"mode": "paper"})
            self.assertEqual(restarted.status_code, 200, restarted.text)

    def test_open_live_order_blocks_switch_to_paper(self):
        # Seed a submitted intent without any broker I/O. A stopped live mode
        # must remain available for reconciliation while this order is open.
        from app.strategy import Decision, Strategy
        with self.client() as client:
            c = client.app.state.controller
            item = c.add("KR", "000001", "Synthetic", source="kiwoom", exchange="KRX")
            c.mode = "live"
            c.portfolio.cash["KRW"] = Decimal("1000000")
            order = Strategy(c.settings, c.portfolio).submit_order(item, Decision("KR", "000001", "BUY", 10, "10000", "breakout"))
            order.status = "unknown"
            c.save()
            refused = self.post(client, "/api/engine/start", {"mode": "paper"})
            self.assertEqual(refused.status_code, 400)
            self.assertIn("실계좌", refused.json()["detail"])
            state = client.get("/api/state").json()
            self.assertEqual(state["mode"], "live")
            self.assertFalse(state["running"])
            self.assertEqual(state["orders"][0]["status"], "unknown")
            self.assertGreater(c.portfolio.orders[order.id].reserved_cash, 0)

    def test_csv_rechecks_running_state_after_receiving_body(self):
        with self.client() as client:
            c = client.app.state.controller
            day = previous_session("KR").isoformat()
            text = "market,symbol,name,date,high,close,adjusted,complete_from_listing\n" + f"KR,000001,Synthetic,{day},10000,9900,true,true"

            def streamed_body():
                # The endpoint checked running=False before awaiting the body.
                # Simulate the engine starting during that await.
                c.running = True
                yield text.encode("utf-8")

            try:
                result = client.post("/api/universe/import?market=KR", content=streamed_body(), headers={"content-type": "text/csv"})
            finally:
                c.running = False
            self.assertEqual(result.status_code, 400, result.text)
            self.assertEqual(client.get("/api/state").json()["watchlist"], [])

    def test_rest_unready_feed_or_stale_adjustment_cannot_submit_live_entry(self):
        broker = UnknownBroker()
        with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "live"}):
            with self.client(broker=broker) as client:
                c = client.app.state.controller
                item = c.add("KR", "000001", "Synthetic", source="kiwoom", exchange="KRX")
                self.assertEqual(self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"}).status_code, 200)
                c.apply_history(item, {"all_time_high": "10000", "history_through": previous_session("KR"), "history_verified": True}, provenance="provider")
                quote = {"timestamp_verified": True, "market_timestamp": datetime.now(timezone.utc).isoformat()}
                with patch("app.main.session_open", return_value=True):
                    for source, ready, adjustment in (
                        ("kiwoom", True, market_day("KR").isoformat()),
                        ("kiwoom_ws", False, market_day("KR").isoformat()),
                        ("kiwoom_ws", True, "2000-01-01"),
                    ):
                        with self.subTest(source=source, ready=ready, adjustment=adjustment):
                            feed = ReadyFeed()
                            feed.ready = ready
                            c.feeds["KR"] = feed
                            c.metadata[item.key]["adjustment_as_of"] = adjustment
                            item.previous_price = item.current_price = None
                            client.portal.call(partial(c.tick, item.key, "10100", source=source, quote=quote))
                            client.portal.call(partial(c.tick, item.key, "10200", source=source, quote=quote))
                self.assertEqual(broker.submissions, 0)
                self.assertEqual(client.get("/api/state").json()["orders"], [])

    def test_malformed_live_quote_timestamps_fail_closed_and_clear_baseline(self):
        broker = UnknownBroker()
        with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "live"}):
            with self.client(broker=broker) as client:
                c = client.app.state.controller
                item = c.add("KR", "000001", "Synthetic", source="kiwoom", exchange="KRX")
                self.assertEqual(self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"}).status_code, 200)
                c.apply_history(item, {"all_time_high": "10000", "history_through": previous_session("KR"), "history_verified": True}, provenance="provider")
                c.feeds["KR"] = ReadyFeed()
                fresh = {"timestamp_verified": True, "market_timestamp": datetime.now(timezone.utc).isoformat()}
                bad_quotes = [
                    {"timestamp_verified": False},
                    {"timestamp_verified": True, "market_timestamp": "not-a-time"},
                    {"timestamp_verified": True, "market_timestamp": datetime.now().isoformat()},
                    {"timestamp_verified": True, "market_timestamp": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()},
                ]
                with patch("app.main.session_open", return_value=True):
                    for bad in bad_quotes:
                        with self.subTest(quote=bad):
                            item.previous_price = item.current_price = None
                            client.portal.call(partial(c.tick, item.key, "10100", source="kiwoom_ws", quote=fresh))
                            client.portal.call(partial(c.tick, item.key, "10200", source="kiwoom_ws", quote=bad))
                            self.assertIsNone(item.current_price)
                            client.portal.call(partial(c.tick, item.key, "10300", source="kiwoom_ws", quote=fresh))
                self.assertEqual(broker.submissions, 0)
                self.assertEqual(client.get("/api/state").json()["orders"], [])

    def test_stale_live_quote_resets_crossing_and_does_not_submit(self):
        broker = UnknownBroker()
        with patch.dict(os.environ, {"ALLOW_LIVE_TRADING": "true", "KIWOOM_ENV": "live"}):
            with self.client(broker=broker) as client:
                c = client.app.state.controller
                item = c.add("KR", "000001", "Synthetic", source="kiwoom", exchange="KRX")
                started = self.post(client, "/api/engine/start", {"mode": "live", "confirmation": "실거래 시작"})
                self.assertEqual(started.status_code, 200, started.text)
                c.apply_history(item, {"all_time_high": "10000", "history_through": previous_session("KR"), "history_verified": True}, provenance="provider")
                c.feeds["KR"] = ReadyFeed()
                fresh = {"timestamp_verified": True, "market_timestamp": datetime.now(timezone.utc).isoformat()}
                stale = {"timestamp_verified": True, "market_timestamp": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
                with patch("app.main.session_open", return_value=True):
                    client.portal.call(partial(c.tick, item.key, "10100", source="kiwoom_ws", quote=fresh))
                    client.portal.call(partial(c.tick, item.key, "10200", source="kiwoom_ws", quote=stale))
                    self.assertIsNone(item.current_price)
                    client.portal.call(partial(c.tick, item.key, "10300", source="kiwoom_ws", quote=fresh))
                state = client.get("/api/state").json()
                self.assertEqual(state["orders"], [])
                self.assertEqual(broker.submissions, 0)


if __name__ == "__main__":
    unittest.main()
