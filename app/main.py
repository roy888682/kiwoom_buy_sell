"""Local dashboard and persistent, single-writer trading orchestration.

Nothing trades at import or server startup. Live requests need credentials, an
environment opt-in, explicit UI arming, verified history and fresh market data.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .marketdata import compute_history, market_day, parse_history_csv, previous_session, session_open
from .storage import Store
from .instance import project_identity
from .strategy import (OPEN_STATUSES, Decision, Instrument, Order, Portfolio,
                       Position, Settings, Strategy, decimal, serialize, utcnow)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def instrument_fields(data):
    return {k: v for k, v in data.items() if k in {f.name for f in fields(Instrument)}}


def restore_portfolio(data):
    return Portfolio(cash=data.get("cash", {}),
                     positions={k: Position(**v) for k, v in data.get("positions", {}).items()},
                     orders={k: Order(**v) for k, v in data.get("orders", {}).items()},
                     stopped_at=data.get("stopped_at", {}))


def validate_symbol(market, symbol):
    symbol = symbol.strip().upper()
    if market not in {"KR", "US"} or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol):
        raise ValueError("시장과 종목 코드를 확인하세요.")
    if market == "KR" and not re.fullmatch(r"[A-Z0-9]{6}", symbol):
        raise ValueError("국내 종목 코드는 6자리여야 합니다.")
    return symbol


def fresh_quote(quote):
    """Receipt time never substitutes for the broker's actual trade time."""
    if not quote or not quote.get("timestamp_verified"):
        return False
    try:
        stamp = datetime.fromisoformat(quote["market_timestamp"])
        return stamp.tzinfo is not None and -5 <= (utcnow()-stamp).total_seconds() <= 90
    except (KeyError, TypeError, ValueError):
        return False


class Controller:
    def __init__(self, directory=None, broker=None):
        self.store = Store(directory or os.getenv("TRADER_DATA_DIR", str(ROOT / "data")))
        try:
            saved = self.store.load() or {}
            self.settings = Settings(**saved.get("settings", {}))
            self.instruments = {k: Instrument(**instrument_fields(v)) for k, v in saved.get("instruments", {}).items()}
            # Samples from a previous process are never treated as fresh crossings.
            for item in self.instruments.values():
                item.previous_price = item.current_price = None
            self.metadata = saved.get("metadata", {})
            self.portfolios = {m: restore_portfolio(saved.get("portfolios", {}).get(m, {})) for m in ("paper", "live")}
            if not saved:
                self.portfolios["paper"].cash = {"KRW": Decimal("10000000"), "USD": Decimal("10000")}
            self.mode = saved.get("mode", "paper")
            self.broker_orders = saved.get("broker_orders", {})
            self.broker_order_dates = saved.get("broker_order_dates", {})
            self.events = saved.get("events", [])[-300:]
            self.running = False
            self.latest_error = None
            self.lock = asyncio.Lock()
            self.worker_task = None
            self.scan_task = None
            self.scan_enabled = False
            self.stopping = False
            self.feeds = {}
            self.feed_tasks = {}
            self.feed_status = {}
            self.last_ticks = {}
            self.expected_sessions = {}
            self.broker = broker
            self.scanner = {"status": "idle", "market": "ALL", "processed": 0, "total": 0,
                            "candidates": 0, "universe_count": len(self.instruments), "last_error": None,
                            "coverage_message": "전체 시장 검색 전입니다. 예제 종목을 전체 시장으로 표시하지 않습니다."}
            for order in self.portfolios["live"].orders.values():
                if order.status in OPEN_STATUSES and not self.broker_orders.get(order.id):
                    order.status = "unknown"
            self.event("info", "프로그램 시작: 자동매매는 정지 상태입니다.")
            self.save()
        except BaseException:
            # Initialization starts no workers; release ownership if restoring
            # invalid state or writing the initial snapshot fails.
            self.store.close()
            raise

    @property
    def portfolio(self):
        return self.portfolios[self.mode]

    def expected_history_day(self, market):
        today = market_day(market)
        cached = self.expected_sessions.get(market)
        if not cached or cached[0] != today:
            cached = (today, previous_session(market))
            self.expected_sessions[market] = cached
        return cached[1]

    def event(self, level, message):
        self.events.append({"created_at": utcnow().isoformat(), "level": level, "message": message})
        self.events = self.events[-300:]

    def save(self):
        self.store.save(serialize({"settings": self.settings, "instruments": self.instruments,
            "metadata": self.metadata, "portfolios": self.portfolios, "mode": self.mode,
            "broker_orders": self.broker_orders, "broker_order_dates": self.broker_order_dates, "events": self.events}))

    def broker_configured(self):
        return bool(self.broker is not None or (os.getenv("KIWOOM_APP_KEY") and os.getenv("KIWOOM_SECRET_KEY")))

    def get_broker(self):
        if self.broker is None:
            if not self.broker_configured():
                raise ValueError("키움 앱 키가 없습니다. .env 또는 환경 설정에 KIWOOM_APP_KEY와 KIWOOM_SECRET_KEY를 입력하세요.")
            from .kiwoom import KiwoomClient
            self.broker = KiwoomClient()
        return self.broker

    def broker_instrument(self, key):
        item = self.instruments[key]
        return {"market": item.market, "symbol": item.symbol, "name": item.name, **self.metadata.get(key, {})}

    def state(self):
        watchlist = []
        for key, item in self.instruments.items():
            meta = self.metadata.get(key, {})
            watchlist.append({**serialize(item), "key": key, "currency": item.currency,
                              "threshold": serialize(item.threshold(self.settings)),
                              "history_reason": meta.get("history_reason", "가격 이력 미확인"),
                              "source": meta.get("source", "manual")})
        positions = [{**serialize(p), "key": p.key, "currency": p.currency,
                      "stop_price": str(p.stop_price(self.settings))} for p in self.portfolio.positions.values()]
        # Return a bounded view, while the scanner continues across the FULL universe.
        visible = sorted(watchlist, key=lambda r: (r["key"] not in self.portfolio.positions, r["current_price"] is None, r["key"]))[:250]
        return {"settings": serialize(self.settings), "running": self.running,
            "armed": self.running and self.mode == "live", "mode": self.mode,
            "cash": {c: str(self.portfolio.available_cash(c)) for c in ("KRW", "USD")},
            "positions": positions, "watchlist": visible, "watchlist_total": len(watchlist),
            "orders": [serialize(o) for o in list(self.portfolio.orders.values())[-200:]][::-1],
            "events": self.events[-80:][::-1], "scanner": self.scanner,
            "feeds": self.feed_status,
            "broker": {"configured": self.broker_configured(), "domestic_live_supported": True,
                "us_live_supported": True, "environment": os.getenv("KIWOOM_ENV", "mock"),
                "message": "키움 공식 REST 국내·미국 주식 API 어댑터. 실계좌 연결·실주문은 사용자 키로 별도 검증해야 합니다. 미국 시세 시각 확인 전 자동주문이 차단됩니다."},
            "latest_error": self.latest_error}

    def add(self, market, symbol, name="", **metadata):
        symbol = validate_symbol(market, symbol)
        key = f"{market}:{symbol}"
        if key not in self.instruments:
            self.instruments[key] = Instrument(market, symbol, name or symbol)
        elif name:
            self.instruments[key].name = name
        self.metadata.setdefault(key, {}).update({k:v for k,v in metadata.items() if v is not None})
        return self.instruments[key]

    def apply_history(self, item, history, *, provenance):
        item.all_time_high = decimal(history["all_time_high"]) if history.get("all_time_high") else None
        through = history.get("history_through")
        item.history_through = date.fromisoformat(through) if isinstance(through, str) else through
        item.history_verified = bool(history.get("history_verified", False))
        # Changing the daily reference cannot manufacture a crossing.
        item.previous_price = item.current_price = None
        self.metadata.setdefault(item.key, {})["history_reason"] = history.get("history_reason", "미확인")
        self.metadata[item.key]["history_source"] = provenance
        self.metadata[item.key]["adjustment_as_of"] = market_day(item.market).isoformat()

    async def tick(self, key, price, *, source="manual", quote=None):
        async with self.lock:
            price = decimal(price)
            if price <= 0:
                raise ValueError("시세는 0보다 커야 합니다.")
            item = self.instruments[key]
            if self.mode == "live" and source not in {"kiwoom", "kiwoom_ws"}:
                raise ValueError("실거래에는 입력/예제 가격을 사용할 수 없습니다.")
            if self.mode == "live" and (not session_open(item.market) or not fresh_quote(quote)):
                # Reset baseline; resuming after an outage requires a fresh pair.
                item.current_price = item.previous_price = None
                self.latest_error = f"{item.key}: 정규장 또는 최근 90초 이내 체결 시각을 확인할 수 없어 주문 차단"
                return None
            if source in {"kiwoom", "kiwoom_ws"} and quote and fresh_quote(quote):
                stamp = datetime.fromisoformat(quote["market_timestamp"])
                if key in self.last_ticks and stamp < self.last_ticks[key]:
                    return None
                self.last_ticks[key] = stamp
            engine = Strategy(self.settings, self.portfolio)
            if not self.running:
                item.previous_price, item.current_price = item.current_price, decimal(price)
                return None
            if self.mode == "live":
                # Orderable cash is already net of broker-held funds. Add local
                # known reserves only to reconstruct the core's gross-cash model.
                # Require accepted orders to reconcile before another entry.
                has_open_buy = any(o.side == "BUY" and o.status in OPEN_STATUSES and o.currency == item.currency
                                   for o in self.portfolio.orders.values())
                threshold = item.threshold(self.settings)
                maybe_entry = (not has_open_buy and threshold is not None and item.current_price is not None
                               and item.current_price < threshold <= decimal(price)
                               and item.history_verified and item.history_through == self.expected_history_day(item.market))
                if maybe_entry:
                    funds = await self.get_broker().orderable_cash(self.broker_instrument(key), str(price))
                    self.portfolio.cash[item.currency] = decimal(funds["available_cash"])
            decision = engine.evaluate_tick(item, price, self.expected_history_day(item.market))
            if not decision:
                return None
            if self.mode == "live":
                # Demo/manual ATHs never enter a real-money order path.
                if decision.side == "BUY":
                    if self.metadata.get(key, {}).get("history_source") not in {"provider", "csv_attested"}:
                        return None
                    if self.metadata.get(key, {}).get("adjustment_as_of") != market_day(item.market).isoformat():
                        return None
                    if source != "kiwoom_ws" or not self.feeds.get(item.market) or not all(f.ready for f in self.feeds.values()):
                        return None
                broker = self.get_broker()
                spec = self.broker_instrument(key)
                if decision.side == "SELL":
                    buys = self.portfolio.pending_orders(key, "BUY")
                    for buy in buys:
                        await self.cancel(buy.id, locked=True)
                    # Cancellation can race a fill; recompute from reconciled positions.
                    position = self.portfolio.positions.get(key)
                    if not position:
                        return None
                    decision.quantity = position.quantity
                    holdings = await broker.holdings(item.market)
                    actual = next((p for p in holdings if p["symbol"] == item.symbol), None)
                    if actual is None or int(actual["quantity"]) != position.quantity:
                        raise ValueError("계좌 보유 수량 불일치: 자동매매를 멈추고 계좌를 동기화하세요.")
                    if int(actual.get("sellable_quantity", 0)) < decision.quantity:
                        raise ValueError("전량 매도 가능 수량이 부족합니다. 외부 주문/담보 수량을 확인하세요.")
                    position.average_price = decimal(actual["average_price"])
                    if price > position.stop_price(self.settings):
                        return None
                else:
                    if any(o.side == "BUY" and o.status in OPEN_STATUSES and o.currency == item.currency
                           for o in self.portfolio.orders.values()):
                        # Cash snapshots may lag a just-accepted order. Wait for
                        # reconciliation rather than spend the reservation twice.
                        return None
                    funds = await broker.orderable_cash(spec, str(decision.limit_price))
                    available = decimal(funds["available_cash"])
                    reserves = sum((o.reserved_cash for o in self.portfolio.orders.values() if o.currency == item.currency), Decimal("0"))
                    self.portfolio.cash[item.currency] = available + reserves
                    budget = available * self.settings.allocation_percent / 100
                    decision.quantity = min(int(budget / (decision.limit_price * (1 + self.settings.fee_reserve_percent / 100))), int(funds["max_quantity"]))
                    if decision.quantity < 1:
                        return None
            if self.mode == "live" and (not self.running or not session_open(item.market) or not fresh_quote(quote)):
                item.current_price = item.previous_price = None
                self.latest_error = f"{item.key}: 주문 준비 중 시세가 만료되었거나 장이 종료되어 주문 차단"
                return None
            order = engine.submit_order(item, decision)
            if self.mode == "paper":
                fee = order.limit_price * order.quantity * self.settings.fee_reserve_percent / 100
                engine.apply_order_update(order.id, order.quantity, order.limit_price, "filled", cumulative_fee=fee)
                self.event("info", f"가상 체결 {item.key} {order.side} {order.quantity}주 @ {order.limit_price}")
            else:
                # Persist uncertain intent BEFORE the network. Never retry an order.
                order.status = "unknown"
                self.save()
                try:
                    result = await self.get_broker().submit_order(self.broker_instrument(key), order.side.lower(), order.quantity,
                        str(order.limit_price) if order.side == "BUY" else None)
                except Exception as exc:
                    from .kiwoom import OrderOutcomeUnknown
                    if isinstance(exc, OrderOutcomeUnknown):
                        self.event("error", "주문 응답 불명: 중복 주문을 방지하려고 자동매매를 중지했습니다. 키움 계좌에서 주문을 확인하세요.")
                    else:
                        engine.apply_order_update(order.id, 0, 0, "rejected")
                        self.event("error", "키움 주문이 거절되었습니다. 주문/계좌 조건을 확인하세요.")
                    self.running = False
                    self.save()
                    raise
                self.broker_orders[order.id] = result["order_id"]
                order.status = "pending"
                self.event("info", f"키움 주문 접수 {item.key} {order.side} {order.quantity}주. 체결 전입니다.")
            self.save()
            return order

    async def reconcile(self):
        async with self.lock:
            engine = Strategy(self.settings, self.portfolio)
            for order in list(self.portfolio.orders.values()):
                if order.status not in OPEN_STATUSES:
                    continue
                broker_id = self.broker_orders.get(order.id)
                if not broker_id:
                    raise ValueError("응답 불명 주문이 있습니다. 키움 계좌에서 확인 후 로컬 주문 기록을 대조해야 합니다.")
                result = await self.get_broker().order_status(self.broker_instrument(order.key), broker_id, **({"order_date": self.broker_order_dates[order.id]} if self.broker_order_dates.get(order.id) else {}))
                engine.apply_order_update(order.id, int(result["filled_quantity"]), result.get("average_fill_price", "0"),
                                          result["status"], cumulative_fee=result.get("cumulative_fee", "0"))
            # Broker positions are authoritative for actual average purchase
            # prices, including fees/rounding and executions outside this process.
            # Unexpected external activity stops automation instead of guessing.
            for market in ("KR", "US"):
                actual = {f"{market}:{r['symbol']}": r for r in await self.get_broker().holdings(market)}
                local = {k:p for k,p in self.portfolio.positions.items() if p.market == market}
                if set(actual) != set(local) or any(int(actual[k]["quantity"]) != p.quantity for k,p in local.items()):
                    raise ValueError("체결 기록과 실제 계좌 보유 수량이 다릅니다. 자동매매를 중지하고 계좌를 대조하세요.")
                for key, position in local.items():
                    position.average_price = decimal(actual[key]["average_price"])
                funds = await self.get_broker().cash(market)
                currency = funds["currency"]
                reserves = sum((o.reserved_cash for o in self.portfolio.orders.values() if o.currency == currency), Decimal("0"))
                self.portfolio.cash[currency] = decimal(funds["available_cash"]) + reserves
            self.save()

    async def cancel(self, order_id, locked=False):
        if not locked:
            async with self.lock:
                return await self.cancel(order_id, locked=True)
        order = self.portfolio.orders.get(order_id)
        if order is None or order.status not in OPEN_STATUSES:
            raise ValueError("취소할 미체결 주문이 없습니다.")
        if self.mode == "paper":
            Strategy(self.settings, self.portfolio).apply_order_update(order.id, order.filled_quantity, order.average_fill_price, "canceled", cumulative_fee=order.fee_paid)
        else:
            broker_id = self.broker_orders.get(order.id)
            if not broker_id:
                raise ValueError("주문 번호가 확인되지 않아 재전송할 수 없습니다. 키움에서 주문을 확인하세요.")
            broker = self.get_broker()
            await broker.cancel_order(self.broker_instrument(order.key), broker_id, order.quantity-order.filled_quantity)
            result = await broker.order_status(self.broker_instrument(order.key), broker_id, **({"order_date": self.broker_order_dates[order.id]} if self.broker_order_dates.get(order.id) else {}))
            if result["status"] not in {"canceled", "filled", "rejected"}:
                raise ValueError("취소가 아직 확정되지 않았습니다. 새 주문을 차단합니다.")
            Strategy(self.settings, self.portfolio).apply_order_update(order.id, int(result["filled_quantity"]), result.get("average_fill_price", "0"), result["status"], cumulative_fee=result.get("cumulative_fee", "0"))
        self.event("info", f"주문 취소 확인 {order.key}")
        self.save()

    async def sync_account(self):
        if self.mode != "live" or self.running:
            raise ValueError("실거래 모드를 정지한 상태에서 계좌를 동기화하세요.")
        await self.reconcile()
        self.event("info", "키움 주문/체결·잔고 동기화를 마쳤습니다.")
        self.save()

    async def start(self, mode, confirmation=""):
        async with self.lock:
            if self.stopping or (self.worker_task and not self.worker_task.done()):
                raise ValueError("기존 실행이 정지될 때까지 기다리세요.")
            if self.running:
                raise ValueError("이미 실행 중입니다.")
            if mode not in {"paper", "live"}:
                raise ValueError("실행 모드를 확인하세요.")
            if self.scan_task and not self.scan_task.done():
                raise ValueError("검색을 정지한 뒤 모드를 전환하세요.")
            if mode == "paper" and any(o.status in OPEN_STATUSES for o in self.portfolios["live"].orders.values()):
                raise ValueError("실계좌 미체결/응답 불명 주문을 동기화·취소한 뒤 모의 실행으로 전환하세요.")
            if mode == "live":
                if os.getenv("ALLOW_LIVE_TRADING", "false").lower() != "true" or confirmation != "실거래 시작":
                    raise ValueError("ALLOW_LIVE_TRADING=true 설정과 '실거래 시작' 확인 문구가 필요합니다.")
                if os.getenv("KIWOOM_ENV", "mock") != "live":
                    raise ValueError("실거래에는 KIWOOM_ENV=live와 운영용 앱 키를 사용하세요.")
                broker = self.get_broker()
                target = self.portfolios["live"]
                if any(o.status in OPEN_STATUSES for o in target.orders.values()):
                    raise ValueError("기존 미체결/응답 불명 주문이 있습니다. 주문을 대조·취소한 뒤 시작하세요.")
                # Master metadata supplies US exchange codes for existing holdings.
                for row in await broker.universe(markets=("KR", "US")):
                    self.add(row["market"], row["symbol"], row.get("name", ""), **{k:v for k,v in row.items() if k not in {"market", "symbol", "name"}})
                actual_positions = {}
                for market in ("KR", "US"):
                    if await broker.open_orders(market):
                        raise ValueError(f"{market} 계좌에 미체결 주문이 있습니다. 키움에서 확인·정리한 뒤 시작하세요.")
                    for row in await broker.holdings(market):
                        qty = int(row["quantity"])
                        if qty <= 0:
                            continue
                        if int(row.get("sellable_quantity", 0)) < qty:
                            raise ValueError("전량 매도할 수 없는 계좌 보유 종목이 있습니다. 계좌 주문·담보 상태를 확인하세요.")
                        item = self.add(market, row["symbol"], row.get("name", ""), **{k:v for k,v in row.items() if k in {"exchange", "kind", "listing_date"}})
                        avg = decimal(row["average_price"])
                        actual_positions[item.key] = Position(market, item.symbol, item.name, qty, avg, avg)
                target.positions = actual_positions
                target.cash = {}
                for market in ("KR", "US"):
                    funds = await broker.cash(market)
                    target.cash[funds["currency"]] = decimal(funds["available_cash"])
            self.mode = mode
            for item in self.instruments.values():
                item.current_price = item.previous_price = None
            self.last_ticks = {}
            self.last_scan_days = None
            self.latest_error = None
            self.running = True
            self.scan_enabled = True
            self.event("info", "실거래 시작: 계좌 동기화 완료. 첫 시세는 돌파 판단의 기준으로만 사용합니다." if mode == "live" else "모의 실행 시작. 실제 주문을 전송하지 않습니다.")
            self.save()
        self.worker_task = asyncio.create_task(self.worker())

    async def stop(self):
        self.stopping = True
        self.running = False
        self.scan_enabled = False
        self.scanner["status"] = "idle"
        self.event("info", "자동매매 정지. 이미 접수한 키움 미체결 주문은 취소 버튼 또는 키움에서 별도로 확인하세요. 정지 중에는 손절도 실행하지 않습니다.")
        if self.worker_task and self.worker_task is not asyncio.current_task():
            # Let any in-flight order resolve before changing modes or reporting stop.
            await self.worker_task
        if self.scan_task and not self.scan_task.done() and self.scan_task is not asyncio.current_task():
            await self.scan_task
        await self.stop_feeds()
        self.stopping = False
        self.save()

    async def worker(self):
        while self.running:
            try:
                if self.mode == "live":
                    await self.reconcile()
                keys = list(self.portfolio.positions)
                if self.settings.universe_mode == "watchlist":
                    keys += [k for k in self.instruments if k not in keys]
                for key in keys:
                    if not self.running:
                        break
                    if not self.broker_configured() or (self.metadata.get(key, {}).get("source") == "demo" and self.mode == "paper"):
                        continue
                    quote = await self.get_broker().quote(self.broker_instrument(key))
                    await self.tick(key, quote["price"], source="kiwoom", quote=quote)
                if self.settings.universe_mode == "all" and self.scan_enabled and self.broker_configured():
                    if not self.scan_task or self.scan_task.done():
                        if self.scanner["status"] == "error":
                            raise ValueError(self.scanner["last_error"] or "전체 시장 검색 오류")
                        # Daily discovery/history update; fresh ticks use streams.
                        days = {m: market_day(m).isoformat() for m in ("KR", "US")}
                        if getattr(self, "last_scan_days", None) != days:
                            await self.begin_scan("ALL")
                elif self.settings.universe_mode == "watchlist" and self.broker_configured() and not self.feeds:
                    await self.start_feeds()
            except Exception as exc:
                self.latest_error = str(exc)
                self.event("error", f"자동매매 정지: {self.latest_error}")
                self.running = False
                self.save()
                break
            # Short waits allow the stop action to finish promptly.
            for _ in range(self.settings.poll_interval_seconds * 5):
                if not self.running:
                    break
                await asyncio.sleep(0.2)

    async def stop_feeds(self):
        feeds, tasks = list(self.feeds.values()), list(self.feed_tasks.values())
        for feed in feeds:
            await feed.stop()
        for task in tasks:
            if task is not asyncio.current_task():
                with contextlib.suppress(Exception):
                    await task
        self.feeds = {}
        self.feed_tasks = {}

    async def start_feeds(self):
        from .stream import KiwoomStream, StreamError
        await self.stop_feeds()
        if not self.running:
            return
        self.feed_status = {}
        selected = {}
        for market in ("KR", "US"):
            rows = []
            for key, item in self.instruments.items():
                if item.market != market or self.metadata.get(key, {}).get("source") == "demo":
                    continue
                valid = (item.history_verified and item.history_through == previous_session(market)
                         and self.metadata.get(key, {}).get("adjustment_as_of") == market_day(market).isoformat())
                if key in self.portfolio.positions or valid:
                    rows.append(self.broker_instrument(key))
            if rows:
                selected[market] = rows
        if not selected:
            self.latest_error = "실시간 구독 가능한 검증 이력/보유 종목이 없습니다. 수정주가 전체 이력과 상장일을 확인하세요."
            self.event("warning", self.latest_error)
            return

        async def quote_received(instrument, quote):
            if self.running:
                await self.tick(f"{instrument['market']}:{instrument['symbol']}", quote["price"], source="kiwoom_ws", quote=quote)

        async def gap(message):
            self.running = False
            self.scan_enabled = False
            self.scanner["status"] = "idle"
            self.latest_error = f"실시간 시세 공백: {message}. 자동매매 정지. 연결을 확인한 뒤 다시 시작하세요."
            async with self.lock:
                for item in self.instruments.values():
                    item.current_price = item.previous_price = None
                self.event("error", self.latest_error)
                self.save()

        async def feed_runner(feed):
            try:
                await feed.run()
            except StreamError:
                pass  # on_gap already records failure and stops the engine.

        # Populate the whole requested set before any ready callback may trade.
        for market, rows in selected.items():
            async def status_changed(status, selected_market=market):
                status = {**status, "status": status.get("status", status.get("state"))}
                async with self.lock:
                    self.feed_status[selected_market] = status
                    if status.get("status") == "ready":
                        for item in self.instruments.values():
                            if item.market == selected_market:
                                item.current_price = item.previous_price = None
                                self.last_ticks.pop(item.key, None)
                        self.event("info", f"{selected_market} 실시간 구독 승인 완료. 새 첫 시세는 기준으로만 사용합니다.")
                    self.save()
            self.feeds[market] = KiwoomStream(self.get_broker(), market, rows, quote_received, gap, status_changed)
        for market, feed in self.feeds.items():
            self.feed_tasks[market] = asyncio.create_task(feed_runner(feed))

    async def scan(self, market):
        self.scanner.update(status="running", market=market, processed=0, candidates=0, last_error=None)
        try:
            markets = ("KR", "US") if market == "ALL" else (market,)
            if self.broker_configured():
                universe = await self.get_broker().universe(markets=markets)
                source = "kiwoom"
                coverage = "키움 공식 종목 목록. 시세 권한·거래소 및 이력 검증 통과 여부를 별도로 확인하세요."
            else:
                universe = [self.broker_instrument(k) for k, i in self.instruments.items() if i.market in markets and self.metadata.get(k, {}).get("source") == "csv"]
                source = "csv"
                coverage = "CSV에 포함된 종목 범위만 검색합니다. 전체 시장 목록이라는 보장은 없습니다. 실시간 가격은 키움 연결이 필요합니다."
                if not universe:
                    raise ValueError("전체 종목 목록을 받으려면 키움 앱 키를 설정하세요. 키 없이 CSV 범위를 검색하거나 예제를 실행할 수 있습니다.")
            self.scanner.update(total=len(universe), universe_count=len(universe), failed=0, coverage_message=coverage)
            consecutive_errors = 0
            for row in universe:
                if self.scanner["status"] != "running":
                    break
                async with self.lock:
                    item = self.add(row["market"], row["symbol"], row.get("name", ""), **{k:v for k,v in row.items() if k not in {"market", "symbol", "name"}})
                    if source == "kiwoom":
                        if self.metadata[item.key].get("source") not in {"csv", "demo"}:
                            self.metadata[item.key]["source"] = "kiwoom"
                try:
                    if source == "kiwoom":
                        day = market_day(item.market).isoformat()
                        if (item.history_through != previous_session(item.market) or not item.history_verified
                                or self.metadata[item.key].get("adjustment_as_of") != day):
                            history = await self.get_broker().history(self.broker_instrument(item.key), market_day(item.market))
                            computed = await asyncio.to_thread(compute_history, history.get("bars", []), item.market,
                                listing_date=self.metadata[item.key].get("listing_date"),
                                complete_from_listing=bool(history.get("history_complete")))
                            async with self.lock:
                                self.apply_history(item, computed, provenance="provider")
                        quote = await self.get_broker().quote(self.broker_instrument(item.key))
                        await self.tick(item.key, quote["price"], source="kiwoom", quote=quote)
                    consecutive_errors = 0
                except Exception as exc:
                    # A suspended or unavailable instrument does not pretend to
                    # be scanned successfully or silently disappear from coverage.
                    self.scanner["failed"] += 1
                    consecutive_errors += 1
                    item.current_price = item.previous_price = None
                    self.metadata[item.key]["scan_error"] = str(exc)
                    self.scanner["last_error"] = f"{item.key}: {exc}"
                    if self.mode == "live" or consecutive_errors >= 3:
                        raise
                    self.event("warning", f"검색 제외 {item.key}: 데이터 조회 오류")
                self.scanner["processed"] += 1
                if item.history_verified:
                    self.scanner["candidates"] += 1
                if self.scanner["processed"] % 20 == 0:
                    self.save()
            if self.scanner["status"] == "running":
                self.scanner["status"] = "completed"
                self.last_scan_days = {m: market_day(m).isoformat() for m in markets}
                self.event("info", f"종목 검색 완료 {self.scanner['processed']}/{len(universe)}. 이력 검증 {self.scanner['candidates']}개. 가격 돌파는 이후 새 시세에서 판단합니다.")
                if self.running and self.broker_configured():
                    await self.start_feeds()
            self.save()
        except asyncio.CancelledError:
            self.scanner["status"] = "idle"
            self.save()
        except Exception as exc:
            self.scanner.update(status="error", last_error=str(exc))
            self.event("error", f"검색 중단: {exc}")
            if self.running:
                self.latest_error = f"전체 시장 검색 중단: {exc}"
                self.running = False
            self.save()

    async def begin_scan(self, market):
        if market not in {"KR", "US", "ALL"}:
            raise ValueError("검색 시장을 확인하세요.")
        if self.scan_task and not self.scan_task.done():
            raise ValueError("이미 검색 중입니다.")
        self.scan_enabled = True
        self.scanner.update(status="running", market=market, processed=0, candidates=0, last_error=None)
        self.scan_task = asyncio.create_task(self.scan(market))

    def reset_demo(self):
        if self.running or (self.scan_task and not self.scan_task.done()):
            raise ValueError("자동매매와 검색을 정지한 뒤 예제를 초기화하세요.")
        if any(o.status in OPEN_STATUSES for o in self.portfolios["live"].orders.values()):
            raise ValueError("미체결 실주문을 확인한 뒤 예제를 초기화하세요.")
        self.mode = "paper"
        self.portfolios["paper"] = Portfolio(cash={"KRW": Decimal("10000000"), "USD": Decimal("10000")})
        # Preserve all user universe data. Only the reserved DEMO symbols are reset.
        for market, symbol, name, high in (("KR", "000000", "국내 규칙 예제 (가상)", "10000"), ("US", "DEMO", "미국 규칙 예제 (가상)", "100")):
            item = self.add(market, symbol, name, source="demo", history_source="demo", history_reason="예제 가격 이력: 실주문 금지")
            item.all_time_high = Decimal(high)
            item.history_through = previous_session(market)
            item.history_verified = True
            item.current_price = item.previous_price = None
        self.scanner.update(status="idle", coverage_message="예제는 규칙 확인용입니다. 전체 시장 검색은 별도로 실행하세요.")
        self.scanner["universe_count"] = len(self.instruments)
        self.event("info", "모의 포트폴리오 초기화: 가상 KRW 1,000만 원 / USD 1만 달러. 실제 계좌와 무관합니다.")
        self.save()

    async def close(self):
        # Stop before closing storage; never cancel an order submission task.
        await self.stop()
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scan_task
        # stop() must succeed before releasing the data-directory lock. If it
        # fails, a worker or in-flight order may still own this state; process
        # exit will release the OS lock after all tasks have actually ended.
        try:
            if self.broker:
                await self.broker.close()
        finally:
            self.store.close()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddInstrument(StrictModel):
    market: str
    symbol: str = Field(max_length=20)
    name: str = Field(default="", max_length=100)


class StartRequest(StrictModel):
    mode: str = "paper"
    confirmation: str = ""


class TickRequest(StrictModel):
    price: str


class ManualHistory(StrictModel):
    all_time_high: str
    history_through: date
    confirmed_adjusted_complete: bool


class ScanRequest(StrictModel):
    market: str = "ALL"


class OrderBinding(StrictModel):
    broker_order_id: str = Field(pattern=r"^[0-9]{1,30}$")
    broker_order_date: date | None = None


def create_app(directory=None, broker=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.controller = Controller(directory, broker)
        try:
            yield
        finally:
            await app.state.controller.close()

    app = FastAPI(title="돌파 · Kiwoom Breakout", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_security(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "외부 사이트의 요청을 차단했습니다."}, status_code=403)
            expected = "text/csv" if request.url.path == "/api/universe/import" else "application/json"
            if not request.headers.get("content-type", "").startswith(expected):
                return JSONResponse({"detail": f"Content-Type은 {expected}여야 합니다."}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    from .kiwoom import KiwoomError

    @app.exception_handler(KiwoomError)
    async def broker_failure(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    def controller(request):
        return request.app.state.controller

    @app.get("/")
    async def home():
        return FileResponse(ROOT / "app/static/index.html")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "kiwoom-breakout",
                "project_id": project_identity(ROOT), "instance_lock_version": 1, "pid": os.getpid(),
                "startup_token": os.environ.get("BREAKLINE_STARTUP_TOKEN", "")}

    @app.get("/api/state")
    async def state(request: Request):
        async with controller(request).lock:
            return controller(request).state()

    @app.put("/api/settings")
    async def settings(request: Request):
        c = controller(request)
        async with c.lock:
            if c.running:
                raise ValueError("매매를 정지한 뒤 설정을 변경하세요.")
            data = await request.json()
            if not isinstance(data, dict) or set(data) - {f.name for f in fields(Settings)}:
                raise ValueError("지원하지 않는 설정 항목입니다.")
            c.settings = Settings(**{**serialize(c.settings), **data})
            for item in c.instruments.values():
                item.current_price = item.previous_price = None
            c.event("info", "매매 설정을 저장했습니다. 다음 첫 시세는 기준으로만 사용합니다.")
            c.save()
            return c.state()

    @app.post("/api/watchlist")
    async def add(request: Request, body: AddInstrument):
        c = controller(request)
        async with c.lock:
            key = f"{body.market}:{body.symbol.strip().upper()}"
            c.add(body.market, body.symbol, body.name, **({"source": "manual"} if key not in c.instruments else {}))
            c.save()
            return c.state()

    @app.delete("/api/watchlist/{key}")
    async def remove(request: Request, key: str):
        c = controller(request)
        async with c.lock:
            if any(key in p.positions or p.pending_orders(key) for p in c.portfolios.values()):
                raise ValueError("보유/미체결 종목은 삭제할 수 없습니다.")
            c.instruments.pop(key, None)
            c.metadata.pop(key, None)
            c.save()
            return c.state()

    @app.post("/api/watchlist/{key}/tick")
    async def manual_tick(request: Request, key: str, body: TickRequest):
        c = controller(request)
        if key not in c.instruments:
            raise HTTPException(404, "종목이 없습니다.")
        if c.mode != "paper":
            raise ValueError("직접 가격 입력은 모의 실행에서만 가능합니다.")
        await c.tick(key, body.price)
        return c.state()

    @app.post("/api/watchlist/{key}/history")
    async def manual_history(request: Request, key: str, body: ManualHistory):
        c = controller(request)
        async with c.lock:
            if c.mode != "paper" or c.running:
                raise ValueError("수동 기준가 입력은 정지된 모의 실행에서만 가능합니다.")
            if key not in c.instruments:
                raise HTTPException(404, "종목이 없습니다.")
            high = decimal(body.all_time_high)
            if high <= 0 or not body.confirmed_adjusted_complete:
                raise ValueError("양수 가격과 이력 확인이 필요합니다.")
            item = c.instruments[key]
            if body.history_through != previous_session(item.market):
                raise ValueError("기준일은 해당 시장 전 거래일이어야 합니다.")
            c.apply_history(item, {"all_time_high": high, "history_through": body.history_through,
                "history_verified": True, "history_reason": "사용자 입력 예제: 실주문 금지"}, provenance="demo")
            c.metadata[key]["source"] = "demo"
            c.save()
            return c.state()

    @app.post("/api/engine/start")
    async def start(request: Request, body: StartRequest):
        c = controller(request)
        await c.start(body.mode, body.confirmation)
        return c.state()

    @app.post("/api/engine/stop")
    async def stop(request: Request):
        c = controller(request)
        await c.stop()
        return c.state()

    @app.post("/api/orders/{order_id}/cancel")
    async def cancel(request: Request, order_id: str):
        c = controller(request)
        await c.cancel(order_id)
        return c.state()

    @app.post("/api/account/sync")
    async def sync_account(request: Request):
        c = controller(request)
        await c.sync_account()
        return c.state()

    @app.post("/api/orders/{order_id}/bind")
    async def bind_order(request: Request, order_id: str, body: OrderBinding):
        c = controller(request)
        async with c.lock:
            if c.mode != "live" or c.running:
                raise ValueError("실거래를 정지한 상태에서 주문 번호를 대조하세요.")
            order = c.portfolio.orders.get(order_id)
            if not order or order.status not in OPEN_STATUSES:
                raise ValueError("미체결/응답 불명 주문만 수동으로 대조할 수 있습니다.")
            if body.broker_order_id in {v for k,v in c.broker_orders.items() if k != order.id}:
                raise ValueError("이미 연결된 키움 주문 번호입니다.")
            result = await c.get_broker().order_status(c.broker_instrument(order.key), body.broker_order_id, **({"order_date": body.broker_order_date.isoformat()} if body.broker_order_date else {}))
            if result.get("symbol") != order.symbol or result.get("side", "").upper() != order.side or int(result.get("quantity", 0)) != order.quantity:
                raise ValueError("키움 주문의 종목·방향·수량이 일치하지 않습니다.")
            c.broker_orders[order.id] = body.broker_order_id
            c.broker_order_dates[order.id] = body.broker_order_date.isoformat() if body.broker_order_date else None
            c.save()
        await c.sync_account()
        return c.state()

    @app.post("/api/scanner/start")
    async def scan(request: Request, body: ScanRequest):
        c = controller(request)
        await c.begin_scan(body.market)
        return c.state()

    @app.post("/api/scanner/stop")
    async def stop_scan(request: Request):
        c = controller(request)
        c.scan_enabled = False
        c.scanner["status"] = "idle"
        if c.scan_task and not c.scan_task.done():
            await c.scan_task
        c.save()
        return c.state()

    @app.post("/api/universe/import")
    async def import_csv(request: Request, market: str = "KR"):
        c = controller(request)
        if c.running or (c.scan_task and not c.scan_task.done()):
            raise ValueError("매매와 검색을 정지한 뒤 이력을 가져오세요.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 25 * 1024 * 1024:
                raise ValueError("CSV는 한 번에 25MB 이하로 나누어 가져오세요.")
        try:
            records = parse_history_csv(raw.decode("utf-8-sig"), market=market)
        except UnicodeDecodeError as exc:
            raise ValueError("CSV 인코딩은 UTF-8이어야 합니다.") from exc
        async with c.lock:
            if c.running or (c.scan_task and not c.scan_task.done()):
                raise ValueError("매매와 검색을 정지한 뒤 이력을 가져오세요.")
            for row in records:
                item = c.add(row["market"], row["symbol"], row.get("name", ""), source="csv", exchange=row.get("exchange"), listing_date=serialize(row.get("listing_date")))
                c.apply_history(item, row, provenance="csv_attested")
            c.scanner["universe_count"] = len(c.instruments)
            c.event("info", f"CSV {len(records)}종목 가져옴. 전체 시장 포함 여부와 상장 이후 이력은 제공자의 검증이 필요합니다.")
            c.save()
            return c.state()

    @app.post("/api/demo/reset")
    async def reset(request: Request):
        c = controller(request)
        async with c.lock:
            c.reset_demo()
            return c.state()

    @app.post("/api/demo/scenario")
    async def demo(request: Request):
        c = controller(request)
        if not c.running or c.mode != "paper":
            raise ValueError("예제 초기화 후 모의 실행을 시작하세요.")
        for key in ("KR:000000", "US:DEMO"):
            if key not in c.instruments:
                raise ValueError("먼저 예제 데이터를 초기화하세요.")
            threshold = c.instruments[key].threshold(c.settings)
            await c.tick(key, threshold * Decimal("0.999"))
            await c.tick(key, threshold)
        return c.state()

    app.mount("/static", StaticFiles(directory=ROOT / "app/static", check_dir=False), name="static")
    return app


app = create_app()
