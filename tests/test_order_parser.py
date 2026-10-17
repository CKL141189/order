import unittest
from unittest.mock import patch

import app as order_app


ORDER_TEXT = """訂單訊息
訂單號碼：K58811289145
預約日期：10/16
預約時間：下午 19：00
服務項目：送機
航班編號：BR071
接送車型：5人座
預約方式：Line
乘客姓名：測試乘客
用車人數： 1人
行李件數： 1大
聯絡電話：0900-000-000
接送地址：台北市內湖區測試路77號
付費方式：費用1000 下車付現
備註： BR071 起飛時間 23:25"""


class OrderParserTests(unittest.TestCase):
    def parse_one(self, text):
        orders, failed = order_app.parse_orders(text)
        self.assertEqual(failed, [])
        self.assertEqual(len(orders), 1)
        return orders[0]

    def test_flight_time_in_notes_does_not_replace_booking_time(self):
        for separator in (" ", "：", ":"):
            with self.subTest(separator=separator):
                note = "BR071 起飛時間" + separator + "23:25"
                order = self.parse_one(ORDER_TEXT.replace("BR071 起飛時間 23:25", note))
                self.assertEqual(order["預約時間"], "下午 19：00")
                self.assertEqual(order["備註"], note)
                self.assertEqual(order["預約日期"], "10/16")
                self.assertEqual(order_app.order_sort_key(order)[1], (19, 0))

    def test_b_format_flight_time_stays_in_notes(self):
        for separator in (" ", "：", ":"):
            with self.subTest(separator=separator):
                note = "BR071 起飛時間" + separator + "23:25"
                order = self.parse_one("\n".join([
                    "B88801135921", "用車日期 10/16", "用車時間 07:00",
                    "用車時間 PM下午", "服務項目 送機", "備註 " + note,
                ]))
                self.assertEqual(order["預約時間"], "下午07:00")
                self.assertEqual(order["備註"], note)
                self.assertEqual(order_app.order_sort_key(order)[1], (19, 0))

    def test_short_field_aliases_still_parse(self):
        for header in ("訂單號碼：K58811289145", "B88801135921", ""):
            with self.subTest(header=header):
                order = self.parse_one("\n".join([
                    header, "日期 10/16", "服務項目 送機", "時間 19:00",
                    "地址 台北市測試路77號",
                ]))
                self.assertEqual(order["預約日期"], "10/16")
                self.assertEqual(order["預約時間"], "19:00")
                self.assertEqual(order["連繫地址"], "台北市測試路77號")

    def test_notes_can_appear_before_other_fields(self):
        order = self.parse_one("\n".join([
            "訂單號碼：K58811289145", "預約時間：下午 19：00",
            "備註：BR071 起飛時間 23:25", "預約日期：10/16",
            "付費方式：費用1000 下車付現",
        ]))
        self.assertEqual(order["預約時間"], "下午 19：00")
        self.assertEqual(order["備註"], "BR071 起飛時間 23:25")
        self.assertEqual(order["預約日期"], "10/16")
        self.assertEqual(order["付費方式"], "費用1000 下車付現")

    def test_inline_fields_and_phone_address_still_parse(self):
        order = self.parse_one(
            "訂單號碼：K48802019948預約日期：5/13預約時間：上午05:45"
            "聯絡電話：0900000000地址：台北市測試路77號"
        )
        self.assertEqual(order["預約時間"], "上午05:45")
        self.assertEqual(order["聯絡電話"], "0900000000")
        self.assertEqual(order["連繫地址"], "台北市測試路77號")

    def test_mislabeled_date_still_parses(self):
        order = self.parse_one("\n".join([
            "訂單編號：K48804193767", "預約時間：6/25",
            "預約時間：下午18:30", "服務車型：5人座",
        ]))
        self.assertEqual(order["預約日期"], "6/25")
        self.assertEqual(order["預約時間"], "下午18:30")

    def test_batch_orders_keep_their_own_times_and_notes(self):
        text = ORDER_TEXT + "\n" + ORDER_TEXT.replace("K58811289145", "K58811289146")
        orders, failed = order_app.parse_orders(text)
        self.assertEqual(failed, [])
        self.assertEqual(len(orders), 2)
        for order in orders:
            self.assertEqual(order["預約時間"], "下午 19：00")
            self.assertEqual(order["備註"], "BR071 起飛時間 23:25")

    def test_parse_api_returns_booking_time_and_complete_notes(self):
        with patch.object(order_app, "_db_initialized", True), patch.object(
            order_app, "get_db", side_effect=AssertionError("Unexpected database access")
        ):
            response = order_app.app.test_client().post("/api/parse", json={"text": ORDER_TEXT})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["orders"][0]["預約時間"], "下午 19：00")
        self.assertEqual(result["orders"][0]["備註"], "BR071 起飛時間 23:25")


if __name__ == "__main__":
    unittest.main()
