import unittest
from unittest.mock import patch

import app as order_app


class OrderListApiTests(unittest.TestCase):
    def setUp(self):
        self.db_init = patch.object(order_app, "_db_initialized", True)
        self.db_init.start()
        self.addCleanup(self.db_init.stop)
        self.client = order_app.app.test_client()

    def test_list_shell_does_not_load_all_orders(self):
        with patch.object(order_app, "load_orders", return_value=[]) as load_all, patch.object(order_app, "get_db"):
            response = self.client.get("/orders")
        self.assertEqual(response.status_code, 200)
        load_all.assert_not_called()
        self.assertIn('id="orderGrid"', response.get_data(as_text=True))
        self.assertIn('id="searchInput"', response.get_data(as_text=True))

    def test_page_api_returns_bounded_cards_and_counts(self):
        page = {"orders": [{"_id": "1", "訂單號碼": "TEST-1", "預約日期": "10/21"}],
                "drivers": {}, "total": 73, "page": 1, "pages": 2, "page_size": 50,
                "counts": {"total": 73, "assigned": 3, "unassigned": 70, "invoice": 8, "luxury": 2, "svc": 1}}
        with patch.object(order_app, "get_order_page", return_value=page, create=True):
            response = self.client.get("/api/orders?tab=10/21")
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["total"], 73)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["counts"]["invoice"], 8)
        self.assertIn("TEST-1", result["html"])
        self.assertNotIn("orders", result)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_summary_has_no_order_details(self):
        dates = [{"date": 1021, "total": 73, "invoice": 8, "luxury": 2, "svc": 1}]
        with patch.object(order_app, "get_order_summary", return_value=dates, create=True):
            response = self.client.get("/api/orders/summary?sort=created")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["dates"], dates)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_invalid_filters_do_not_query_database(self):
        with patch.object(order_app, "get_order_page") as page, patch.object(order_app, "get_order_summary") as summary:
            for url in ("/api/orders?tab=2/31", "/api/orders?page=no", "/api/orders?sort=bad", "/api/orders/summary?sort=bad"):
                self.assertEqual(self.client.get(url).status_code, 400)
            page.assert_not_called()
            summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
