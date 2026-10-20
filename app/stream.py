"""Acknowledged, bounded Kiwoom realtime feeds (0B / FE).

The broker determines subscription limits. Every requested batch must be
acknowledged before this feed becomes ready; a rejected batch stops the feed.
There is deliberately no automatic reconnection or order retry. On any gap,
the controller must disarm and clear its previous-price baselines.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import os
import re
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .kiwoom import _market, number


class StreamError(RuntimeError):
    """A sanitized protocol/data failure; never contains authentication data."""


class KiwoomStream:
    def __init__(
        self, broker, market: str, instruments: list[dict],
        on_quote: Callable[[dict, dict], Awaitable[None]],
        on_gap: Callable[[str], Awaitable[None]],
        on_status: Callable[[dict], Awaitable[None]] | None = None,
        *, connector=None, batch_size: int = 50, queue_size: int = 1000,
        ack_timeout: float = 15, clock: Callable[[], datetime] | None = None,
    ):
        if market not in {"KR", "US"}:
            raise ValueError("실시간 시장은 KR 또는 US여야 합니다.")
        if not isinstance(batch_size, int) or batch_size < 1 or batch_size > 1000:
            raise ValueError("실시간 등록 묶음 크기는 1~1000이어야 합니다.")
        if not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError("실시간 대기열 크기는 양수여야 합니다.")
        unique = {}
        for instrument in instruments:
            item_market, symbol, exchange = _market(instrument)
            if item_market != market:
                raise ValueError("다른 시장의 종목을 같은 실시간 연결에 등록할 수 없습니다.")
            unique[(symbol, exchange)] = {**instrument, "symbol": symbol, "exchange": exchange}
        if not unique:
            raise ValueError("등록할 실시간 종목이 없습니다.")
        if (len(unique) + batch_size - 1) // batch_size > 9999:
            raise ValueError("실시간 등록 그룹 수를 초과합니다.")
        self.broker = broker
        self.market = market
        self.instruments = list(unique.values())
        self._by_key = unique
        self._by_symbol: dict[str, list[dict]] = {}
        for instrument in self.instruments:
            self._by_symbol.setdefault(instrument["symbol"], []).append(instrument)
        self.on_quote = on_quote
        self.on_gap = on_gap
        self.on_status = on_status
        self._connector = connector or self._default_connect
        self.batch_size = batch_size
        self.ack_timeout = ack_timeout
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._send_lock = asyncio.Lock()
        self._pending_ack = None
        self._socket = None
        self._reader_task = None
        self._processor_task = None
        self._running = False
        self._stopping = False
        self.ready = False
        self.requested_count = len(self.instruments)
        self.admitted_count = 0
        self.pre_ready_quotes = 0

    async def _default_connect(self, uri):
        from websockets.asyncio.client import connect
        return await connect(uri, open_timeout=self.ack_timeout, close_timeout=5,
                             ping_interval=None, max_queue=16, max_size=1024 * 1024)

    async def _status(self, state: str, message: str = ""):
        if self.on_status:
            await self.on_status({
                "state": state, "market": self.market, "ready": self.ready,
                "requested": self.requested_count, "admitted": self.admitted_count,
                "pre_ready_quotes": self.pre_ready_quotes, "message": message,
            })

    async def _send(self, packet: dict):
        async with self._send_lock:
            await self._socket.send(json.dumps(packet, ensure_ascii=False))

    async def _request_ack(self, packet: dict):
        future = asyncio.get_running_loop().create_future()
        self._pending_ack = (packet["trnm"], future, packet.get("grp_no"))
        try:
            await self._send(packet)
            try:
                await asyncio.wait_for(future, timeout=self.ack_timeout)
            except TimeoutError:
                raise StreamError("키움 실시간 로그인/등록 응답 시간이 초과되었습니다.") from None
        finally:
            self._pending_ack = None

    async def run(self):
        """Run until explicitly stopped or fail closed on a gap/protocol error.

        The caller owns this coroutine's task and must catch StreamError.
        stop() closes reception but waits for an in-flight quote callback (which
        can be sending an order) to finish rather than canceling it.
        """
        if self._running:
            raise StreamError("실시간 연결이 이미 실행 중입니다.")
        # stop() may run before a just-scheduled run() gets its first turn.
        # A stopped feed is single-use; the controller creates a fresh instance.
        if self._stopping:
            await self._status("stopped")
            return
        self._running = True
        self._stopping = False
        self.ready = False
        self.admitted_count = 0
        self.pre_ready_quotes = 0
        self._reader_task = self._processor_task = None
        while not self._queue.empty():
            self._queue.get_nowait()
        failed = False
        try:
            await self._status("connecting")
            await self.broker.authenticate()
            if self._stopping:
                return
            token = self.broker._token  # Internal-only transfer; never returned or logged.
            if not isinstance(token, str) or not token:
                raise StreamError("실시간 인증 토큰이 없습니다.")
            host = "api.kiwoom.com" if self.broker.env == "live" else "mockapi.kiwoom.com"
            endpoint = "dostk" if self.market == "KR" else "us"
            self._socket = await self._connector(f"wss://{host}:10000/api/{endpoint}/websocket")
            if self._stopping:
                return
            self._reader_task = asyncio.create_task(self._receive())
            await self._request_ack({"trnm": "LOGIN", "token": token})
            await self._status("admitting")
            for offset in range(0, len(self.instruments), self.batch_size):
                if self._stopping:
                    return
                batch = self.instruments[offset:offset + self.batch_size]
                items = ([item["symbol"] for item in batch] if self.market == "KR" else
                         [{"jmcode": item["symbol"], "stex_tp": item["exchange"]} for item in batch])
                await self._request_ack({
                    "trnm": "REG", "grp_no": str(offset // self.batch_size + 1), "refresh": "1",
                    "data": [{"item": items, "type": ["0B" if self.market == "KR" else "FE"]}],
                })
                self.admitted_count += len(batch)
                await self._status("admitting")
            if self._stopping:
                return
            self.ready = True
            await self._status("ready", "요청한 전체 종목의 실시간 등록 응답을 확인했습니다.")
            self._processor_task = asyncio.create_task(self._process())
            done, _ = await asyncio.wait([self._reader_task, self._processor_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if not self._stopping:
                raise StreamError("키움 실시간 연결이 종료되었습니다.")
        except asyncio.CancelledError:
            if not self._stopping:
                failed = True
                self.ready = False
                await self.on_gap(f"{self.market} 실시간 작업이 취소되어 자동매매를 중지합니다.")
                await self._status("error", "실시간 작업이 취소되었습니다.")
            raise
        except Exception as exc:
            if not self._stopping:
                failed = True
                self.ready = False
                message = str(exc) if isinstance(exc, StreamError) else "실시간 통신 또는 인증에 실패했습니다."
                await self.on_gap(f"{self.market}: {message}")
                await self._status("error", message)
                raise StreamError(message) from None
        finally:
            self.ready = False
            self._stopping = True
            self._wake_processor()
            if self._socket:
                with contextlib.suppress(Exception):
                    await self._socket.close()
            if self._reader_task:
                self._reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reader_task
            # Never cancel a callback that may currently be submitting an order.
            if self._processor_task:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._processor_task)
            self._running = False
            if not failed:
                await self._status("stopped")

    async def stop(self):
        self._stopping = True
        self.ready = False
        self._wake_processor()
        if self._socket:
            with contextlib.suppress(Exception):
                await self._socket.close()
        if self._processor_task and asyncio.current_task() is not self._processor_task:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._processor_task)

    def _wake_processor(self):
        # Discard queued ticks only after explicit stop/disarm, never while live.
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    async def _process(self):
        while True:
            item = await self._queue.get()
            if item is None or self._stopping:
                return
            instrument, quote = item
            await self.on_quote(instrument, quote)

    async def _receive(self):
        try:
            while not self._stopping:
                raw = await self._socket.recv()
                try:
                    packet = json.loads(raw)
                except (ValueError, TypeError, UnicodeError):
                    raise StreamError("실시간 응답이 올바른 JSON이 아닙니다.") from None
                if not isinstance(packet, dict):
                    raise StreamError("실시간 응답 형식이 올바르지 않습니다.")
                trnm = packet.get("trnm")
                if trnm == "PING":
                    await self._send(packet)
                elif trnm in {"LOGIN", "REG"}:
                    pending = self._pending_ack
                    if not pending or pending[0] != trnm or pending[1].done():
                        raise StreamError("예상하지 않은 실시간 등록/인증 응답입니다.")
                    if packet.get("grp_no") is not None and pending[2] is not None and str(packet["grp_no"]) != str(pending[2]):
                        raise StreamError("실시간 등록 응답의 그룹번호가 요청과 다릅니다.")
                    if str(packet.get("return_code")) != "0":
                        raise StreamError("키움이 실시간 인증 또는 종목 등록을 거절했습니다. 구독 한도·권한을 확인하세요.")
                    pending[1].set_result(True)
                elif trnm == "REAL":
                    records = packet.get("data")
                    if not isinstance(records, list):
                        raise StreamError("실시간 시세 목록 응답이 올바르지 않습니다.")
                    if not self.ready:
                        self.pre_ready_quotes += len(records)
                        continue
                    for index, record in enumerate(records):
                        pair = self._quote(record)
                        if pair is None:
                            continue
                        try:
                            self._queue.put_nowait(pair)
                        except asyncio.QueueFull:
                            raise StreamError("실시간 처리 대기열이 가득 찼습니다. 시세 누락을 방지하려고 자동매매를 중지합니다.") from None
                        if index % 64 == 63:
                            await asyncio.sleep(0)
                else:
                    raise StreamError("알 수 없는 실시간 프로토콜 응답입니다.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, StreamError) else StreamError("실시간 연결이 끊겼습니다. 재시작 전 가격 기준을 초기화하세요.")
            if self._pending_ack and not self._pending_ack[1].done():
                self._pending_ack[1].set_exception(error)
            if not self._stopping:
                raise error from None

    def _quote(self, record):
        if not isinstance(record, dict):
            raise StreamError("실시간 체결 레코드 형식이 올바르지 않습니다.")
        expected_type = "0B" if self.market == "KR" else "FE"
        if record.get("type") != expected_type:
            raise StreamError("등록하지 않은 실시간 시세 유형이 수신되었습니다.")
        values = record.get("values")
        if not isinstance(values, dict):
            raise StreamError("실시간 체결 필드가 없습니다.")
        item = record.get("item")
        if isinstance(item, dict):
            symbol = str(item.get("jmcode", "")).upper()
            exchange = item.get("stex_tp")
        else:
            symbol = str(item or "").upper()
            exchange = record.get("stexTp") or record.get("stex_tp")
        if self.market == "KR":
            exchange = "KRX"
        candidates = self._by_symbol.get(symbol, [])
        instrument = self._by_key.get((symbol, exchange)) if exchange else (candidates[0] if len(candidates) == 1 else None)
        if instrument is None:
            raise StreamError("등록하지 않았거나 거래소를 확인할 수 없는 종목의 시세입니다.")
        try:
            price = number(values.get("10"), absolute=True)
        except Exception:
            raise StreamError("실시간 체결 가격이 올바르지 않습니다.") from None
        if price <= 0:
            raise StreamError("실시간 체결 가격이 0 이하입니다.")
        now = self._clock()
        if now.tzinfo is None:
            raise StreamError("실시간 시스템 시계에 시간대가 없습니다.")
        trade_time = str(values.get("20", ""))
        if self.market == "KR":
            zone = "Asia/Seoul"
            trade_date = now.astimezone(ZoneInfo(zone)).strftime("%Y%m%d")
        else:
            zone = getattr(self.broker, "_us_timestamp_tz", None) or os.getenv("KIWOOM_US_TIMESTAMP_TZ")
            if zone not in {"Asia/Seoul", "America/New_York"}:
                zone = None
            if zone == "America/New_York" and values.get("51020"):
                trade_time = str(values["51020"])
            trade_date = str(values.get("22", ""))
        market_timestamp = None
        if zone:
            if not re.fullmatch(r"[0-9]{8}", trade_date) or not re.fullmatch(r"[0-9]{6}", trade_time):
                raise StreamError("실시간 체결 일자/시각 형식을 확인할 수 없습니다.")
            try:
                stamp = datetime.strptime(trade_date + trade_time, "%Y%m%d%H%M%S")
                market_timestamp = stamp.replace(tzinfo=ZoneInfo(zone)).isoformat()
            except ValueError:
                raise StreamError("실시간 체결 일자/시각을 확인할 수 없습니다.") from None
        return instrument, {
            "market": self.market, "symbol": symbol, "price": str(price),
            "currency": "KRW" if self.market == "KR" else "USD",
            "observed_at": now.isoformat(), "market_timestamp": market_timestamp,
            "timestamp_verified": market_timestamp is not None, "source": "websocket",
            "raw_trade_date": trade_date, "raw_trade_time": str(values.get("20", "")),
            "local_trade_time": str(values.get("51020", "")),
        }
