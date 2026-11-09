import unittest

from order_queries import date_number, list_options, scope_clause


class OrderQueryOptionsTests(unittest.TestCase):
    def test_date_normalization(self):
        for value in ("5/16", "05/16", " 5／16 "):
            self.assertEqual(date_number(value), 516)

    def test_invalid_options_are_rejected(self):
        for args in ({"tab": "2/31"}, {"tab": "bad"}, {"page": "bad"},
                     {"sort": "DROP TABLE"}, {"driver": "unknown"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                list_options(args, "11/9")

    def test_created_date_scope_uses_created_day(self):
        options = list_options({"sort": "created", "tab": "05/13"}, "11/9")
        self.assertEqual(scope_clause(options), ("created_day = %s", [513]))

    def test_global_search_is_parameterized_and_ignores_date(self):
        options = list_options({"q": "x%' OR TRUE --", "tab": "10/21"}, "11/9")
        sql, params = scope_clause(options)
        self.assertNotIn("booking_day", sql)
        self.assertNotIn(options["q"], sql)
        self.assertEqual(params, [options["q"]])

    def test_cross_year_range_and_page_floor(self):
        options = list_options({"tab": "custom", "start": "12/31", "end": "1/2", "page": "-5"}, "11/9")
        self.assertEqual(scope_clause(options), ("(booking_day >= %s OR booking_day <= %s)", [1231, 102]))
        self.assertEqual(options["page"], 1)
