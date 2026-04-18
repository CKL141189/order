from flask import Flask, render_template, request, jsonify
import re
import sqlite3
import json
from datetime import datetime, timedelta

app = Flask(__name__)
DB = "orders.db"

FIELDS = [
    "訂單號碼", "預約日期", "預約時間", "服務項目", "航班編號",
    "接送車型", "預約方式", "乘客姓名", "用車人數", "行李件數",
    "聯絡電話", "接送地址", "付費方式", "備註"
]

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drivers (
                order_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)

init_db()

def parse_order(text):
    order = {}
    pattern = "(" + "|".join(re.escape(f) for f in FIELDS) + ")："
    parts = re.split(pattern, text)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        value = parts[i + 1].strip()
        order[key] = value
    return order if order else None

def parse_orders(raw_text):
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in raw_text.strip().splitlines() if line.strip()]
    combined = "".join(lines)
    chunks = re.split(r'(?=訂單號碼：)', combined)
    orders = []
    for chunk in chunks:
        if chunk.strip():
            order = parse_order(chunk)
            if order:
                orders.append(order)
    return orders

def order_sort_key(order):
    date_str = order.get("預約日期", "")
    time_str = order.get("預約時間", "")
    try:
        month, day = map(int, date_str.split("/"))
        date = datetime(datetime.now().year, month, day)
    except Exception:
        date = datetime.max
    time_str = time_str.replace("上午", "").replace("下午", "").replace("：", ":").strip()
    try:
        t = datetime.strptime(time_str, "%H:%M")
        time_val = (t.hour, t.minute)
    except Exception:
        time_val = (0, 0)
    return (date, time_val)

def get_date_tabs():
    today = datetime.now()
    labels = ["今天", "明天", "後天", "第4天", "第5天", "第6天", "第7天"]
    tabs = []
    for i in range(7):
        d = today + timedelta(days=i)
        tabs.append({
            "label": labels[i],
            "date_key": f"{d.month}/{d.day}",
            "display": f"{d.month}/{d.day}",
        })
    return tabs

def load_orders():
    with get_db() as conn:
        rows = conn.execute("SELECT id, data FROM orders ORDER BY id").fetchall()
    orders = []
    for row in rows:
        o = json.loads(row["data"])
        o["_id"] = str(row["id"])
        orders.append(o)
    return orders

def save_orders(orders):
    with get_db() as conn:
        conn.execute("DELETE FROM orders")
        for o in orders:
            clean = {k: v for k, v in o.items() if not k.startswith("_")}
            conn.execute("INSERT INTO orders (data) VALUES (?)", (json.dumps(clean, ensure_ascii=False),))

@app.route("/", methods=["GET", "POST"])
def index():
    error = ""
    raw_text = ""

    if request.method == "POST":
        action = request.form.get("action", "sort")
        if action == "sort":
            raw_text = request.form.get("orders_text", "")
            new_orders = parse_orders(raw_text)
            if raw_text.strip() and not new_orders:
                error = "無法解析訂單，請確認格式是否正確。"
            else:
                existing = load_orders()
                existing_nos = {o.get("訂單號碼") for o in existing}
                for o in new_orders:
                    if o.get("訂單號碼") not in existing_nos:
                        existing.append(o)
                existing.sort(key=order_sort_key)
                save_orders(existing)
        elif action == "clear":
            with get_db() as conn:
                conn.execute("DELETE FROM orders")
                conn.execute("DELETE FROM drivers")

    orders = load_orders()
    date_tabs = get_date_tabs()
    tab_keys = {t["date_key"] for t in date_tabs}
    for order in orders:
        order["_tab"] = order.get("預約日期", "") if order.get("預約日期", "") in tab_keys else "其他"

    has_other = any(o["_tab"] == "其他" for o in orders)

    with get_db() as conn:
        driver_rows = conn.execute("SELECT order_id, name FROM drivers").fetchall()
    drivers = {row["order_id"]: row["name"] for row in driver_rows}

    return render_template(
        "index.html",
        orders=orders,
        raw_text=raw_text,
        error=error,
        fields=FIELDS,
        date_tabs=date_tabs,
        has_other=has_other,
        drivers=drivers,
    )

@app.route("/api/drivers", methods=["POST"])
def set_driver():
    data = request.get_json()
    order_id = str(data.get("order_id", ""))
    name = data.get("name", "").strip()
    with get_db() as conn:
        if name:
            conn.execute("INSERT OR REPLACE INTO drivers (order_id, name) VALUES (?, ?)", (order_id, name))
        else:
            conn.execute("DELETE FROM drivers WHERE order_id = ?", (order_id,))
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(debug=True)
