"""Read-only portfolio marks, kept separate for each settlement currency.

Persisted prices remain useful after a stop or restart, but only a verified
provider tick received by this process can make a mark fresh. Cash is the
ledger's gross cash: pending buy reservations affect available cash only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

from .strategy import Portfolio, Settings, serialize


ZERO = Decimal("0")
HUNDRED = Decimal("100")
PROVIDER_SOURCES = frozenset({"kiwoom", "kiwoom_ws"})


def _price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > ZERO else None


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _precision(portfolio, instruments, metadata, settings) -> int:
    """Allow multiplication and addition without rounding supplied money."""
    values = list(portfolio.cash.values()) + [settings.stop_loss_percent]
    quantity_digits = 1
    for position in portfolio.positions.values():
        values.extend((position.average_price, position.current_price))
        quantity_digits = max(quantity_digits, len(str(position.quantity)))
        instrument = instruments.get(position.key)
        if instrument is not None:
            values.append(instrument.current_price)
        values.append(metadata.get(position.key, {}).get("last_quote_price"))
    for order in portfolio.orders.values():
        values.extend((order.limit_price, order.fee_reserve_percent))
        quantity_digits = max(quantity_digits, len(str(order.quantity)))
    integer_places, fraction_places = 1, 0
    for value in values:
        try:
            number = Decimal(str(value)).copy_abs() if not isinstance(value, bool) else None
        except (InvalidOperation, TypeError, ValueError):
            number = None
        if number is not None and not number.is_finite():
            number = None
        if number is not None:
            integer_places = max(integer_places, number.adjusted() + 1)
            fraction_places = max(fraction_places, -number.as_tuple().exponent)
    count_digits = len(str(len(portfolio.positions) + len(portfolio.orders) + 1))
    return max(64, 3 * (integer_places + fraction_places) + quantity_digits + count_digits + 20)


def _percent(pnl: Decimal, cost: Decimal) -> Decimal:
    return pnl / cost * HUNDRED if cost else ZERO


def value_portfolio(
    portfolio: Portfolio,
    instruments: Mapping,
    metadata: Mapping,
    last_ticks: Mapping,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    real_market: bool = False,
) -> dict:
    """Return JSON-safe positions and complete/partial KRW and USD valuations.

    Unknown marks make the complete market value, profit and total assets null;
    ``known_market_value`` retains the priced subset. Cost basis and cash are
    independently known. Stale marks retain values with an explicit status.
    """
    settings = settings or Settings()
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    clock = clock.astimezone(timezone.utc)
    positions = []
    totals = {
        currency: {
            "known_market_value": ZERO,
            "cost_basis": ZERO,
            "priced_positions": 0,
            "unpriced_positions": 0,
            "stale_positions": 0,
            "valuation_status": "fresh",
        }
        for currency in ("KRW", "USD")
    }
    with localcontext() as context:
        context.prec = _precision(portfolio, instruments, metadata, settings)
        for key, position in portfolio.positions.items():
            if position.quantity <= 0:
                continue
            instrument = instruments.get(key)
            meta = metadata.get(key, {})
            source = meta.get("last_quote_source")
            verified_provider = source in PROVIDER_SOURCES and meta.get("last_quote_verified") is True
            simulation = not real_market and not verified_provider and meta.get("source", "manual") in {"demo", "manual"}
            quote_at = _timestamp(meta.get("last_quote_at"))
            active_at = _timestamp(last_ticks.get(key))
            if quote_at is None and verified_provider:
                quote_at = active_at

            # Metadata survives resets of an instrument's crossing detector.
            # A position's persisted mark also survives an application restart.
            provider_price = _price(meta.get("last_quote_price")) if verified_provider else None
            current_price = provider_price
            if current_price is None and not real_market and instrument is not None:
                current_price = _price(instrument.current_price)
            if current_price is None:
                current_price = _price(position.current_price)

            age = (clock - quote_at).total_seconds() if quote_at is not None else None
            active_age = (clock - active_at).total_seconds() if active_at is not None else None
            if current_price is None:
                status = "unavailable"
            elif simulation:
                status = "simulation"
            elif provider_price is not None and active_age is not None and -5 <= active_age <= 90 and age is not None and -5 <= age <= 90:
                status = "fresh"
            else:
                status = "stale"
            price_source = source if provider_price is not None else (meta.get("source", "manual") if simulation else "stored")
            cost_basis = position.average_price * position.quantity
            market_value = current_price * position.quantity if current_price is not None else None
            pnl = market_value - cost_basis if market_value is not None else None
            row = {
                **serialize(position),
                "key": key,
                "name": instrument.name if instrument is not None and instrument.name else position.name,
                "currency": position.currency,
                "stop_price": position.stop_price(settings),
                "current_price": current_price,
                "market_value": market_value,
                "cost_basis": cost_basis,
                "unrealized_pnl": pnl,
                "return_percent": _percent(pnl, cost_basis) if pnl is not None else None,
                "quote_asof": quote_at,
                "quote_age_seconds": age,
                "valuation_status": status,
                "price_source": price_source,
            }
            positions.append(row)
            bucket = totals[position.currency]
            bucket["cost_basis"] += cost_basis
            if market_value is None:
                bucket["unpriced_positions"] += 1
                bucket["valuation_status"] = "unavailable"
            else:
                bucket["known_market_value"] += market_value
                bucket["priced_positions"] += 1
                if status == "stale":
                    bucket["stale_positions"] += 1
                    if bucket["valuation_status"] != "unavailable":
                        bucket["valuation_status"] = "stale"
                elif status == "simulation" and bucket["valuation_status"] == "fresh":
                    bucket["valuation_status"] = "simulation"

        for currency, bucket in totals.items():
            cash = portfolio.cash.get(currency, ZERO)
            market_value = bucket["known_market_value"] if not bucket["unpriced_positions"] else None
            pnl = market_value - bucket["cost_basis"] if market_value is not None else None
            bucket.update(
                market_value=market_value,
                unrealized_pnl=pnl,
                return_percent=_percent(pnl, bucket["cost_basis"]) if pnl is not None else None,
                cash_balance=cash,
                available_cash=portfolio.available_cash(currency),
                total_assets=cash + market_value if market_value is not None else None,
            )
        return serialize({"positions": positions, "valuation": totals})
