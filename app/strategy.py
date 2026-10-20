"""Broker-neutral, Decimal-based breakout strategy and cumulative fill ledger.

Cash is gross unreserved cash. ``available_cash`` subtracts this ledger's pending
buy reserves; do not feed a broker's already-net orderable amount into ``cash``
without adding its pending reservations back. Submission never implies a fill.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any
from uuid import uuid4

ZERO = Decimal("0")
HUNDRED = Decimal("100")
OPEN_STATUSES = frozenset({"pending", "partial", "unknown"})
FINAL_STATUSES = frozenset({"filled", "canceled", "rejected"})


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("숫자에 참/거짓을 사용할 수 없습니다.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("올바른 숫자를 입력하세요.") from exc
    if not result.is_finite():
        raise ValueError("유한한 숫자를 입력하세요.")
    # Keep user-supplied exponents inside practical cash-equity ranges.
    # Decimal arithmetic is exact but absurd exponents can otherwise overflow
    # in downstream multiplication, turning a settings error into a server error.
    magnitude = result.copy_abs()
    if magnitude > Decimal("1e18") or (magnitude and magnitude < Decimal("1e-12")):
        raise ValueError("숫자는 절댓값 10^18 이하, 0이 아니면 10^-12 이상이어야 합니다.")
    return result


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_date(value: date | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(value) if isinstance(value, str) else value


def as_datetime(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def serialize(value: Any) -> Any:
    """Convert dataclasses and exact numeric values into JSON-safe primitives."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return {f.name: serialize(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    return value


@dataclass
class Settings:
    breakout_percent: Decimal = Decimal("2")
    allocation_percent: Decimal = Decimal("30")
    stop_loss_percent: Decimal = Decimal("5")
    allow_pyramiding: bool = False
    reentry_cooldown_minutes: int = 60
    max_positions: int = 5
    fee_reserve_percent: Decimal = Decimal("0.5")
    poll_interval_seconds: int = 15
    universe_mode: str = "all"

    def __post_init__(self) -> None:
        for name in ("breakout_percent", "allocation_percent", "stop_loss_percent", "fee_reserve_percent"):
            setattr(self, name, decimal(getattr(self, name)))
        if self.breakout_percent <= -HUNDRED:
            raise ValueError("돌파율은 -100%보다 커야 합니다.")
        if not ZERO < self.allocation_percent <= HUNDRED:
            raise ValueError("매입 비율은 0% 초과, 100% 이하여야 합니다.")
        if not ZERO < self.stop_loss_percent < HUNDRED:
            raise ValueError("손절률은 0% 초과, 100% 미만이어야 합니다.")
        if not ZERO <= self.fee_reserve_percent <= HUNDRED:
            raise ValueError("수수료 여유율은 0~100%여야 합니다.")
        if not isinstance(self.allow_pyramiding, bool):
            raise ValueError("추가 매수 허용 여부는 참/거짓이어야 합니다.")
        for name, minimum, maximum in (("reentry_cooldown_minutes", 0, 525600), ("max_positions", 1, 10000), ("poll_interval_seconds", 1, 3600)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name}: {minimum}~{maximum} 사이 정수여야 합니다.")
        if self.universe_mode not in {"all", "watchlist"}:
            raise ValueError("종목 검색 범위를 확인하세요.")


@dataclass
class Instrument:
    market: str
    symbol: str
    name: str = ""
    all_time_high: Decimal | None = None
    history_through: date | None = None
    history_verified: bool = False
    previous_price: Decimal | None = None
    current_price: Decimal | None = None

    def __post_init__(self) -> None:
        self.market = self.market.upper()
        self.symbol = self.symbol.strip().upper()
        if self.market not in {"KR", "US"} or not self.symbol:
            raise ValueError("종목의 시장과 코드를 확인하세요.")
        for name in ("all_time_high", "previous_price", "current_price"):
            value = getattr(self, name)
            if value is not None:
                value = decimal(value)
                if value <= ZERO:
                    raise ValueError("가격은 0보다 커야 합니다.")
                setattr(self, name, value)
        self.history_through = as_date(self.history_through)
        if not isinstance(self.history_verified, bool):
            raise ValueError("가격 이력 확인 상태는 참/거짓이어야 합니다.")

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"

    @property
    def currency(self) -> str:
        return "KRW" if self.market == "KR" else "USD"

    def threshold(self, settings: Settings) -> Decimal | None:
        if self.all_time_high is None:
            return None
        return self.all_time_high * (1 + settings.breakout_percent / HUNDRED)


@dataclass
class Position:
    market: str
    symbol: str
    name: str
    quantity: int
    average_price: Decimal
    current_price: Decimal

    def __post_init__(self) -> None:
        self.average_price = decimal(self.average_price)
        self.current_price = decimal(self.current_price)
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 0:
            raise ValueError("보유 수량은 0 이상의 정수여야 합니다.")
        if self.average_price <= ZERO or self.current_price <= ZERO:
            raise ValueError("보유 평균가와 현재가는 양수여야 합니다.")

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"

    @property
    def currency(self) -> str:
        return "KRW" if self.market == "KR" else "USD"

    def stop_price(self, settings: Settings) -> Decimal:
        return self.average_price * (1 - settings.stop_loss_percent / HUNDRED)


@dataclass
class Decision:
    market: str
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal
    reason: str

    def __post_init__(self) -> None:
        self.limit_price = decimal(self.limit_price)
        if self.side not in {"BUY", "SELL"} or isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0 or self.limit_price <= ZERO:
            raise ValueError("주문 방향·수량·가격을 확인하세요.")

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"


@dataclass
class Order:
    id: str
    created_at: datetime
    market: str
    symbol: str
    name: str
    side: str
    quantity: int
    limit_price: Decimal
    fee_reserve_percent: Decimal
    currency: str
    reason: str
    status: str = "pending"
    filled_quantity: int = 0
    average_fill_price: Decimal = ZERO
    fee_paid: Decimal = ZERO

    def __post_init__(self) -> None:
        self.created_at = as_datetime(self.created_at)
        for name in ("limit_price", "fee_reserve_percent", "average_fill_price", "fee_paid"):
            setattr(self, name, decimal(getattr(self, name)))
        if self.status not in OPEN_STATUSES | FINAL_STATUSES:
            raise ValueError("주문 상태를 확인하세요.")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("주문 수량은 양의 정수여야 합니다.")
        if not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError("체결 수량을 확인하세요.")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("주문 방향을 확인하세요.")
        if self.limit_price <= ZERO or self.fee_reserve_percent < ZERO or self.fee_paid < ZERO:
            raise ValueError("주문 가격과 비용을 확인하세요.")

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"

    @property
    def reserved_cash(self) -> Decimal:
        if self.side != "BUY" or self.status not in OPEN_STATUSES:
            return ZERO
        return (self.quantity - self.filled_quantity) * self.limit_price * (1 + self.fee_reserve_percent / HUNDRED)


@dataclass
class Portfolio:
    cash: dict[str, Decimal] = field(default_factory=lambda: {"KRW": ZERO, "USD": ZERO})
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    stopped_at: dict[str, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = {k: decimal(v) for k, v in self.cash.items()}
        for currency in ("KRW", "USD"):
            self.cash.setdefault(currency, ZERO)
        self.stopped_at = {k: as_datetime(v) for k, v in self.stopped_at.items()}

    def available_cash(self, currency: str) -> Decimal:
        reserved = sum((o.reserved_cash for o in self.orders.values() if o.currency == currency), ZERO)
        return max(ZERO, self.cash.get(currency, ZERO) - reserved)

    def pending_orders(self, key: str, side: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.key == key and o.status in OPEN_STATUSES and (side is None or o.side == side)]

    def occupied_keys(self) -> set[str]:
        return {key for key, p in self.positions.items() if p.quantity > 0} | {o.key for o in self.orders.values() if o.side == "BUY" and o.status in OPEN_STATUSES}


class Strategy:
    def __init__(self, settings: Settings | None = None, portfolio: Portfolio | None = None) -> None:
        self.settings = settings or Settings()
        self.portfolio = portfolio or Portfolio()

    def evaluate_tick(self, instrument: Instrument, price: Any, expected_history_date: date | str, now: datetime | None = None) -> Decision | None:
        """Observe one fresh quote; buys require an actual upward crossing.

        The orchestrator supplies the exchange's confirmed previous trading day,
        not simply yesterday. A missing/old ATH blocks buys, never risk exits.
        On a stop decision cancel outstanding buys through the broker before
        submission, reconcile their fills, then reevaluate liquidation quantity.
        """
        price = decimal(price)
        if price <= ZERO:
            raise ValueError("시세는 0보다 커야 합니다.")
        now = as_datetime(now or utcnow())
        previous = instrument.current_price
        instrument.previous_price = previous
        instrument.current_price = price
        position = self.portfolio.positions.get(instrument.key)
        if position and position.quantity > 0:
            position.current_price = price
            if price <= position.stop_price(self.settings):
                if not self.portfolio.pending_orders(instrument.key, "SELL"):
                    return Decision(instrument.market, instrument.symbol, "SELL", position.quantity, price, "stop_loss")
                return None
        if self.portfolio.pending_orders(instrument.key):
            return None
        if position and position.quantity > 0 and not self.settings.allow_pyramiding:
            return None
        if not instrument.history_verified or instrument.history_through != as_date(expected_history_date):
            return None
        threshold = instrument.threshold(self.settings)
        if previous is None or threshold is None or not previous < threshold <= price:
            return None
        stopped = self.portfolio.stopped_at.get(instrument.key)
        if stopped and now < stopped + timedelta(minutes=self.settings.reentry_cooldown_minutes):
            return None
        if instrument.key not in self.portfolio.occupied_keys() and len(self.portfolio.occupied_keys()) >= self.settings.max_positions:
            return None
        available = self.portfolio.available_cash(instrument.currency)
        budget = available * self.settings.allocation_percent / HUNDRED
        unit_cost = price * (1 + self.settings.fee_reserve_percent / HUNDRED)
        quantity = int((budget / unit_cost).to_integral_value(rounding=ROUND_FLOOR))
        if quantity <= 0:
            return None
        return Decision(instrument.market, instrument.symbol, "BUY", quantity, price, "breakout")

    def submit_order(self, instrument: Instrument, decision: Decision, order_id: str | None = None, now: datetime | None = None) -> Order:
        """Reserve an accepted intent before network I/O; caller handles errors.

        Mark ambiguous network outcomes ``unknown`` and reconcile them instead
        of retrying: unknown orders retain their reserves and deduplication.
        """
        if decision.key != instrument.key:
            raise ValueError("시세 종목과 주문 종목이 다릅니다.")
        if self.portfolio.pending_orders(instrument.key, decision.side):
            raise ValueError("이 종목에는 같은 방향의 미체결 주문이 있습니다.")
        if decision.side == "BUY":
            if self.portfolio.pending_orders(instrument.key):
                raise ValueError("이 종목에는 미체결 주문이 있습니다.")
            position = self.portfolio.positions.get(instrument.key)
            if position and position.quantity and not self.settings.allow_pyramiding:
                raise ValueError("종목별 추가 매수가 꺼져 있습니다.")
            occupied = self.portfolio.occupied_keys()
            if instrument.key not in occupied and len(occupied) >= self.settings.max_positions:
                raise ValueError("최대 보유 종목 수에 도달했습니다.")
            reserve = decision.quantity * decision.limit_price * (1 + self.settings.fee_reserve_percent / HUNDRED)
            if reserve > self.portfolio.available_cash(instrument.currency):
                raise ValueError("주문 가능 현금이 부족합니다.")
        else:
            position = self.portfolio.positions.get(instrument.key)
            if not position or decision.quantity > position.quantity:
                raise ValueError("매도 수량이 보유 수량보다 많습니다.")
        order_id = order_id or uuid4().hex
        if order_id in self.portfolio.orders:
            raise ValueError("중복 주문 번호입니다.")
        order = Order(order_id, now or utcnow(), instrument.market, instrument.symbol, instrument.name, decision.side, decision.quantity, decision.limit_price, self.settings.fee_reserve_percent, instrument.currency, decision.reason)
        self.portfolio.orders[order.id] = order
        return order

    def apply_order_update(self, order_id: str, filled_quantity: int, average_fill_price: Any, status: str = "partial", cumulative_fee: Any = ZERO, now: datetime | None = None) -> Order:
        """Apply broker cumulative fills exactly once, including late fills.

        Fees are the actual cumulative broker fees (zero if not yet reported).
        Terminal orders may still receive a later confirmed fill; stale updates
        cannot reopen final orders or move cumulative executions backwards.
        """
        order = self.portfolio.orders[order_id]
        if status not in OPEN_STATUSES | FINAL_STATUSES:
            raise ValueError("알 수 없는 주문 상태입니다.")
        if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int) or not order.filled_quantity <= filled_quantity <= order.quantity:
            raise ValueError("누적 체결 수량은 감소하거나 주문 수량을 넘을 수 없습니다.")
        average = decimal(average_fill_price)
        fee = decimal(cumulative_fee)
        if fee < order.fee_paid or fee < ZERO:
            raise ValueError("누적 비용은 감소할 수 없습니다.")
        if filled_quantity > 0 and average <= ZERO:
            raise ValueError("체결 평균가는 0보다 커야 합니다.")
        if filled_quantity == 0 and average != ZERO:
            raise ValueError("체결이 없으면 평균 체결가는 0이어야 합니다.")
        if status == "filled" and filled_quantity != order.quantity:
            raise ValueError("완료 주문의 수량이 주문 수량과 다릅니다.")
        old_notional = order.filled_quantity * order.average_fill_price
        new_notional = filled_quantity * average
        delta_quantity = filled_quantity - order.filled_quantity
        delta_notional = new_notional - old_notional
        delta_fee = fee - order.fee_paid
        if delta_quantity == 0 and delta_notional != ZERO:
            raise ValueError("새 체결 없이 평균 체결가를 바꿀 수 없습니다.")
        if delta_quantity > 0 and delta_notional <= ZERO:
            raise ValueError("추가 체결 금액은 0보다 커야 합니다.")
        position = self.portfolio.positions.get(order.key)
        if delta_quantity:
            if order.side == "BUY":
                if position:
                    total = position.quantity + delta_quantity
                    position.average_price = (position.quantity * position.average_price + delta_notional) / total
                    position.quantity = total
                    position.current_price = delta_notional / delta_quantity
                else:
                    self.portfolio.positions[order.key] = Position(order.market, order.symbol, order.name, delta_quantity, delta_notional / delta_quantity, delta_notional / delta_quantity)
                self.portfolio.cash[order.currency] -= delta_notional
            else:
                if not position or delta_quantity > position.quantity:
                    raise ValueError("매도 체결 수량이 현재 보유 수량을 넘습니다. 잔고를 재조회하세요.")
                position.quantity -= delta_quantity
                position.current_price = delta_notional / delta_quantity
                if not position.quantity:
                    del self.portfolio.positions[order.key]
                self.portfolio.cash[order.currency] += delta_notional
                if order.reason == "stop_loss":
                    self.portfolio.stopped_at[order.key] = as_datetime(now or utcnow())
        self.portfolio.cash[order.currency] -= delta_fee
        order.filled_quantity = filled_quantity
        order.average_fill_price = average
        order.fee_paid = fee
        if filled_quantity == order.quantity:
            order.status = "filled"
        elif order.status in FINAL_STATUSES and status in OPEN_STATUSES:
            pass
        else:
            order.status = status
        return order
