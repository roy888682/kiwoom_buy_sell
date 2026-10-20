"""Realtime protocol tests: injectable sockets, no external network or orders."""

import asyncio
from datetime import datetime, timezone
import json
import unittest

from app.stream import KiwoomStream, StreamError


class Broker:
    env = "mock"
    _token = None
    _us_timestamp_tz = None

    async def authenticate(self):
        self._token = "private-test-token"


class Socket:
    def __init__(self, *, reject_group=None, timeout_group=None, warmup=None):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.reject_group = reject_group
        self.timeout_group = timeout_group
        self.warmup = warmup

    async def send(self, raw):
        packet = json.loads(raw)
        self.sent.append(packet)
        if packet["trnm"] == "LOGIN":
            self.push({"trnm": "LOGIN", "return_code": 0})
        elif packet["trnm"] == "REG":
            group = packet["grp_no"]
            if self.warmup:
                self.push(self.warmup)
            if group != self.timeout_group:
                self.push({"trnm": "REG", "return_code": 1 if group == self.reject_group else 0,
                           "return_msg": "private-test-token-must-not-appear"})

    def push(self, packet):
        self.incoming.put_nowait(json.dumps(packet))

    async def recv(self):
        result = await self.incoming.get()
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait(ConnectionError("socket closed"))


KR = {"market": "KR", "symbol": "005930", "exchange": "KRX"}
US = {"market": "US", "symbol": "AAPL", "exchange": "ND"}
NOW = datetime(2026, 10, 20, 1, 10, 10, tzinfo=timezone.utc)


def kr_tick(symbol="005930", price="-10200", time="101010"):
    return {"trnm": "REAL", "data": [{"type": "0B", "item": symbol,
            "values": {"10": price, "20": time}}]}


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.quotes = []
        self.gaps = []
        self.statuses = []
        self.ready = asyncio.Event()
        self.quoted = asyncio.Event()
        self.streams = []
        self.tasks = []

    async def asyncTearDown(self):
        for stream in self.streams:
            await stream.stop()
        for task in self.tasks:
            try:
                await task
            except (StreamError, asyncio.CancelledError):
                pass

    def build(self, socket=None, market="KR", instruments=None, on_quote=None, broker=None, **kwargs):
        self.socket = socket or Socket()
        self.uri = None

        async def connector(uri):
            self.uri = uri
            return self.socket

        async def quote(instrument, values):
            self.quotes.append((instrument, values))
            self.quoted.set()

        async def gap(message):
            self.gaps.append(message)

        async def status(values):
            self.statuses.append(values)
            if values["state"] == "ready":
                self.ready.set()

        stream = KiwoomStream(broker or Broker(), market, instruments or [KR], on_quote or quote, gap, status,
                              connector=connector, clock=lambda: NOW, **kwargs)
        self.streams.append(stream)
        return stream

    async def start(self, stream):
        task = asyncio.create_task(stream.run())
        self.tasks.append(task)
        await asyncio.wait_for(self.ready.wait(), timeout=1)
        return task

    async def test_all_batches_must_be_acknowledged_before_ready(self):
        symbols = [KR, {**KR, "symbol": "000660"}, {**KR, "symbol": "069500"}]
        stream = self.build(instruments=symbols, batch_size=2)
        await self.start(stream)
        regs = [packet for packet in self.socket.sent if packet["trnm"] == "REG"]
        self.assertEqual([len(p["data"][0]["item"]) for p in regs], [2, 1])
        self.assertEqual(regs[0]["data"][0]["type"], ["0B"])
        self.assertEqual([p["grp_no"] for p in regs], ["1", "2"])
        self.assertEqual(stream.admitted_count, 3)
        self.assertTrue(stream.ready)
        self.assertEqual(self.uri, "wss://mockapi.kiwoom.com:10000/api/dostk/websocket")

    async def test_subscription_limit_rejection_never_claims_full_coverage(self):
        stream = self.build(Socket(reject_group="2"), instruments=[KR, {**KR, "symbol": "000660"}], batch_size=1)
        with self.assertRaises(StreamError) as captured:
            await stream.run()
        self.assertEqual(stream.admitted_count, 1)
        self.assertFalse(stream.ready)
        self.assertFalse(any(s["state"] == "ready" for s in self.statuses))
        self.assertEqual(len(self.gaps), 1)
        self.assertNotIn("private-test-token", str(captured.exception) + str(self.gaps) + str(self.statuses))

    async def test_subscription_timeout_disarms(self):
        stream = self.build(Socket(timeout_group="1"), ack_timeout=0.01)
        with self.assertRaises(StreamError):
            await stream.run()
        self.assertFalse(stream.ready)
        self.assertEqual(len(self.gaps), 1)

    async def test_mismatched_registration_group_never_admits_symbols(self):
        class WrongGroup(Socket):
            async def send(self, raw):
                packet = json.loads(raw)
                if packet["trnm"] == "REG":
                    self.sent.append(packet)
                    self.push({"trnm": "REG", "return_code": 0, "grp_no": "wrong"})
                else:
                    await super().send(raw)

        stream = self.build(WrongGroup())
        with self.assertRaises(StreamError):
            await stream.run()
        self.assertEqual(stream.admitted_count, 0)
        self.assertFalse(stream.ready)

    async def test_ping_echo_and_domestic_quote_mapping(self):
        stream = self.build()
        await self.start(stream)
        self.socket.push({"trnm": "PING"})
        self.socket.push(kr_tick())
        await asyncio.wait_for(self.quoted.wait(), 1)
        self.assertIn({"trnm": "PING"}, self.socket.sent)
        instrument, quote = self.quotes[0]
        self.assertEqual(instrument["symbol"], "005930")
        self.assertEqual(quote["price"], "10200")
        self.assertEqual(quote["market_timestamp"], "2026-10-20T10:10:10+09:00")
        self.assertTrue(quote["timestamp_verified"])

    async def test_warmup_quotes_cannot_trade_before_all_subscriptions_ready(self):
        stream = self.build(Socket(warmup=kr_tick()))
        await self.start(stream)
        self.assertEqual(stream.pre_ready_quotes, 1)
        self.assertEqual(self.quotes, [])
        self.socket.push(kr_tick(price="10210"))
        await asyncio.wait_for(self.quoted.wait(), 1)
        self.assertEqual(len(self.quotes), 1)
        self.assertEqual(self.quotes[0][1]["price"], "10210")

    async def test_us_subscription_and_confirmed_local_timestamp(self):
        broker = Broker()
        broker._us_timestamp_tz = "America/New_York"
        stream = self.build(market="US", instruments=[US], broker=broker)
        await self.start(stream)
        reg = self.socket.sent[-1]
        self.assertEqual(reg["data"][0], {"item": [{"jmcode": "AAPL", "stex_tp": "ND"}], "type": ["FE"]})
        self.socket.push({"trnm": "REAL", "data": [{"type": "FE", "item": "AAPL", "stexTp": "ND",
            "values": {"10": "+210.1100", "22": "20261020", "20": "233010", "51020": "103010"}}]})
        await asyncio.wait_for(self.quoted.wait(), 1)
        quote = self.quotes[0][1]
        self.assertEqual(quote["market_timestamp"], "2026-10-20T10:30:10-04:00")
        self.assertEqual(quote["currency"], "USD")

    async def test_us_unconfirmed_timezone_keeps_timestamp_unverified(self):
        stream = self.build(market="US", instruments=[US])
        await self.start(stream)
        self.socket.push({"trnm": "REAL", "data": [{"type": "FE", "item": "AAPL", "stexTp": "ND",
            "values": {"10": "210", "22": "20261020", "20": "233010"}}]})
        await asyncio.wait_for(self.quoted.wait(), 1)
        self.assertFalse(self.quotes[0][1]["timestamp_verified"])
        self.assertIsNone(self.quotes[0][1]["market_timestamp"])

    async def test_malformed_active_frame_disarms(self):
        stream = self.build()
        task = await self.start(stream)
        self.socket.incoming.put_nowait("not valid JSON private-test-token")
        with self.assertRaises(StreamError):
            await asyncio.wait_for(task, 1)
        self.assertFalse(stream.ready)
        self.assertEqual(len(self.gaps), 1)
        self.assertNotIn("private-test-token", str(self.gaps))

    async def test_socket_loss_disarms_without_reconnect(self):
        stream = self.build()
        task = await self.start(stream)
        self.socket.incoming.put_nowait(ConnectionError("secret server diagnostic"))
        with self.assertRaises(StreamError):
            await asyncio.wait_for(task, 1)
        self.assertFalse(stream.ready)
        self.assertEqual(len(self.gaps), 1)
        self.assertEqual(len([p for p in self.socket.sent if p["trnm"] == "LOGIN"]), 1)

    async def test_bounded_queue_overflow_stops_instead_of_silently_dropping(self):
        stream = self.build(queue_size=1)
        task = await self.start(stream)
        packet = kr_tick()
        packet["data"].append(kr_tick(price="10201")["data"][0])
        self.socket.push(packet)
        with self.assertRaises(StreamError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(len(self.gaps), 1)
        self.assertIn("대기열", self.gaps[0])

    async def test_stop_does_not_cancel_callback_that_may_be_sending_order(self):
        began, finish = asyncio.Event(), asyncio.Event()
        completed = []

        async def slow_callback(instrument, quote):
            began.set()
            await finish.wait()
            completed.append(True)

        stream = self.build(on_quote=slow_callback)
        task = await self.start(stream)
        self.socket.push(kr_tick())
        await asyncio.wait_for(began.wait(), 1)
        stop_task = asyncio.create_task(stream.stop())
        await asyncio.sleep(0)
        self.assertFalse(stop_task.done())
        finish.set()
        await asyncio.wait_for(stop_task, 1)
        await asyncio.wait_for(task, 1)
        self.assertEqual(completed, [True])
        self.assertEqual(self.gaps, [])

    async def test_stop_before_scheduled_run_never_connects(self):
        stream = self.build()
        task = asyncio.create_task(stream.run())
        self.tasks.append(task)
        await stream.stop()
        await asyncio.wait_for(task, 1)
        self.assertIsNone(self.uri)
        self.assertFalse(stream.ready)
        self.assertEqual(self.gaps, [])

    async def test_unknown_symbol_and_invalid_timestamp_fail_closed(self):
        for packet in [kr_tick(symbol="999999"), kr_tick(time="invalid")]:
            self.ready.clear()
            stream = self.build()
            task = await self.start(stream)
            self.socket.push(packet)
            with self.assertRaises(StreamError):
                await asyncio.wait_for(task, 1)
            self.assertFalse(stream.ready)


if __name__ == "__main__":
    unittest.main()
