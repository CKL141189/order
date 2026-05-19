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
    "聯絡電話", "連繫地址", "接送地址", "先到地址", "加點地址", "接送順序", "出發地址", "抵達地址", "付費方式", "備註"
]

FIELD_ALIASES = {
    "日期": "預約日期",
    "預約日": "預約日期",
    "用車日": "預約日期",
    "用車日期": "預約日期",
    "搭車日期": "預約日期",
    "乘車日期": "預約日期",
    "接送日期": "預約日期",
    "時間": "預約時間",
    "預約時間": "預約時間",
    "用車時間": "預約時間",
    "地址": "連繫地址",
    "聯絡地址": "連繫地址",
    "聯繫地址": "連繫地址",
    "接送地點": "接送順序",
    "訂單編號": "訂單號碼",
    "目的地址": "抵達地址",
    "目的地": "抵達地址",
    "到達地址": "抵達地址",
    "下車地址": "抵達地址",
    "下車地點": "抵達地址",
    "送達地址": "抵達地址",
    "終點地址": "抵達地址",
    "訂單訊息": "訂單號碼",
}

# B-format orders use space-separated key value (no colon)
B_FORMAT_FIELDS = [
    "日期", "預約日", "用車日", "用車日期", "搭車日期", "乘車日期", "接送日期", "時間", "預約時間", "用車時間", "服務項目", "航班編號", "接送車型",
    "乘客姓名", "用車人數", "行李件數", "聯絡電話", "聯絡地址",
    "地址", "付費方式", "出發地址", "先到地址", "目的地址", "目的地", "到達地址", "下車地址", "下車地點", "送達地址", "終點地址", "抵達地址", "加點地址", "備註",
]

# All field names recognized by K-format parser (canonical + aliases)
_ALL_K_FIELDS = FIELDS + [f for f in FIELD_ALIASES if f not in FIELDS]

def normalize_date_value(value):
    if not value:
        return ""
    m = re.search(r'(\d{1,2})\s*[／/]\s*(\d{1,2})', str(value))
    if not m:
        return str(value).strip()
    return f"{int(m.group(1))}/{int(m.group(2))}"

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deleted_orders (
                    id SERIAL PRIMARY KEY,
                    data TEXT NOT NULL,
                    driver_name TEXT,
                    deleted_at TEXT NOT NULL
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
    pattern = "(" + "|".join(re.escape(f) for f in _ALL_K_FIELDS) + ")(?:[：:]|[ \t]{1,3})"
    parts = re.split(pattern, text)
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        canonical = FIELD_ALIASES.get(key, key)
        value = parts[i + 1].strip().replace("訂單訊息", "").strip()
        if canonical == "預約日期":
            value = normalize_date_value(value)
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
        if canonical == "預約日期":
            value = normalize_date_value(value)
        if value:
            # Concatenate duplicate fields (e.g. 用車時間 6:50 + 用車時間 AM上午)
            if canonical in order:
                order[canonical] = order[canonical] + " " + value
            else:
                order[canonical] = value
    return order if len(order) > 1 else None

def parse_orders(raw_text):
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    def _clean_line(line):
        return line.strip().strip("\ufeff\u200b\u200c\u200d")

    def _is_order_message_header(line):
        return re.sub(r'[\s\u3000：:]+', '', line) == "訂單訊息"

    lines = []
    for raw_line in raw_text.strip().splitlines():
        line = _clean_line(raw_line)
        if line and not _is_order_message_header(line):
            lines.append(line)

    def _is_k_start(line):
        return bool(re.search(r'訂單(?:號碼|編號|訊息)[ \t：:]', line))

    def _find_loose_order_id(line):
        return re.search(r'([BbKk]\d{10,}(?:-\d+)?)', line)

    blocks = []  # each entry: [fmt, lines_list]
    for line in lines:
        if _is_k_start(line):
            blocks.append(["K", [line]])
        else:
            id_match = _find_loose_order_id(line)
            if id_match:
                blocks.append(["B", [line.strip()]])
            elif blocks:
                blocks[-1][1].append(line)

    def _is_sufficient(order):
        return any(k in order for k in ("預約日期", "預約時間", "服務項目", "乘客姓名"))

    orders = []
    failed = []
    if not blocks:
        raw = "\n".join(lines)
        order = parse_order("".join(lines))
        if order and _is_sufficient(order):
            return [order], []
        return [], ([raw] if raw else [])

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
            m = _find_loose_order_id(text)
            if m:
                order_id = text[:m.end(1)].strip()
                body_text = text[m.end(1):].strip()
                # If body lines use K-format colon separators, parse as K
                first_body = body_text or (block_lines[1].strip() if len(block_lines) > 1 else "")
                _kf = re.compile("(" + "|".join(re.escape(f) for f in _ALL_K_FIELDS) + ")[：:]")
                if first_body and _kf.match(first_body):
                    k_text = "訂單號碼：" + order_id + body_text
                    order = parse_order(k_text)
                else:
                    order = parse_b_order(order_id, body_text)
                if order and _is_sufficient(order):
                    orders.append(order)
                else:
                    failed.append(raw)
            else:
                failed.append(raw)
    return orders, failed

def order_sort_key(order):
    date_str = normalize_date_value(order.get("預約日期", ""))
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
    orders.sort(key=order_sort_key)
    return orders

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
        norm_date = normalize_date_value(order.get("預約日期", ""))
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

@app.route("/api/failed/<int:fid>/add", methods=["POST"])
def add_failed_order(fid):
    now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT text FROM failed_orders WHERE id=%s", (fid,))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "not_found"})
            orders, failed = parse_orders(row["text"])
            if not orders:
                return jsonify({"ok": False, "error": "parse_failed", "failed": failed})
            for o in orders:
                o.setdefault("created_at", now_str)
                clean = {k: v for k, v in o.items() if not k.startswith("_")}
                order_no = clean.get("訂單號碼", "")
                updated = False
                if order_no:
                    cur.execute("SELECT id, data FROM orders")
                    for existing in cur.fetchall():
                        existing_data = json.loads(existing["data"])
                        if existing_data.get("訂單號碼") == order_no:
                            existing_data.update(clean)
                            cur.execute(
                                "UPDATE orders SET data=%s WHERE id=%s",
                                (json.dumps(existing_data, ensure_ascii=False), existing["id"])
                            )
                            updated = True
                            break
                if not updated:
                    cur.execute("INSERT INTO orders (data) VALUES (%s)", (json.dumps(clean, ensure_ascii=False),))
            cur.execute("UPDATE failed_orders SET resolved=TRUE WHERE id=%s", (fid,))
    return jsonify({"ok": True, "count": len(orders), "failed": failed})

@app.route("/api/failed/<int:fid>/delete", methods=["POST"])
def delete_failed(fid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM failed_orders WHERE id=%s", (fid,))
    return jsonify({"ok": True})

@app.route("/api/add_orders", methods=["POST"])
def api_add_orders():
    new_orders = request.get_json(force=True).get("orders", [])
    now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
    with get_db() as conn:
        with conn.cursor() as cur:
            for o in new_orders:
                o.setdefault("created_at", now_str)
                clean = {k: v for k, v in o.items() if not k.startswith("_")}
                cur.execute("INSERT INTO orders (data) VALUES (%s)", (json.dumps(clean, ensure_ascii=False),))
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
    return jsonify({"ok": True, "order_id": order_id, "name": name})

@app.route("/api/orders/delete", methods=["POST"])
def delete_orders():
    ids = request.get_json(force=True).get("ids", [])
    now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for oid in ids:
                cur.execute("SELECT data FROM orders WHERE id=%s", (int(oid),))
                row = cur.fetchone()
                if not row:
                    continue
                cur.execute("SELECT name FROM drivers WHERE order_id=%s", (str(oid),))
                driver_row = cur.fetchone()
                driver_name = driver_row["name"] if driver_row else None
                cur.execute(
                    "INSERT INTO deleted_orders (data, driver_name, deleted_at) VALUES (%s, %s, %s)",
                    (row["data"], driver_name, now_str)
                )
                cur.execute("DELETE FROM orders WHERE id=%s", (int(oid),))
                cur.execute("DELETE FROM drivers WHERE order_id=%s", (str(oid),))
    return jsonify({"ok": True})

@app.route("/deleted")
def deleted_page():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, data, driver_name, deleted_at FROM deleted_orders ORDER BY id DESC")
            rows = cur.fetchall()
    items = []
    for r in rows:
        d = json.loads(r["data"])
        d["_id"] = str(r["id"])
        d["_driver"] = r["driver_name"] or ""
        d["_deleted_at"] = r["deleted_at"]
        items.append(d)
    return render_template("deleted.html", items=items, fields=FIELDS)

@app.route("/api/orders/restore", methods=["POST"])
def restore_orders():
    ids = request.get_json(force=True).get("ids", [])
    now_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y/%m/%d %H:%M")
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for did in ids:
                cur.execute("SELECT data, driver_name FROM deleted_orders WHERE id=%s", (int(did),))
                row = cur.fetchone()
                if not row:
                    continue
                data = json.loads(row["data"])
                data.setdefault("created_at", now_str)
                cur.execute("INSERT INTO orders (data) VALUES (%s) RETURNING id", (json.dumps(data, ensure_ascii=False),))
                new_id = cur.fetchone()["id"]
                if row["driver_name"]:
                    cur.execute(
                        "INSERT INTO drivers (order_id, name) VALUES (%s, %s) ON CONFLICT (order_id) DO UPDATE SET name=EXCLUDED.name",
                        (str(new_id), row["driver_name"])
                    )
                cur.execute("DELETE FROM deleted_orders WHERE id=%s", (int(did),))
    return jsonify({"ok": True})

@app.route("/api/orders/purge", methods=["POST"])
def purge_orders():
    ids = request.get_json(force=True).get("ids", [])
    with get_db() as conn:
        with conn.cursor() as cur:
            for did in ids:
                cur.execute("DELETE FROM deleted_orders WHERE id=%s", (int(did),))
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
    return jsonify({"ok": True, "order": order})

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
            m, _ = map(int, normalize_date_value(o.get("預約日期", "")).split("/"))
            return m
        except Exception:
            return None

    working = [o for o in orders if o["_driver"] and order_month(o) == filter_month]
    if filter_date:
        working = [o for o in working if normalize_date_value(o.get("預約日期", "")) == filter_date]

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
