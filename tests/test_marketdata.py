import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from app.marketdata import (
    compute_history,
    market_day,
    parse_history_csv,
    previous_session,
    session_open,
    validate_symbol,
)


UTC = timezone.utc
NOW = datetime(2025, 1, 7, 12, tzinfo=UTC)  # Tuesday, before US open.


def bar(day, high="100", close="95", adjusted=True):
    return {"date": day, "high": high, "close": close, "adjusted": adjusted}


class MarketCalendarTests(unittest.TestCase):
    def test_market_timezone_changes_date(self):
        instant = datetime(2025, 1, 7, 1, tzinfo=UTC)
        self.assertEqual(market_day("KR", instant), date(2025, 1, 7))
        self.assertEqual(market_day("US", instant), date(2025, 1, 6))

    def test_previous_session_excludes_today_after_close(self):
        after_close = datetime(2025, 1, 7, 23, tzinfo=UTC)
        self.assertEqual(previous_session("US", after_close), date(2025, 1, 6))

    def test_weekend_and_us_new_year_holiday(self):
        self.assertEqual(previous_session("US", datetime(2025, 1, 6, 15, tzinfo=UTC)), date(2025, 1, 3))
        self.assertEqual(previous_session("US", datetime(2025, 1, 2, 15, tzinfo=UTC)), date(2024, 12, 31))

    def test_korean_lunar_new_year_holiday(self):
        self.assertEqual(previous_session("KR", datetime(2024, 2, 13, 3, tzinfo=UTC)), date(2024, 2, 8))

    def test_regular_session_only_and_us_dst(self):
        self.assertTrue(session_open("US", datetime(2025, 3, 10, 13, 30, tzinfo=UTC)))
        self.assertFalse(session_open("US", datetime(2025, 3, 10, 13, 29, tzinfo=UTC)))
        self.assertFalse(session_open("US", datetime(2025, 3, 10, 20, tzinfo=UTC)))
        self.assertFalse(session_open("US", datetime(2025, 1, 1, 15, tzinfo=UTC)))

    def test_korean_regular_session(self):
        self.assertTrue(session_open("KR", datetime(2025, 1, 7, 0, tzinfo=UTC)))
        self.assertFalse(session_open("KR", datetime(2025, 1, 7, 6, 30, tzinfo=UTC)))

    def test_naive_now_and_unknown_market_rejected(self):
        with self.assertRaises(ValueError):
            market_day("US", datetime(2025, 1, 7))
        with self.assertRaises(ValueError):
            previous_session("JP", NOW)


class HistoryTests(unittest.TestCase):
    def test_today_is_excluded_and_listing_history_verified(self):
        result = compute_history([
            bar("2025-01-03", "100"),
            bar("2025-01-06", "110"),
            bar("2025-01-07", "900"),
        ], "US", listing_date="2025-01-03", now=NOW)
        self.assertEqual(result["all_time_high"], Decimal("110"))
        self.assertEqual(result["history_through"], date(2025, 1, 6))
        self.assertTrue(result["history_verified"])
        self.assertEqual(len(result["rows"]), 2)

    def test_no_listing_coverage_is_not_all_time_proof(self):
        result = compute_history([bar("2025-01-06")], "US", now=NOW)
        self.assertFalse(result["history_verified"])
        self.assertIn("unknown", result["history_reason"])

    def test_supplier_attestation_is_labeled(self):
        result = compute_history([bar("2025-01-06")], "US", complete_from_listing=True, now=NOW)
        self.assertTrue(result["history_verified"])
        self.assertEqual(result["provenance"], "user-attested")
        self.assertIn("attested", result["history_reason"])

    def test_unadjusted_split_history_blocks_purchases(self):
        result = compute_history([bar("2025-01-06", adjusted=False)], "US", complete_from_listing=True, now=NOW)
        self.assertFalse(result["history_verified"])
        self.assertIn("Split-adjusted", result["history_reason"])

    def test_stale_or_missing_session_history_blocks(self):
        stale = compute_history([bar("2025-01-03")], "US", complete_from_listing=True, now=NOW)
        self.assertFalse(stale["history_verified"])
        self.assertIn("stale", stale["history_reason"])
        missing = compute_history([bar("2025-01-02"), bar("2025-01-06")], "US", listing_date="2025-01-02", now=NOW)
        self.assertFalse(missing["history_verified"])
        self.assertIn("missing 1", missing["history_reason"])

    def test_latest_page_does_not_prove_full_listing_history(self):
        result = compute_history([bar("2025-01-06")], "US", listing_date="2025-01-03", now=NOW)
        self.assertFalse(result["history_verified"])
        self.assertIn("first session", result["history_reason"])

    def test_listing_on_non_session_starts_next_session(self):
        result = compute_history([bar("2025-01-06")], "US", listing_date="2025-01-04", now=NOW)
        self.assertTrue(result["history_verified"])

    def test_duplicate_future_and_invalid_prices_rejected(self):
        cases = [
            [bar("2025-01-06"), bar("2025-01-06")],
            [bar("2025-01-08")],
            [bar("2025-01-06", "NaN")],
            [bar("2025-01-06", "Infinity")],
            [bar("2025-01-06", "0")],
            [bar("2025-01-06", "-1")],
            [bar("2025-01-06", "90", "95")],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                compute_history(rows, "US", complete_from_listing=True, now=NOW)

    def test_non_session_bar_blocks(self):
        result = compute_history([bar("2025-01-05"), bar("2025-01-06")], "US", complete_from_listing=True, now=NOW)
        self.assertFalse(result["history_verified"])
        self.assertIn("non-session", result["history_reason"])


class CsvTests(unittest.TestCase):
    header = "market,symbol,name,date,high,close,adjusted,complete_from_listing,listing_date,exchange\n"

    def test_grouped_csv_preserves_leading_zeroes_and_attestation(self):
        csv = self.header + (
            "KR,005930,삼성전자,2025-01-06,60000,59000,true,true,,KRX\n"
            "US,AAPL,Apple,2025-01-06,245,240,true,true,,NAS\n"
        )
        results = parse_history_csv(csv, now=NOW)
        self.assertEqual([item["symbol"] for item in results], ["005930", "AAPL"])
        self.assertTrue(all(item["history_verified"] for item in results))
        self.assertEqual(results[0]["currency"], "KRW")
        self.assertEqual(results[1]["currency"], "USD")

    def test_unadjusted_csv_rejected(self):
        csv = self.header + "US,AAPL,Apple,2025-01-06,245,240,false,true,,NAS\n"
        with self.assertRaisesRegex(ValueError, "split-adjusted"):
            parse_history_csv(csv, now=NOW)

    def test_csv_listing_date_alone_does_not_replace_attestation(self):
        csv = self.header + "US,AAPL,Apple,2025-01-06,245,240,true,false,2025-01-06,NAS\n"
        result = parse_history_csv(csv, now=NOW)[0]
        self.assertFalse(result["history_verified"])
        self.assertEqual(result["provenance"], "unverified")
        self.assertIn("not attested", result["history_reason"])

    def test_missing_columns_and_mismatched_market_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing CSV"):
            parse_history_csv("symbol,date\nAAPL,2025-01-06\n", now=NOW)
        csv = self.header + "US,AAPL,Apple,2025-01-06,245,240,true,true,,NAS\n"
        with self.assertRaisesRegex(ValueError, "selected market"):
            parse_history_csv(csv, market="KR", now=NOW)

    def test_inconsistent_metadata_and_duplicate_rows_rejected(self):
        row = "US,AAPL,Apple,2025-01-06,245,240,true,true,,NAS\n"
        with self.assertRaisesRegex(ValueError, "duplicate historical"):
            parse_history_csv(self.header + row + row, now=NOW)
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            parse_history_csv(self.header + row + "US,AAPL,Other,2025-01-03,245,240,true,true,,NAS\n", now=NOW)

    def test_symbols_and_malformed_rows_rejected(self):
        self.assertEqual(validate_symbol("US", "brk.b"), "BRK.B")
        self.assertEqual(validate_symbol("KR", "005930"), "005930")
        self.assertEqual(validate_symbol("KR", "1234a0"), "1234A0")
        for market, symbol in [("KR", "5930"), ("US", "../x"), ("US", "<script>")]:
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                validate_symbol(market, symbol)
        with self.assertRaisesRegex(ValueError, "malformed"):
            parse_history_csv(self.header + "US,AAPL,Apple\n", now=NOW)


if __name__ == "__main__":
    unittest.main()
