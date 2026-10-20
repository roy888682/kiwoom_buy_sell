"""Small, explicit Kiwoom REST adapter; no SDK, stored tokens, or order retries.

Contracts checked against Kiwoom-Securities/Kiwoom-REST-API commit
953e5dbff123f437ab4d11a78a95191a685eb51f, kiwoom/_data/kiwoom_api_spec.json.
https://github.com/Kiwoom-Securities/Kiwoom-REST-API
All monetary values crossing this module's public boundary are decimal strings.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
import re
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx


class KiwoomError(RuntimeError):
    """A rejected request or an unusable broker response, without secrets."""


class OrderOutcomeUnknown(KiwoomError):
    """The broker may have received an order: reconcile, NEVER blindly retry."""


def number(value: Any, *, absolute: bool = False) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        raise KiwoomError("키움 응답의 필수 숫자가 없거나 올바르지 않습니다.") from None
    if not result.is_finite():
        raise KiwoomError("키움 응답의 숫자가 유한하지 않습니다.")
    return abs(result) if absolute else result


def count(value: Any) -> int:
    result = number(value)
    if result < 0 or result != result.to_integral_value():
        raise KiwoomError("키움 응답의 수량이 올바르지 않습니다.")
    return int(result)


def iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None


def _market(instrument: dict) -> tuple[str, str, str]:
    market = instrument.get("market", "")
    symbol = str(instrument.get("symbol", "")).strip().upper()
    exchange = instrument.get("exchange") or ("KRX" if market == "KR" else "")
    if market == "KR":
        if not re.fullmatch(r"[0-9A-Z]{6}", symbol) or exchange != "KRX":
            raise KiwoomError("국내 종목은 KRX 6자리 영문·숫자 종목코드여야 합니다.")
    elif market == "US":
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", symbol) or exchange not in {"ND", "NY", "NA"}:
            raise KiwoomError("미국 종목은 종목코드와 거래소 ND/NY/NA가 필요합니다.")
    else:
        raise KiwoomError("지원 시장은 KR 및 US입니다.")
    return market, symbol, exchange


class KiwoomClient:
    kind = "kiwoom"

    def __init__(
        self,
        environment: str | None = None,
        app_key: str | None = None,
        secret_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = 0.25,
        timeout: float = 15.0,
        us_timestamp_tz: str | None = None,
    ):
        self.env = environment or os.getenv("KIWOOM_ENV", "mock")
        if self.env not in {"mock", "live"}:
            raise KiwoomError("KIWOOM_ENV는 mock 또는 live여야 합니다.")
        self.environment = self.env
        self._app_key = app_key if app_key is not None else os.getenv("KIWOOM_APP_KEY", "")
        self._secret_key = secret_key if secret_key is not None else os.getenv("KIWOOM_SECRET_KEY", "")
        self.base_url = "https://mockapi.kiwoom.com" if self.env == "mock" else "https://api.kiwoom.com"
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)
        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._auth_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0
        self._min_interval = max(0.0, min_interval)
        configured_tz = us_timestamp_tz or os.getenv("KIWOOM_US_TIMESTAMP_TZ")
        if configured_tz and configured_tz not in {"Asia/Seoul", "America/New_York"}:
            raise KiwoomError("미국 틱 시간대는 브로커와 확인한 Asia/Seoul 또는 America/New_York이어야 합니다.")
        self._us_timestamp_tz = configured_tz

    @property
    def configured(self) -> bool:
        return bool(self._app_key and self._secret_key)

    @property
    def capabilities(self) -> dict:
        return {
            "markets": ["KR", "US"], "assets": ["STOCK", "ETF"],
            "exchange_codes": {"KR": ["KRX"], "US": ["ND", "NY", "NA"]},
            "environment": self.env, "configured": self.configured,
            "history": "adjusted daily; verified full listing coverage required",
            "us_listing_date_in_master": False,
            "us_timestamp_timezone_confirmed": bool(self._us_timestamp_tz),
            "us_price_service": "NASDAQ TotalView free service per official REST documentation",
            "order_retries": False,
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self, api_id: str, path: str, body: dict,
        *, token: str | None = None, continuation: tuple[str, str] | None = None,
        mutation: bool = False,
    ) -> tuple[dict, httpx.Headers]:
        headers = {"api-id": api_id, "content-type": "application/json;charset=UTF-8"}
        if token:
            headers["authorization"] = "Bearer " + token
        if continuation:
            headers["cont-yn"], headers["next-key"] = continuation
        async with self._request_lock:
            delay = self._min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()
            try:
                response = await self._client.post(path, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                error = OrderOutcomeUnknown if mutation else KiwoomError
                raise error(f"{api_id}: 통신 또는 응답 오류입니다." +
                            (" 주문 접수 여부를 조회하기 전 재주문하지 마세요." if mutation else "")) from None
        if not isinstance(data, dict) or "return_code" not in data:
            error = OrderOutcomeUnknown if mutation else KiwoomError
            raise error(f"{api_id}: 응답 결과코드가 없어 처리 결과를 확인할 수 없습니다.")
        code = str(data["return_code"])
        if code != "0":
            # Never echo raw response bodies, headers, keys, or vendor messages.
            safe_code = code if re.fullmatch(r"-?[0-9]{1,8}", code) else "unknown"
            raise KiwoomError(f"{api_id}: 키움이 요청을 거절했습니다 (코드 {safe_code}).")
        return data, response.headers

    async def authenticate(self) -> dict:
        if not self.configured:
            raise KiwoomError("환경 변수 KIWOOM_APP_KEY와 KIWOOM_SECRET_KEY가 필요합니다.")
        async with self._auth_lock:
            if self._token and self._expires_at > datetime.now(timezone.utc) + timedelta(minutes=1):
                return {"connected": True, "environment": self.env}
            data, _ = await self._request("au10001", "/oauth2/token", {
                "grant_type": "client_credentials", "appkey": self._app_key, "secretkey": self._secret_key,
            })
            token = data.get("token")
            expiry = str(data.get("expires_dt", ""))
            try:
                expires_at = datetime.strptime(expiry, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
            except ValueError:
                raise KiwoomError("토큰 만료일 응답을 확인할 수 없습니다.") from None
            if not isinstance(token, str) or not token or expires_at <= datetime.now(timezone.utc):
                raise KiwoomError("유효한 접근 토큰을 발급받지 못했습니다.")
            self._token = token
            self._expires_at = expires_at
        return {"connected": True, "environment": self.env}

    async def probe(self) -> dict:
        return await self.authenticate()

    async def _call(self, api_id: str, path: str, body: dict, **kwargs) -> tuple[dict, httpx.Headers]:
        await self.authenticate()
        return await self._request(api_id, path, body, token=self._token, **kwargs)

    async def _pages(self, api_id: str, path: str, body: dict, key: str, *, max_pages: int = 1000) -> list[dict]:
        rows: list[dict] = []
        continuation = None
        seen: set[str] = set()
        for _ in range(max_pages):
            data, headers = await self._call(api_id, path, body, continuation=continuation)
            page = data.get(key)
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise KiwoomError(f"{api_id}: 필수 목록 응답을 확인할 수 없습니다.")
            rows.extend(page)
            if headers.get("cont-yn", "N") != "Y":
                return rows
            next_key = headers.get("next-key", "")
            if not next_key or next_key in seen or not page:
                raise KiwoomError(f"{api_id}: 연속조회가 진행되지 않아 불완전한 자료를 사용하지 않습니다.")
            seen.add(next_key)
            continuation = ("Y", next_key)
        raise KiwoomError(f"{api_id}: 연속조회 한도를 넘었습니다. 불완전한 자료를 사용하지 않습니다.")

    async def universe(self, markets: Iterable[str] = ("KR", "US")) -> list[dict]:
        selected = set(markets)
        if selected - {"KR", "US"}:
            raise KiwoomError("지원하지 않는 시장입니다.")
        instruments: dict[tuple[str, str], dict] = {}
        if "KR" in selected:
            for group in ("0", "10", "8"):
                rows = await self._pages("ka10099", "/api/dostk/stkinfo", {"mrkt_tp": group}, "list")
                for row in rows:
                    item = {
                        "market": "KR", "symbol": str(row.get("code", "")),
                        "name": row.get("name", ""), "exchange": "KRX", "currency": "KRW",
                        "kind": "ETF" if group == "8" or str(row.get("marketCode")) == "8" else "STOCK",
                        "listing_date": iso_date(row.get("regDay")),
                        "broker_state": row.get("state", ""),
                        "order_warning": str(row.get("orderWarning", "")),
                    }
                    _market(item)
                    instruments[("KR", item["symbol"])] = item
        if "US" in selected:
            # ETF/ETN classification is separate; ETNs are outside the requested universe.
            funds = await self._pages("usa10104", "/api/us/stkinfo", {"stex_tp": "%"}, "list")
            etns = {str(row.get("code")) for row in funds if str(row.get("etn", "")).upper() == "Y"}
            etfs = {str(row.get("code")) for row in funds if str(row.get("etn", "")).upper() == "N"}
            rows = await self._pages("usa10099", "/api/us/stkinfo", {"stex_tp": "%"}, "list")
            for row in rows:
                symbol = str(row.get("stk_cd", "")).upper()
                if symbol in etns:
                    continue
                item = {
                    "market": "US", "symbol": symbol, "exchange": row.get("stex_tp", ""),
                    "name": row.get("stk_nm") or row.get("stk_enm") or symbol, "currency": "USD",
                    "kind": "ETF" if symbol in etfs or str(row.get("isEtf", "")).upper() == "Y" else "STOCK",
                    "listing_date": None,
                }
                _market(item)
                instruments[("US", symbol)] = item
        return sorted(instruments.values(), key=lambda item: (item["market"], item["symbol"]))

    async def quote(self, instrument: dict) -> dict:
        """Use latest timestamped tick, not a receipt timestamp disguised as trade time.

        US chart timestamp timezone is not defined in the published JSON spec.
        Until configured after verification with the broker, timestamp_verified
        is false and automated execution must remain blocked by the caller.
        """
        market, symbol, exchange = _market(instrument)
        if market == "KR":
            data, _ = await self._call("ka10079", "/api/dostk/chart", {
                "stk_cd": symbol, "tic_scope": "1", "upd_stkpc_tp": "0",
            })
            rows = data.get("stk_tic_chart_qry")
            tz = "Asia/Seoul"
        else:
            data, _ = await self._call("usa06010", "/api/us/chart", {
                "stex_tp": exchange, "stk_cd": symbol, "tic_scope": "1",
                "upd_stkpc_tp": "0", "exrt_appl_tp": "0",
            })
            rows = data.get("result_list")
            tz = self._us_timestamp_tz
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise KiwoomError("현재 체결가 자료가 없습니다.")
        row = rows[0]
        price = number(row.get("cur_prc"), absolute=True)
        if price <= 0:
            raise KiwoomError("현재 체결가가 0 이하입니다.")
        raw_time = str(row.get("cntr_tm", ""))
        traded_at = None
        if tz:
            try:
                traded_at = datetime.strptime(raw_time, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo(tz)).isoformat()
            except ValueError:
                pass
        return {
            "market": market, "symbol": symbol, "price": str(price),
            "currency": "KRW" if market == "KR" else "USD",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "market_timestamp": traded_at, "timestamp_verified": traded_at is not None,
            "raw_trade_time": raw_time,
            "timestamp_note": "" if traded_at else "미국 틱 시간대 확인 또는 유효한 체결 시각이 필요합니다.",
        }

    async def history(self, instrument: dict, as_of: date) -> dict:
        """Request today's adjusted vintage, retain completed market days only."""
        market, symbol, exchange = _market(instrument)
        if isinstance(as_of, datetime):
            as_of = as_of.date()
        if market == "KR":
            rows = await self._pages("ka10081", "/api/dostk/chart", {
                "stk_cd": symbol, "base_dt": as_of.strftime("%Y%m%d"), "upd_stkpc_tp": "1",
            }, "stk_dt_pole_chart_qry")
        else:
            rows = await self._pages("usa06012", "/api/us/chart", {
                "stex_tp": exchange, "stk_cd": symbol, "strt_dt": as_of.strftime("%Y%m%d"),
                "upd_stkpc_tp": "1", "exrt_appl_tp": "0",
            }, "result_list")
        by_date: dict[str, dict] = {}
        for row in rows:
            trading_date = iso_date(row.get("dt"))
            if trading_date is None:
                raise KiwoomError("일봉 일자를 확인할 수 없습니다.")
            if trading_date >= as_of.isoformat():
                continue
            high = number(row.get("high_pric"), absolute=True)
            close = number(row.get("cur_prc"), absolute=True)
            if high <= 0 or close <= 0 or high < close:
                raise KiwoomError("수정 일봉 가격을 확인할 수 없습니다.")
            bar = {"date": trading_date, "high": str(high), "close": str(close), "adjusted": True}
            if trading_date in by_date and by_date[trading_date] != bar:
                raise KiwoomError("연속조회에서 동일 일자의 가격이 일치하지 않습니다.")
            by_date[trading_date] = bar
        bars = sorted(by_date.values(), key=lambda row: row["date"])
        listing = iso_date(instrument.get("listing_date"))
        complete = bool(listing and bars and bars[0]["date"] == listing)
        reason = ("상장일과 최초 일봉 일치; 거래일 연속성은 별도 검증 필요" if complete else
                  "상장 이후 전체 가격 이력 또는 검증된 상장일이 부족합니다.")
        return {
            "bars": bars, "adjusted": True, "adjustment_as_of": as_of.isoformat(),
            "history_complete": complete, "reason": reason,
            "high": str(max(number(row["high"]) for row in bars)) if bars else None,
        }

    async def orderable_cash(self, instrument: dict, price: str | Decimal) -> dict:
        market, symbol, exchange = _market(instrument)
        amount = number(price)
        if amount <= 0:
            raise KiwoomError("주문 가능 수량 조회 가격은 양수여야 합니다.")
        if market == "KR":
            data, _ = await self._call("kt00011", "/api/dostk/acnt", {"stk_cd": symbol, "uv": str(amount)})
            cash = min(number(data.get("ord_alowa")), number(data.get("min_ord_alow_amt")))
            quantity = count(data.get("min_ord_alowq"))
        else:
            data, _ = await self._call("ust31490", "/api/us/ordr", {
                "stex_tp": exchange, "stk_cd": symbol, "uv": str(amount),
            })
            if data.get("crnc_code") != "USD":
                raise KiwoomError("미국 주문 가능 금액의 통화가 USD가 아닙니다.")
            # ord_alowa excludes KRW substitution; never use krw_* buying power.
            cash = min(number(data.get("ord_alowa")), number(data.get("min_ord_alowa")))
            quantity = count(data.get("min_ord_alowq"))
        return {"available_cash": str(max(Decimal(0), cash)), "max_quantity": quantity,
                "currency": "KRW" if market == "KR" else "USD", "includes_broker_reservations": True}

    async def cash(self, market: str) -> dict:
        """Account overview; per-symbol orderable_cash is mandatory before buying."""
        if market == "KR":
            data, _ = await self._call("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
            amount = min(number(data.get("ord_alow_amt")), number(data.get("100stk_ord_alow_amt")))
            return {"currency": "KRW", "available_cash": str(max(Decimal(0), amount))}
        if market != "US":
            raise KiwoomError("지원 시장은 KR 및 US입니다.")
        rows = await self._pages("ust21110", "/api/us/acnt", {}, "result_list")
        matching = [row for row in rows if row.get("crnc_code") == "USD"]
        if len(matching) != 1:
            raise KiwoomError("USD 주문 가능 금액을 확인할 수 없습니다.")
        return {"currency": "USD", "available_cash": str(max(Decimal(0), number(matching[0].get("fc_ord_alowa"))))}

    async def holdings(self, market: str) -> list[dict]:
        if market == "KR":
            rows = await self._pages("kt00018", "/api/dostk/acnt", {
                "qry_tp": "1", "dmst_stex_tp": "KRX",
            }, "acnt_evlt_remn_indv_tot")
        elif market == "US":
            rows = await self._pages("ust21070", "/api/us/acnt", {"stex_tp": "", "stk_cd": ""}, "result_list")
        else:
            raise KiwoomError("지원 시장은 KR 및 US입니다.")
        holdings: dict[str, dict] = {}
        for row in rows:
            if market == "KR":
                # Cash-only engine must not accidentally aggregate credit positions.
                if row.get("crd_tp") not in {None, "", "00", "0"}:
                    continue
                symbol = str(row.get("stk_cd", ""))
                if symbol.startswith("A") and len(symbol) == 7:
                    symbol = symbol[1:]
                qty, sellable = count(row.get("rmnd_qty")), count(row.get("trde_able_qty"))
                average = number(row.get("pur_pric"))
            else:
                if row.get("crnc_code") != "USD":
                    raise KiwoomError("미국 보유 종목 통화가 USD가 아닙니다.")
                symbol = str(row.get("stk_cd", ""))
                qty, sellable = count(row.get("poss_qty")), count(row.get("sell_alowq"))
                average = number(row.get("frgn_stk_book_uv"))
            if qty == 0:
                continue
            if average <= 0 or sellable > qty:
                raise KiwoomError("보유 수량 또는 실제 매입 평균가를 확인할 수 없습니다.")
            if symbol in holdings:
                existing = holdings[symbol]
                total_quantity = existing["quantity"] + qty
                weighted = (number(existing["average_price"]) * existing["quantity"] + average * qty) / total_quantity
                existing.update(quantity=total_quantity, sellable_quantity=existing["sellable_quantity"] + sellable,
                                average_price=str(weighted))
            else:
                holdings[symbol] = {
                    "market": market, "symbol": symbol, "quantity": qty, "sellable_quantity": sellable,
                    "average_price": str(average), "name": row.get("stk_nm") or row.get("frgn_stk_nm") or symbol,
                    "currency": "KRW" if market == "KR" else "USD",
                }
        return list(holdings.values())

    async def submit_order(self, instrument: dict, side: str, quantity: int,
                           limit_price: str | Decimal | None = None) -> dict:
        market, symbol, exchange = _market(instrument)
        if side not in {"buy", "sell"} or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise KiwoomError("주문 방향과 정수 수량을 확인하세요.")
        price = None if limit_price is None else number(limit_price)
        if price is not None and price <= 0:
            raise KiwoomError("지정가는 양수여야 합니다.")
        if market == "KR":
            api_id = "kt10000" if side == "buy" else "kt10001"
            path = "/api/dostk/ordr"
            body = {"dmst_stex_tp": exchange, "stk_cd": symbol, "ord_qty": str(quantity),
                    "ord_uv": "" if price is None else str(price), "trde_tp": "3" if price is None else "0", "cond_uv": ""}
        else:
            api_id = "ust20000" if side == "buy" else "ust20001"
            path = "/api/us/ordr"
            body = {"stex_tp": exchange, "stk_cd": symbol, "ord_qty": str(quantity),
                    "ord_uv": "" if price is None else str(price), "trde_tp": "03" if price is None else "00"}
        data, _ = await self._call(api_id, path, body, mutation=True)
        order_id = str(data.get("ord_no") or "").strip()
        if not order_id:
            raise OrderOutcomeUnknown("키움 성공 응답에 주문번호가 없습니다. 주문내역 확인 전 재주문하지 마세요.")
        return {"order_id": order_id, "status": "accepted", "market": market,
                "symbol": symbol, "side": side, "quantity": quantity,
                "limit_price": None if price is None else str(price), "filled_quantity": 0}

    @staticmethod
    def _normal_order(row: dict, market: str) -> dict:
        quantity = count(row.get("ord_qty"))
        filled = count(row.get("cntr_qty", "0"))
        remaining = count(row.get("ord_remnq", row.get("oso_qty", "0")))
        cancelled = count(row.get("cncl_qty", "0"))
        state = str(row.get("ord_stat") or row.get("ord_stat_nm") or row.get("ord_stt") or row.get("acpt_tp") or "")
        symbol = str(row.get("stk_cd", ""))
        if market == "KR" and symbol.startswith("A") and len(symbol) == 7:
            symbol = symbol[1:]
        if filled > quantity or remaining > quantity:
            raise KiwoomError("주문 체결 수량 응답이 올바르지 않습니다.")
        if filled == quantity and quantity:
            status = "filled"
        elif "거부" in state or "실패" in state or row.get("text1"):
            status = "rejected"
        elif cancelled and remaining == 0 or "취소" in state and remaining == 0:
            status = "canceled"
        elif filled > 0 and remaining > 0:
            status = "partial"
        elif remaining > 0:
            status = "pending"
        else:
            status = "unknown"
        raw_average = row.get("cntr_uv", row.get("cntr_pric", "0"))
        average = number(raw_average or "0", absolute=True)
        return {"order_id": str(row.get("ord_no", "")), "market": market, "symbol": symbol,
                "status": status, "quantity": quantity, "filled_quantity": filled,
                "remaining_quantity": remaining, "cancelled_quantity": cancelled,
                "average_fill_price": str(average), "broker_state": state,
                "side": "buy" if str(row.get("slby_tp", row.get("trde_tp", ""))) == "2" or
                                  "매수" in str(row.get("slby_tp_nm", row.get("io_tp_nm", ""))) else "sell",
                "fees": None}

    async def open_orders(self, market: str) -> list[dict]:
        if market == "KR":
            rows = await self._pages("ka10075", "/api/dostk/acnt", {
                "all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0",
            }, "oso")
        elif market == "US":
            rows = await self._pages("ust21050", "/api/us/acnt", {
                "ord_dt": "", "slby_tp": "0", "stex_tp": "", "stk_cd": "",
            }, "result_list")
        else:
            raise KiwoomError("지원 시장은 KR 및 US입니다.")
        return [self._normal_order(row, market) for row in rows]

    async def order_status(self, instrument: dict, order_id: str, order_date: str | None = None) -> dict:
        market, symbol, exchange = _market(instrument)
        if market == "KR":
            rows = await self._pages("kt00007", "/api/dostk/acnt", {
                "ord_dt": (order_date or "").replace("-", ""), "qry_tp": "1", "stk_bond_tp": "1", "sell_tp": "0",
                "stk_cd": symbol, "fr_ord_no": "", "dmst_stex_tp": "%",
            }, "acnt_ord_cntr_prps_dtl")
        else:
            # ust21510 avoids the documented example's misspelt result_lsit in ust21150.
            # With a supplied historical date, use the specification's result_list.
            if order_date:
                rows = await self._pages("ust21150", "/api/us/acnt", {
                    "ord_dt": order_date.replace("-", ""), "query_tp": "1", "slby_tp": "0", "stex_tp": exchange,
                    "stk_cd": symbol, "oppo_trde_tp": "%", "fr_ord_no": "",
                }, "result_list")
            else:
                rows = await self._pages("ust21510", "/api/us/acnt", {
                    "slby_tp": "0", "stex_tp": exchange, "stk_cd": symbol,
                }, "result_list")
        matching = [row for row in rows if str(row.get("ord_no", "")).lstrip("0") == str(order_id).lstrip("0")]
        if not matching:
            raise KiwoomError("해당 주문번호를 조회하지 못했습니다. 주문을 다시 내지 말고 계좌 주문내역을 확인하세요.")
        if len(matching) > 1:
            raise KiwoomError("동일 주문번호가 여러 번 반환되어 체결 확인이 필요합니다.")
        return self._normal_order(matching[0], market)

    async def cancel_order(self, instrument: dict, order_id: str, quantity: int = 0) -> dict:
        market, symbol, exchange = _market(instrument)
        if not order_id or not re.fullmatch(r"[0-9]+", str(order_id)) or quantity < 0:
            raise KiwoomError("취소할 원주문번호와 수량을 확인하세요.")
        if market == "KR":
            data, _ = await self._call("kt10003", "/api/dostk/ordr", {
                "dmst_stex_tp": exchange, "orig_ord_no": str(order_id), "stk_cd": symbol, "cncl_qty": str(quantity),
            }, mutation=True)
        else:
            data, _ = await self._call("ust20003", "/api/us/ordr", {
                "orig_ord_no": str(order_id), "stex_tp": exchange, "stk_cd": symbol,
            }, mutation=True)
        cancel_id = str(data.get("ord_no") or "").strip()
        if not cancel_id:
            raise OrderOutcomeUnknown("취소 요청 응답에 주문번호가 없습니다. 실제 취소 여부를 조회하세요.")
        return {"order_id": str(order_id), "cancel_order_id": cancel_id, "status": "cancel_requested"}
