from flask import Flask, render_template, request, jsonify, redirect
import re
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")

FIELDS = [
    "訂單號碼", "預約日期", "預約時間", "服務項目", "航班編號",
    "接送車型", "預約方式", "乘客姓名", "用車人數", "行李件數",
    "聯絡電話", "連繫地址", "接送地址", "加點地址", "接送順序", "出發地址", "抵達地址", "付費方式", "備註"
]

FIELD_ALIASES = {
    "用車日期": "預約日期",
    "用車時間": "預約時間",
    "聯絡地址": "連繫地址",
    "聯繫地址": "連繫地址",
    "接送地點": "接送順序",
    "訂單編號": "訂單號碼",
    "先到地址": "連繫地址",
    "訂單訊息": "訂單號碼",
}

# B-format orders use space-separated key value (no colon)
B_FORMAT_FIELDS = [
    "用車日期", "用車時間", "服務項目", "航班編號", "接送車型",
    "乘客姓名", "用車人數", "行李件數", "聯絡電話", "聯絡地址",
    "付費方式", "出發地址", "抵達地址", "加點地址", "備註",
]

# All field names recognized by K-format parser (canonical + aliases)
_ALL_K_FIELDS = FIELDS + [f for f in FIELD_ALIASES if f not in FIELDS]

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    data TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS drivers (
                    order_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS failed_orders (
                    id SERIAL PRIMARY KEY,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS claude_logs (
                    id SERIAL PRIMARY KEY,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved BOOLEAN DEFAULT FALSE
                )
            """)

_db_initialized = False

@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True

def parse_order(text):
    order = {}
    # Support both ： (fullwidth colon) and 1–3 spaces as field separator
    pattern = "(" + "|".join(re.escape(f) for f in _ALL_K_FIELDS) + ")(?:：|[ \t]{1,3})"
    parts = re.split(pattern, text)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        canonical = FIELD_ALIASES.get(key, key)
        value = parts[i + 1].strip().replace("訂單訊息", "").strip()
        if value:
            order[canonical] = value
    return order if order else None

def parse_b_order(order_id, text):
    order = {"訂單號碼": order_id}
    if not text.strip():
        return order
    # Normalize: fullwidth spaces in field names (e.g. 備　　註 → 備註)
    text = re.sub(r'備[\s\u3000]+註', '備註', text)
    # Ensure every B-format field name is followed by a space (handles missing/tab separators)
    for field in B_FORMAT_FIELDS:
        text = re.sub(re.escape(field) + r'(?=[^\s\t])', field + ' ', text)
        text = text.replace(field + '\t', field + ' ')
    pattern = "(" + "|".join(re.escape(f) for f in B_FORMAT_FIELDS) + ") "
    parts = re.split(pattern, text)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        canonical = FIELD_ALIASES.get(key, key)
        value = parts[i + 1].strip()
        if value:
            # Concatenate duplicate fields (e.g. 用車時間 6:50 + 用車時間 AM上午)
            if canonical in order:
                order[canonical] = order[canonical] + " " + value
            else:
                order[canonical] = value
    return order if len(order) > 1 else None

def parse_orders(raw_text):
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in raw_text.strip().splitlines()
             if line.strip() and line.strip() != "訂單訊息"]

    def _is_k_start(line):
        return bool(re.search(r'訂單(?:號碼|編號|訊息)[ \t：：]', line))

    blocks = []  # each entry: [fmt, lines_list]
    for line in lines:
        if _is_k_start(line):
            blocks.append(["K", [line]])
        elif re.match(r'^[BbKk]\d{10,}', line) and not _is_k_start(line):
            blocks.append(["B", [line]])
        elif blocks:
            blocks[-1][1].append(line)

    def _is_sufficient(order):
        return any(k in order for k in ("預約日期", "預約時間", "服務項目", "乘客姓名"))

    orders = []
    failed = []
    for fmt, block_lines in blocks:
        raw = "\n".join(block_lines)
        if fmt == "K":
            order = parse_order("".join(block_lines))
            if order and _is_sufficient(order):
                orders.append(order)
            else:
                failed.append(raw)
        else:
            text = " ".join(block_lines)
            m = re.match(r'^([BbKk]\d{10,})\s*(.*)', text, re.DOTALL)
            if m:
                order = parse_b_order(m.group(1), m.group(2).strip())
                if order and _is_sufficient(order):
                    orders.append(order)
                else:
                    failed.append(raw)
            else:
                failed.append(raw)
    return orders, failed

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
    m = re.search(r'(\d{1,2})\s*[：:]\s*(\d{2})', time_str)
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
    today = datetime.utcnow() + timedelta(hours=8)
    labels = ["昨天", "今天", "明天", "後天"]
    tabs = []
    for i, label in enumerate(labels):
        d = today + timedelta(days=i - 1)
        display = f"{d.month}/{d.day}"
        tabs.append({
            "label": label,
            "date_key": display,
            "display": display,
        })
    return tabs

def load_orders():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, data FROM orders ORDER BY id")
            rows = cur.fetchall()
    orders = []
    for row in rows:
        o = json.loads(row["data"])
        o["_id"] = str(row["id"])
        orders.append(o)
    return orders

def save_orders(orders):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders")
            for o in orders:
                clean = {k: v for k, v in o.items() if not k.startswith("_")}
                cur.execute("INSERT INTO orders (data) VALUES (%s)", (json.dumps(clean, ensure_ascii=False),))

@app.route("/")
def index():
    return redirect("/orders")

@app.route("/add", methods=["GET"])
def add_order_page():
    return render_template("index.html")

@app.route("/orders", methods=["GET"])
def orders_page():
    orders = load_orders()
    date_tabs = get_date_tabs()
    tab_keys = {t["date_key"] for t in date_tabs}
    for order in orders:
        raw_date = order.get("預約日期", "")
        try:
            m, d = raw_date.split("/")
            norm_date = f"{int(m)}/{int(d)}"
        except Exception:
            norm_date = raw_date
        order["_tab"] = norm_date if norm_date in tab_keys else "其他"
    has_other = any(o["_tab"] == "其他" for o in orders)
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT order_id, name FROM drivers")
            driver_rows = cur.fetchall()
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
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders")
            cur.execute("DELETE FROM drivers")
    return redirect("/orders")

@app.route("/api/parse", methods=["POST"])
def api_parse():
    text = request.get_json(force=True).get("text", "")
    orders, failed = parse_orders(text)
    if failed:
        now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
        with get_db() as conn:
            with conn.cursor() as cur:
                for t in failed:
                    cur.execute("SELECT id FROM failed_orders WHERE text=%s AND resolved=FALSE", (t,))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO failed_orders (text, created_at) VALUES (%s, %s)", (t, now_str))
    return jsonify({"orders": orders, "failed": failed})

@app.route("/failed")
def failed_page():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, text, created_at, resolved FROM failed_orders ORDER BY id DESC")
            failed_rows = cur.fetchall()
            cur.execute("SELECT id, text, created_at, resolved FROM claude_logs ORDER BY id DESC")
            claude_rows = cur.fetchall()
    failed_items = [{**dict(r), "type": "failed"} for r in failed_rows]
    claude_items = [{**dict(r), "type": "claude"} for r in claude_rows]
    items = sorted(failed_items + claude_items, key=lambda x: x["created_at"], reverse=True)
    return render_template("failed.html", items=items)

@app.route("/api/claude/save", methods=["POST"])
def save_claude_log():
    text = request.get_json(force=True).get("text", "").strip()
    if not text:
        return jsonify({"ok": False})
    now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO claude_logs (text, created_at) VALUES (%s, %s)", (text, now_str))
    return jsonify({"ok": True})

@app.route("/claude-logs")
def claude_logs_page():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, text, created_at, resolved FROM claude_logs ORDER BY id DESC")
            rows = cur.fetchall()
    items = [dict(r) for r in rows]
    return render_template("claude_logs.html", items=items)

@app.route("/api/claude/<int:lid>/resolve", methods=["POST"])
def resolve_claude_log(lid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE claude_logs SET resolved=TRUE WHERE id=%s", (lid,))
    return jsonify({"ok": True})

@app.route("/api/claude/<int:lid>/delete", methods=["POST"])
def delete_claude_log(lid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM claude_logs WHERE id=%s", (lid,))
    return jsonify({"ok": True})

@app.route("/api/failed/unresolved", methods=["GET"])
def get_unresolved_failed():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, text, created_at FROM failed_orders WHERE resolved=FALSE ORDER BY id DESC")
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/failed/<int:fid>/resolve", methods=["POST"])
def resolve_failed(fid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE failed_orders SET resolved=TRUE WHERE id=%s", (fid,))
    return jsonify({"ok": True})

@app.route("/api/failed/<int:fid>/delete", methods=["POST"])
def delete_failed(fid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM failed_orders WHERE id=%s", (fid,))
    return jsonify({"ok": True})

@app.route("/api/add_orders", methods=["POST"])
def api_add_orders():
    new_orders = request.get_json(force=True).get("orders", [])
    existing = load_orders()
    now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
    for o in new_orders:
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
        with conn.cursor() as cur:
            if name:
                cur.execute(
                    "INSERT INTO drivers (order_id, name) VALUES (%s, %s) ON CONFLICT (order_id) DO UPDATE SET name = EXCLUDED.name",
                    (order_id, name)
                )
            else:
                cur.execute("DELETE FROM drivers WHERE order_id = %s", (order_id,))
    return jsonify({"ok": True})

@app.route("/api/orders/delete", methods=["POST"])
def delete_orders():
    ids = request.get_json(force=True).get("ids", [])
    with get_db() as conn:
        with conn.cursor() as cur:
            for oid in ids:
                cur.execute("DELETE FROM orders WHERE id=%s", (int(oid),))
                cur.execute("DELETE FROM drivers WHERE order_id=%s", (str(oid),))
    return jsonify({"ok": True})

@app.route("/api/orders/<int:order_id>", methods=["POST"])
def update_order(order_id):
    data = request.get_json()
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT data FROM orders WHERE id=%s", (order_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False}), 404
            order = json.loads(row["data"])
            order.update({k: v for k, v in data.items() if not k.startswith("_")})
            cur.execute("UPDATE orders SET data=%s WHERE id=%s",
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT order_id, name FROM drivers")
            driver_rows = cur.fetchall()
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
