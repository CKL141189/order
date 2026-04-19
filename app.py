from flask import Flask, render_template, request, jsonify, redirect
import re
import sqlite3
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)
DB = os.environ.get("DB_PATH", "orders.db")

FIELDS = [
    "訂單號碼", "預約日期", "預約時間", "服務項目", "航班編號",
    "接送車型", "預約方式", "乘客姓名", "用車人數", "行李件數",
    "聯絡電話", "接送地址", "接送順序", "連繫地址", "加點地址", "付費方式", "備註"
]

FIELD_ALIASES = {
    "用車日期": "預約日期",
    "用車時間": "預約時間",
    "聯絡地址": "連繫地址",
    "出發地址": "接送地址",
}

# B-format orders use space-separated key value (no colon)
B_FORMAT_FIELDS = [
    "用車日期", "用車時間", "服務項目", "航班編號", "接送車型",
    "乘客姓名", "用車人數", "行李件數", "聯絡電話", "聯絡地址",
    "付費方式", "出發地址", "加點地址", "備註",
]

# All field names recognized by K-format parser (canonical + aliases)
_ALL_K_FIELDS = FIELDS + [f for f in FIELD_ALIASES if f not in FIELDS]

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
    pattern = "(" + "|".join(re.escape(f) for f in _ALL_K_FIELDS) + ")："
    parts = re.split(pattern, text)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        canonical = FIELD_ALIASES.get(key, key)
        value = parts[i + 1].strip().replace("訂單訊息", "").strip()
        order[canonical] = value
    return order if order else None

def parse_b_order(order_id, text):
    order = {"訂單號碼": order_id}
    if not text.strip():
        return order
    pattern = "(" + "|".join(re.escape(f) for f in B_FORMAT_FIELDS) + ") "
    parts = re.split(pattern, text)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        canonical = FIELD_ALIASES.get(key, key)
        value = parts[i + 1].strip()
        if value:
            order[canonical] = value
    return order if len(order) > 1 else None

def parse_orders(raw_text):
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in raw_text.strip().splitlines()
             if line.strip() and line.strip() != "訂單訊息"]

    blocks = []  # each entry: [fmt, lines_list]
    for line in lines:
        if "訂單號碼：" in line:
            blocks.append(["K", [line]])
        elif re.match(r'^[BbKk]\d{10,}(\s|$)', line) and "訂單號碼：" not in line:
            blocks.append(["B", [line]])
        elif blocks:
            blocks[-1][1].append(line)

    orders = []
    for fmt, block_lines in blocks:
        if fmt == "K":
            order = parse_order("".join(block_lines))
            if order:
                orders.append(order)
        else:
            text = " ".join(block_lines)
            m = re.match(r'^([BbKk]\d{10,})\s*(.*)', text, re.DOTALL)
            if m:
                order = parse_b_order(m.group(1), m.group(2).strip())
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
    is_pm = "下午" in time_str
    is_am = "上午" in time_str
    m = re.search(r'(\d{1,2})[：:](\d{2})', time_str)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        if is_pm and h < 12:
            h += 12
        if is_am and h == 12:
            h = 0
        time_val = (h, minute)
    else:
        time_val = (0, 0)
    return (date, time_val)

def get_date_tabs():
    today = datetime.now()
    labels = ["昨天", "今天", "明天", "後天"]
    tabs = []
    for i, label in enumerate(labels):
        d = today + timedelta(days=i - 1)
        tabs.append({
            "label": label,
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

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/orders", methods=["GET"])
def orders_page():
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
        "orders.html",
        orders=orders,
        fields=FIELDS,
        date_tabs=date_tabs,
        has_other=has_other,
        drivers=drivers,
    )

@app.route("/clear", methods=["POST"])
def clear_orders():
    with get_db() as conn:
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM drivers")
    return redirect("/orders")

@app.route("/api/parse", methods=["POST"])
def api_parse():
    text = request.get_json(force=True).get("text", "")
    orders = parse_orders(text)
    return jsonify({"orders": orders})

@app.route("/api/add_orders", methods=["POST"])
def api_add_orders():
    new_orders = request.get_json(force=True).get("orders", [])
    existing = load_orders()
    existing_map = {o.get("訂單號碼"): i for i, o in enumerate(existing)}
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    for o in new_orders:
        no = o.get("訂單號碼")
        if no and no in existing_map:
            existing[existing_map[no]].update({k: v for k, v in o.items() if not k.startswith("_")})
        else:
            o.setdefault("created_at", now_str)
            existing.append(o)
    existing.sort(key=order_sort_key)
    save_orders(existing)
    return jsonify({"ok": True})

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

@app.route("/api/orders/delete", methods=["POST"])
def delete_orders():
    ids = request.get_json(force=True).get("ids", [])
    with get_db() as conn:
        for oid in ids:
            conn.execute("DELETE FROM orders WHERE id=?", (int(oid),))
            conn.execute("DELETE FROM drivers WHERE order_id=?", (str(oid),))
    return jsonify({"ok": True})

@app.route("/api/orders/<int:order_id>", methods=["POST"])
def update_order(order_id):
    data = request.get_json()
    with get_db() as conn:
        row = conn.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return jsonify({"ok": False}), 404
        order = json.loads(row["data"])
        order.update({k: v for k, v in data.items() if not k.startswith("_")})
        conn.execute("UPDATE orders SET data=? WHERE id=?",
                     (json.dumps(order, ensure_ascii=False), order_id))
    return jsonify({"ok": True})

@app.route("/stats")
def stats():
    from collections import defaultdict
    today = datetime.now()
    search_phone = request.args.get("phone", "").strip()
    filter_date = request.args.get("date", "").strip()
    try:
        filter_month = int(request.args.get("month", today.month))
    except ValueError:
        filter_month = today.month

    orders = load_orders()
    with get_db() as conn:
        driver_rows = conn.execute("SELECT order_id, name FROM drivers").fetchall()
    drivers = {row["order_id"]: row["name"] for row in driver_rows}

    for order in orders:
        order["_driver"] = drivers.get(order["_id"], "")

    def order_month(o):
        try:
            m, _ = map(int, o.get("預約日期", "").split("/"))
            return m
        except Exception:
            return None

    working = [o for o in orders if o["_driver"] and order_month(o) == filter_month]
    if filter_date:
        working = [o for o in working if o.get("預約日期", "") == filter_date]

    phone_orders = []
    if search_phone:
        phone_orders = [o for o in working if search_phone in o.get("聯絡電話", "")]

    stats_map = defaultdict(lambda: {"total": 0, "cash": 0, "transfer": 0, "other": 0})
    for o in working:
        name = o["_driver"]
        payment = o.get("付費方式", "")
        stats_map[name]["total"] += 1
        if "下車付現" in payment:
            stats_map[name]["cash"] += 1
        elif "匯款預付" in payment or "匯款" in payment:
            stats_map[name]["transfer"] += 1
        else:
            stats_map[name]["other"] += 1

    driver_stats = sorted(stats_map.items(), key=lambda x: -x[1]["total"])

    return render_template(
        "stats.html",
        driver_stats=driver_stats,
        working=working,
        phone_orders=phone_orders,
        search_phone=search_phone,
        filter_date=filter_date,
        filter_month=filter_month,
        current_month=today.month,
        fields=FIELDS,
    )

if __name__ == "__main__":
    app.run(debug=True)
