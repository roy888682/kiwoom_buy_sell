"""Exchange sessions and conservative, split-adjusted all-time-high inputs.

The historical high is frozen for the local market day: today's bars never enter
the reference, even after that market closes. Imported ``complete_from_listing``
is an explicit statement by the data supplier, not independent data verification.
Missing sessions, stale final bars or unadjusted prices fail closed for entries.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


_MARKETS = {
    "KR": ("XKRX", ZoneInfo("Asia/Seoul")),
    "US": ("XNYS", ZoneInfo("America/New_York")),
}
_REQUIRED_COLUMNS = {
    "market", "symbol", "name", "date", "high", "close", "adjusted",
    "complete_from_listing",
}


def _market(market: str) -> str:
    result = str(market).upper()
    if result not in _MARKETS:
        raise ValueError("market must be KR or US")
    return result


def _instant(now: datetime | None = None) -> datetime:
    result = now or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return result


def market_day(market: str, now: datetime | None = None) -> date:
    """Return today's date in the selected market's local timezone."""
    market = _market(market)
    return _instant(now).astimezone(_MARKETS[market][1]).date()


@lru_cache(maxsize=32)
def _calendar(market: str, start_year: int, end_year: int):
    # Deliberately request bounds: exchange_calendars defaults have a moving,
    # relatively short history and can silently be unsuitable for old listings.
    name = _MARKETS[_market(market)][0]
    return xcals.get_calendar(
        name,
        start=f"{start_year:04d}-01-01",
        end=f"{end_year:04d}-12-31",
    )


def _calendar_for(market: str, start: date, end: date):
    return _calendar(_market(market), start.year - 1, end.year + 1)


def previous_session(market: str, now: datetime | None = None) -> date:
    """Last exchange session strictly before the local current calendar day."""
    market = _market(market)
    today = market_day(market, now)
    cal = _calendar_for(market, today - timedelta(days=20), today)
    return cal.date_to_session(today - timedelta(days=1), direction="previous").date()


def session_open(market: str, now: datetime | None = None) -> bool:
    """Whether an instant is inside a regular exchange session (close excluded)."""
    market = _market(market)
    instant = _instant(now)
    today = market_day(market, instant)
    cal = _calendar_for(market, today, today)
    if not cal.is_session(today):
        return False
    current = pd.Timestamp(instant).tz_convert("UTC")
    opened = cal.session_open(today)
    closed = cal.session_close(today)
    if not opened <= current < closed:
        return False
    break_start = cal.session_break_start(today)
    break_end = cal.session_break_end(today)
    return bool(pd.isna(break_start) or not break_start <= current < break_end)


def _date(value: Any, field: str = "date") -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite positive decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be a finite positive decimal")
    return result


def _bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no", ""}:
            return False
    raise ValueError(f"{field} must be true or false")


def validate_symbol(market: str, symbol: str) -> str:
    """Normalize a cash-equity/ETF identifier; reject malformed CSV/API inputs."""
    market = _market(market)
    symbol = str(symbol).strip().upper()
    pattern = r"[A-Z0-9]{6}" if market == "KR" else r"[A-Z][A-Z0-9.\-]{0,15}"
    if not re.fullmatch(pattern, symbol):
        raise ValueError(f"invalid {market} stock/ETF symbol: {symbol!r}")
    return symbol


def compute_history(
    rows: Iterable[Mapping[str, Any]],
    market: str,
    listing_date: date | str | None = None,
    complete_from_listing: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute yesterday's adjusted ATH, retaining an honest coverage diagnosis.

    API history should supply a reliable ``listing_date``. CSV data may instead
    explicitly attest ``complete_from_listing``. Either route still requires
    adjusted bars for every exchange session through the previous session. We
    intentionally reject apparently incomplete histories, including missing
    halted-session bars, until a complete supplemental series is supplied.
    Invalid dates/prices and duplicate rows are input errors and raise ValueError.
    """
    market = _market(market)
    today = market_day(market, now)
    cutoff = previous_session(market, now)
    listed = _date(listing_date, "listing_date") if listing_date else None
    attested = _bool(complete_from_listing, "complete_from_listing")
    cleaned: list[dict[str, Any]] = []
    seen: set[date] = set()
    for row in rows:
        day = _date(row.get("date"))
        if day in seen:
            raise ValueError(f"duplicate historical date: {day}")
        seen.add(day)
        if day > today:
            raise ValueError(f"future historical date: {day}")
        high = _positive_decimal(row.get("high"), "high")
        close = _positive_decimal(row.get("close"), "close")
        if close > high:
            raise ValueError(f"close exceeds high on {day}")
        adjusted = _bool(row.get("adjusted", False), "adjusted")
        if day < today:
            cleaned.append({"date": day, "high": high, "close": close, "adjusted": adjusted})
    cleaned.sort(key=lambda row: row["date"])
    high = max((row["high"] for row in cleaned), default=None)
    through = cleaned[-1]["date"] if cleaned else None
    result = {
        "all_time_high": high,
        "history_through": through,
        "history_verified": False,
        "history_reason": "No completed historical bars before the local market day.",
        "listing_date": listed,
        "rows": cleaned,
        "expected_history_date": cutoff,
        "provenance": "listing-date-coverage" if listed else "user-attested" if attested else "unverified",
    }
    if not cleaned:
        return result
    if not all(row["adjusted"] for row in cleaned):
        result["history_reason"] = "Split-adjusted history is required; unadjusted bars block purchases."
        return result
    if through != cutoff:
        result["history_reason"] = f"History is stale: requires the previous session {cutoff}, found {through}."
        return result
    if listed is None and not attested:
        result["history_reason"] = "Listing-date coverage is unknown; pagination alone does not prove an all-time high."
        return result
    earliest = cleaned[0]["date"]
    if listed and listed > cutoff:
        result["history_reason"] = "Listing has no completed historical session yet."
        return result
    cal = _calendar_for(market, min(listed or earliest, earliest), cutoff)
    first = cal.date_to_session(listed, direction="next").date() if listed else earliest
    if earliest != first:
        result["history_reason"] = f"Listing history is incomplete: requires first session {first}, found {earliest}."
        return result
    expected_dates = {session.date() for session in cal.sessions_in_range(first, cutoff)}
    actual_dates = {row["date"] for row in cleaned}
    missing = expected_dates - actual_dates
    unexpected = actual_dates - expected_dates
    if unexpected:
        result["history_reason"] = f"History contains a non-session date: {min(unexpected)}."
        return result
    if missing:
        result["history_reason"] = f"History is missing {len(missing)} exchange session(s), first {min(missing)}."
        return result
    result["history_verified"] = True
    result["history_reason"] = (
        "Complete adjusted session coverage from supplied listing date through the previous session."
        if listed else
        "Complete adjusted history attested by CSV supplier; all sessions since its first bar are present."
    )
    return result


def parse_history_csv(
    text: str,
    market: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Parse grouped historical data without pretending an import covers a market.

    Required columns: market,symbol,name,date,high,close,adjusted,
    complete_from_listing. Optional columns: listing_date,exchange. Instrument
    metadata must stay consistent between rows. A successful import can contain
    incomplete histories: their ``history_verified=False`` blocks entries and
    allows the UI to explain exactly what is missing.
    """
    selected_market = _market(market) if market else None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("CSV is empty")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)):
        raise ValueError("CSV contains duplicate column names")
    if not _REQUIRED_COLUMNS <= set(columns):
        missing = ", ".join(sorted(_REQUIRED_COLUMNS - set(columns)))
        raise ValueError(f"missing CSV columns: {missing}")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    metadata: dict[tuple[str, str], dict[str, Any]] = {}
    for line, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"malformed CSV row at line {line}")
        try:
            row_market = _market(row["market"])
            if selected_market and row_market != selected_market:
                raise ValueError(f"row market {row_market} does not match selected market {selected_market}")
            symbol = validate_symbol(row_market, row["symbol"])
            key = (row_market, symbol)
            adjusted = _bool(row["adjusted"], "adjusted")
            if not adjusted:
                raise ValueError("CSV prices must be split-adjusted (adjusted=true)")
            current_meta = {
                "market": row_market,
                "symbol": symbol,
                "name": row["name"].strip() or symbol,
                "exchange": row.get("exchange", "").strip().upper(),
                "listing_date": _date(row["listing_date"], "listing_date") if row.get("listing_date", "").strip() else None,
                "complete_from_listing": _bool(row["complete_from_listing"], "complete_from_listing"),
            }
            if key in metadata and metadata[key] != current_meta:
                raise ValueError("inconsistent name/exchange/listing-date/completeness metadata for the same symbol")
            metadata[key] = current_meta
            grouped[key].append({
                "date": _date(row["date"]),
                "high": _positive_decimal(row["high"], "high"),
                "close": _positive_decimal(row["close"], "close"),
                "adjusted": adjusted,
            })
        except (TypeError, ValueError) as exc:
            raise ValueError(f"CSV line {line}: {exc}") from exc
    if not grouped:
        raise ValueError("CSV has no history rows")
    result = []
    for key, rows in grouped.items():
        info = metadata[key]
        history = compute_history(
            rows, info["market"], info["listing_date"], info["complete_from_listing"], now,
        )
        # A date typed into a CSV is not independently reliable exchange
        # metadata. Imported data still requires an explicit supplier attestation.
        if not info["complete_from_listing"]:
            history["history_verified"] = False
            history["history_reason"] = "CSV supplier has not attested complete split-adjusted history from listing."
            history["provenance"] = "unverified"
        else:
            history["provenance"] = "user-attested"
        result.append({**info, **history, "currency": "KRW" if info["market"] == "KR" else "USD"})
    return result


import_history_csv = parse_history_csv
