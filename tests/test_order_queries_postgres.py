"""Run with TEST_DATABASE_URL pointing to a test PostgreSQL database.

All fixtures use connection-local temporary tables, never production tables.
"""
from contextlib import contextmanager
import json
import os
import unittest

import psycopg2

from order_queries import get_order_page, get_order_summary, list_options


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL not set")
class OrderQueryPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.conn.close)
        with self.conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE orders (id SERIAL PRIMARY KEY, data TEXT NOT NULL)")
            cur.execute("CREATE TEMP TABLE drivers (order_id TEXT PRIMARY KEY, name TEXT NOT NULL)")
            for i in range(1, 124):
                order = {"訂單號碼": f"TEST-{i:03}", "預約日期": "10/21" if i <= 120 else "10/27",
                         "預約時間": "下午 21：35抵達", "created_at": "2026/10/20 09:00:00" if i % 2 else "2026/10/21 09:00:00",
                         "聯絡電話": f"09{i:08}", "付費方式": "需開發票" if i % 3 == 0 else "現金",
                         "接送車型": "ALPHARD" if i % 5 == 0 else "五人座",
                         "服務項目": "單程" if i % 7 == 0 else "送機"}
                cur.execute("INSERT INTO orders (data) VALUES (%s)", (json.dumps(order, ensure_ascii=False),))
                if i % 2 == 0:
                    cur.execute("INSERT INTO drivers VALUES (%s, %s)", (str(i), "Test driver"))

    @contextmanager
    def get_db(self):
        yield self.conn

    def page(self, **args):
        return get_order_page(self.get_db, list_options(args, "10/21"))

    def test_pages_are_bounded_stable_and_nonoverlapping(self):
        first, second, last = self.page(), self.page(page="2"), self.page(page="999")
        self.assertEqual(first["total"], 120)
        self.assertEqual(first["pages"], 2)
        self.assertEqual(first["page_size"], 100)
        self.assertEqual(len(first["orders"]), 100)
        self.assertEqual([int(o["_id"]) for o in second["orders"]], list(range(101, 121)))
        self.assertEqual(last["page"], 2)
        self.assertEqual(len(last["orders"]), 20)

    def test_counts_are_scope_wide_and_filters_combine(self):
        result = self.page(invoice="1", luxury="1", driver="assigned")
        self.assertEqual(result["counts"], {"total": 120, "assigned": 60, "unassigned": 60, "invoice": 40, "luxury": 24, "svc": 17})
        self.assertEqual([o["_id"] for o in result["orders"]], ["30", "60", "90", "120"])
        self.assertEqual(result["total"], 4)

    def test_created_date_is_filtered_and_sorted_not_booking_date(self):
        result = self.page(sort="created")
        self.assertEqual(result["total"], 61)
        self.assertEqual(result["orders"][0]["_id"], "122")
        self.assertTrue(all(o["created_at"].startswith("2026/10/21") for o in result["orders"]))

    def test_global_search_and_literal_wildcards(self):
        self.assertEqual(self.page(q="test-123")["total"], 1)
        self.assertEqual(self.page(q="0900000123")["total"], 1)
        self.assertEqual(self.page(q="%")["total"], 0)
        self.assertEqual(self.page(q="' OR TRUE --")["total"], 0)

    def test_summary_includes_dates_outside_current_page(self):
        rows = get_order_summary(self.get_db, "date")
        self.assertEqual(rows, [{"date": 1021, "total": 120, "invoice": 40, "luxury": 24, "svc": 17},
                                {"date": 1027, "total": 3, "invoice": 1, "luxury": 0, "svc": 0}])
        self.assertEqual([r["total"] for r in get_order_summary(self.get_db, "created")], [62, 61])

    def test_summary_plan_parses_each_record_once(self):
        from order_queries import LISTING_CTE
        with self.conn.cursor() as cur:
            cur.execute("EXPLAIN (FORMAT JSON) " + LISTING_CTE + " SELECT booking_day, count(*) FROM listing GROUP BY booking_day")
            plan = json.dumps(cur.fetchone()[0])
        self.assertIn('CTE source', plan)
        self.assertIn('CTE parsed', plan)

    def test_normalized_dates_custom_range_and_empty_day(self):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE orders SET data=%s WHERE id=1", (json.dumps({"預約日期": "05／16 星期六", "預約時間": "上午 03：20"}),))
        self.assertEqual(self.page(tab="5/16")["total"], 1)
        self.assertEqual(self.page(tab="custom", start="10/21", end="10/27")["total"], 122)
        empty = self.page(tab="1/1")
        self.assertEqual((empty["orders"], empty["total"], empty["pages"], empty["page"]), ([], 0, 1, 1))

    def test_edits_driver_changes_and_deletes_are_visible_immediately(self):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE orders SET data=%s WHERE id=1", (json.dumps({"預約日期": "10/27", "備註": "開發票", "預約時間": "上午01:00"}),))
            cur.execute("INSERT INTO drivers VALUES ('1', 'New driver')")
        result = self.page(tab="10/27", invoice="1", driver="assigned")
        self.assertEqual([o["_id"] for o in result["orders"]], ["1"])
        self.assertEqual(result["drivers"], {"1": "New driver"})
        self.assertEqual(get_order_summary(self.get_db, "date")[1]["invoice"], 2)
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id > 100")
        self.assertEqual(self.page(page="3")["page"], 1)
