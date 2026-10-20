"""Broker contract tests with HTTP fixtures; these never contact Kiwoom."""

import json
from datetime import date
import unittest

import httpx

from app.kiwoom import KiwoomClient, KiwoomError, OrderOutcomeUnknown


KR = {"market": "KR", "symbol": "005930", "exchange": "KRX", "listing_date": "2026-10-16"}
US = {"market": "US", "symbol": "AAPL", "exchange": "ND"}


def success(**data):
    return httpx.Response(200, json={"return_code": 0, **data})


class KiwoomContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.handler = None

        async def dispatch(request):
            self.requests.append(request)
            if request.url.path == "/oauth2/token":
                return success(token="test-token-do-not-expose", expires_dt="20990101000000", token_type="Bearer")
            return self.handler(request)

        self.client = KiwoomClient("mock", "test-key", "test-secret", transport=httpx.MockTransport(dispatch), min_interval=0)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_auth_reuses_token_without_returning_it(self):
        first = await self.client.authenticate()
        second = await self.client.authenticate()
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("test-token", str(first) + str(second) + str(self.client.capabilities))
        self.assertEqual(json.loads(self.requests[0].content), {
            "grant_type": "client_credentials", "appkey": "test-key", "secretkey": "test-secret",
        })

    async def test_domestic_universe_paginates_and_identifies_etfs(self):
        def handler(request):
            body = json.loads(request.content)
            if body["mrkt_tp"] == "0" and request.headers.get("next-key") != "page2":
                return httpx.Response(200, headers={"cont-yn": "Y", "next-key": "page2"}, json={
                    "return_code": 0, "list": [{"code": "005930", "name": "삼성전자", "regDay": "19750611"}],
                })
            if body["mrkt_tp"] == "0":
                return success(list=[{"code": "000660", "name": "SK하이닉스", "regDay": "19961226"}])
            if body["mrkt_tp"] == "8":
                return success(list=[{"code": "069500", "name": "KODEX 200", "regDay": "20021014"}])
            return success(list=[])
        self.handler = handler
        rows = await self.client.universe(["KR"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["kind"], "ETF")
        self.assertEqual(rows[1]["listing_date"], "1975-06-11")
        page2 = [r for r in self.requests if r.headers.get("next-key") == "page2"]
        self.assertEqual(len(page2), 1)
        self.assertEqual(page2[0].headers["cont-yn"], "Y")

    async def test_us_universe_excludes_etns_and_has_no_invented_listing_date(self):
        def handler(request):
            if request.headers["api-id"] == "usa10104":
                return success(list=[{"code": "SPY", "etn": "N"}, {"code": "ETNN", "etn": "Y"}])
            return success(list=[
                {"stk_cd": "AAPL", "stex_tp": "ND", "isEtf": "N"},
                {"stk_cd": "SPY", "stex_tp": "NA", "isEtf": "Y"},
                {"stk_cd": "ETNN", "stex_tp": "NA", "isEtf": "Y"},
            ])
        self.handler = handler
        rows = await self.client.universe(["US"])
        self.assertEqual([row["symbol"] for row in rows], ["AAPL", "SPY"])
        self.assertIsNone(rows[0]["listing_date"])
        self.assertEqual(rows[1]["kind"], "ETF")

    async def test_latest_adjusted_history_excludes_today_and_does_not_use_52week_high(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["base_dt"], "20261020")
            self.assertEqual(body["upd_stkpc_tp"], "1")
            if not request.headers.get("next-key"):
                return httpx.Response(200, headers={"cont-yn": "Y", "next-key": "older"}, json={
                    "return_code": 0, "stk_dt_pole_chart_qry": [
                        {"dt": "20261020", "high_pric": "99999", "cur_prc": "500"},
                        {"dt": "20261019", "high_pric": "10000", "cur_prc": "9900"},
                    ],
                })
            return success(stk_dt_pole_chart_qry=[{"dt": "20261016", "high_pric": "11000", "cur_prc": "10000"}])
        self.handler = handler
        result = await self.client.history(KR, date(2026, 10, 20))
        self.assertEqual(result["high"], "11000")
        self.assertTrue(result["history_complete"])
        self.assertTrue(all(row["adjusted"] for row in result["bars"]))
        self.assertEqual(result["adjustment_as_of"], "2026-10-20")

    async def test_missing_us_listing_date_blocks_completeness(self):
        self.handler = lambda request: success(result_list=[{"dt": "20000101", "high_pric": "5", "cur_prc": "4"}])
        result = await self.client.history(US, date(2026, 10, 20))
        self.assertFalse(result["history_complete"])
        self.assertEqual(result["high"], "5")

    async def test_repeating_continuation_does_not_silently_truncate(self):
        self.handler = lambda request: httpx.Response(200, headers={"cont-yn": "Y", "next-key": "same"}, json={
            "return_code": 0, "stk_dt_pole_chart_qry": [{"dt": "20261019", "high_pric": "100", "cur_prc": "99"}],
        })
        with self.assertRaises(KiwoomError):
            await self.client.history(KR, date(2026, 10, 20))

    async def test_domestic_tick_quote_preserves_broker_trade_timestamp(self):
        self.handler = lambda request: success(stk_tic_chart_qry=[{"cur_prc": "-10200", "cntr_tm": "20261020101010"}])
        result = await self.client.quote(KR)
        self.assertEqual(result["price"], "10200")
        self.assertEqual(result["market_timestamp"], "2026-10-20T10:10:10+09:00")
        self.assertTrue(result["timestamp_verified"])

    async def test_us_quote_requires_explicit_timestamp_timezone(self):
        self.handler = lambda request: success(result_list=[{"cur_prc": "+210.1100", "cntr_tm": "20261020101010"}])
        result = await self.client.quote(US)
        self.assertFalse(result["timestamp_verified"])
        self.assertIsNone(result["market_timestamp"])
        self.client._us_timestamp_tz = "America/New_York"
        result = await self.client.quote(US)
        self.assertEqual(result["market_timestamp"], "2026-10-20T10:10:10-04:00")

    async def test_orderable_cash_uses_us_dollars_without_krw_or_margin_power(self):
        self.handler = lambda request: success(
            ord_alowa="1000.25", min_ord_alowa="900.00", min_ord_alowq="000000000004",
            krw_ord_alowa_100="9000000", crnc_code="USD",
        )
        result = await self.client.orderable_cash(US, "200")
        self.assertEqual(result["available_cash"], "900.00")
        self.assertEqual(result["max_quantity"], 4)
        self.assertTrue(result["includes_broker_reservations"])
        self.assertEqual(self.requests[-1].headers["api-id"], "ust31490")

    async def test_missing_cash_field_is_not_silently_zero(self):
        self.handler = lambda request: success(min_ord_alowa="1000", min_ord_alowq="1", crnc_code="USD")
        with self.assertRaises(KiwoomError):
            await self.client.orderable_cash(US, "200")

    async def test_accepted_order_is_not_a_fill_and_market_order_codes_differ(self):
        self.handler = lambda request: success(ord_no="0000099")
        domestic = await self.client.submit_order(KR, "buy", 3)
        self.assertEqual(domestic["status"], "accepted")
        self.assertEqual(domestic["filled_quantity"], 0)
        self.assertEqual(json.loads(self.requests[-1].content)["trde_tp"], "3")
        overseas = await self.client.submit_order(US, "sell", 2)
        self.assertEqual(overseas["side"], "sell")
        self.assertEqual(self.requests[-1].headers["api-id"], "ust20001")
        self.assertEqual(json.loads(self.requests[-1].content)["trde_tp"], "03")

    async def test_order_timeout_is_unknown_and_not_retried(self):
        def handler(request):
            raise httpx.ReadTimeout("request possibly accepted", request=request)
        self.handler = handler
        with self.assertRaises(OrderOutcomeUnknown):
            await self.client.submit_order(KR, "buy", 3, "10200")
        self.assertEqual(len([r for r in self.requests if r.url.path.endswith("/ordr")]), 1)

    async def test_missing_order_id_is_unknown_and_rejection_is_definite(self):
        self.handler = lambda request: success()
        with self.assertRaises(OrderOutcomeUnknown):
            await self.client.submit_order(KR, "buy", 3)
        self.handler = lambda request: httpx.Response(200, json={"return_code": -300, "return_msg": "test-secret"})
        try:
            await self.client.submit_order(KR, "buy", 3)
        except KiwoomError as error:
            self.assertNotIsInstance(error, OrderOutcomeUnknown)
            self.assertNotIn("test-secret", str(error))
        else:
            self.fail("rejected order was accepted")

    async def test_holdings_use_actual_average_and_sellable_not_settlement_quantity(self):
        self.handler = lambda request: success(result_list=[{
            "stk_cd": "AAPL", "crnc_code": "USD", "poss_qty": "4", "qty": "0", "sell_alowq": "3",
            "frgn_stk_book_uv": "201.3147", "frgn_stk_nm": "애플",
        }])
        rows = await self.client.holdings("US")
        self.assertEqual(rows[0]["quantity"], 4)
        self.assertEqual(rows[0]["sellable_quantity"], 3)
        self.assertEqual(rows[0]["average_price"], "201.3147")

    async def test_order_status_reads_cumulative_partial_and_full_fills(self):
        state = {"ord_no": "000000099", "stk_cd": "AAPL", "ord_qty": "4", "cntr_qty": "1",
                 "cntr_uv": "200.50", "ord_remnq": "3", "slby_tp": "2", "ord_stat": "부분체결"}
        self.handler = lambda request: success(result_list=[state])
        result = await self.client.order_status(US, "99")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["filled_quantity"], 1)
        self.assertEqual(result["average_fill_price"], "200.50")
        state.update(cntr_qty="4", ord_remnq="0", ord_stat="체결완료")
        result = await self.client.order_status(US, "99")
        self.assertEqual(result["status"], "filled")

    async def test_duplicate_cash_lots_are_aggregated_without_losing_quantity(self):
        self.handler = lambda request: success(acnt_evlt_remn_indv_tot=[
            {"stk_cd": "A005930", "rmnd_qty": "3", "trde_able_qty": "2", "pur_pric": "10000", "crd_tp": "00"},
            {"stk_cd": "A005930", "rmnd_qty": "1", "trde_able_qty": "1", "pur_pric": "10800", "crd_tp": "00"},
            {"stk_cd": "A005930", "rmnd_qty": "5", "trde_able_qty": "5", "pur_pric": "9000", "crd_tp": "03"},
        ])
        rows = await self.client.holdings("KR")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 4)
        self.assertEqual(rows[0]["sellable_quantity"], 3)
        self.assertEqual(rows[0]["average_price"], "10200")

    async def test_cancel_is_request_not_confirmed_cancellation(self):
        self.handler = lambda request: success(ord_no="00000100")
        result = await self.client.cancel_order(US, "99")
        self.assertEqual(result["status"], "cancel_requested")
        self.assertEqual(self.requests[-1].headers["api-id"], "ust20003")
        self.assertEqual(json.loads(self.requests[-1].content), {"orig_ord_no": "99", "stex_tp": "ND", "stk_cd": "AAPL"})

    async def test_order_missing_from_broker_is_an_error_not_a_zero_fill(self):
        self.handler = lambda request: success(result_list=[])
        with self.assertRaises(KiwoomError):
            await self.client.order_status(US, "99")

    async def test_schema_failure_is_not_reported_as_empty_holdings(self):
        self.handler = lambda request: success(result_lsit=[])
        with self.assertRaises(KiwoomError):
            await self.client.holdings("US")


if __name__ == "__main__":
    unittest.main()
