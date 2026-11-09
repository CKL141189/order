from html.parser import HTMLParser
import unittest

import app as order_app


PAYMENT = "含稅1365下車付現"
NOTE = "接機1100加點200/須開發票"
LEGACY_PAYMENT = PAYMENT + "備　　註    " + NOTE


class OrderCardParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.cards = []
        self.invoice_badges = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if "order-card" in classes:
            self.cards.append(attrs)
        if "chtag" in classes and "inv" in classes:
            self.invoice_badges += 1


def render_orders(orders):
    with order_app.app.test_request_context("/orders"):
        return order_app.render_template(
            "orders.html", orders=orders, fields=order_app.FIELDS,
            date_tabs=[{"label": date, "date_key": date, "display": date}
                       for date in ("10/21", "10/27")],
            has_other=True, drivers={},
        )


class InvoiceOrderTests(unittest.TestCase):
    def test_spaced_note_label_is_separated_from_payment(self):
        for date in ("10/21", "10/27"):
            for label in ("備註：", "備　　註    ", "備 註\t", "備　　註:"):
                with self.subTest(date=date, label=label):
                    text = "\n".join([
                        "訂單號碼：B98803255076", "預約日期：" + date,
                        "預約時間：下午21：35抵達", "服務項目：(接機)桃園機場",
                        "付費方式：" + PAYMENT + label + NOTE,
                    ])
                    orders, failed = order_app.parse_orders(text)
                    self.assertEqual(failed, [])
                    self.assertEqual(len(orders), 1)
                    self.assertEqual(orders[0]["付費方式"], PAYMENT)
                    self.assertEqual(orders[0]["備註"], NOTE)
                    self.assertEqual(orders[0]["預約日期"], date)
                    self.assertEqual(orders[0]["預約時間"], "下午21：35抵達")

    def test_b_format_keeps_spaced_note_support(self):
        orders, failed = order_app.parse_orders("\n".join([
            "B98803255076", "用車日期 10/27", "用車時間 21:35",
            "付費方式 " + LEGACY_PAYMENT,
        ]))
        self.assertEqual(failed, [])
        self.assertEqual(orders[0]["付費方式"], PAYMENT)
        self.assertEqual(orders[0]["備註"], NOTE)

    def test_invoice_badge_includes_legacy_payment_and_normal_notes(self):
        cases = [
            ({"付費方式": LEGACY_PAYMENT}, True),
            ({"付費方式": LEGACY_PAYMENT, "備註": "班機準時"}, True),
            ({"付費方式": PAYMENT, "備註": NOTE}, True),
            ({"付費方式": "費用1365下車付現/統編12345678"}, True),
            ({"備註": "統一編號12345678"}, True),
            ({"備註": None, "付費方式": LEGACY_PAYMENT}, True),
            ({"付費方式": PAYMENT}, False),
            ({"付費方式": "費用1300下車付現", "備註": "班機準時"}, False),
        ]
        for date in ("10/21", "10/27"):
            for data, expected in cases:
                with self.subTest(date=date, data=data):
                    order = {"_id": "1", "_tab": "其他", "訂單號碼": "B98803255076",
                             "預約日期": date, **data}
                    page = OrderCardParser(render_orders([order]))
                    self.assertEqual(len(page.cards), 1)
                    self.assertEqual(page.cards[0]["data-date"], date)
                    self.assertEqual(page.cards[0]["data-has-invoice"], "yes" if expected else "no")
                    self.assertEqual(page.invoice_badges, int(expected))


if __name__ == "__main__":
    unittest.main()
