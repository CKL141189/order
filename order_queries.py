"""Bounded order reads and date summaries over the existing JSON records."""

from datetime import datetime
import re

import psycopg2.extras


PAGE_SIZE = 50

# Materialize parsing once: inlining repeats JSON casts and regexes per aggregate.
LISTING_CTE = r"""
WITH source AS MATERIALIZED (
    SELECT id, data::jsonb AS doc FROM orders
), parsed AS MATERIALIZED (
    SELECT id, doc,
        regexp_match(COALESCE(doc->>'預約日期', ''),
            '([0-9]{1,2})[[:space:]]*[/／][[:space:]]*([0-9]{1,2})') AS booking,
        regexp_match(COALESCE(doc->>'created_at', ''),
            '^[0-9]{4}/([0-9]{1,2})/([0-9]{1,2})') AS created,
        regexp_match(COALESCE(doc->>'預約時間', ''),
            '([0-9]{1,2})[[:space:]]*[:：][[:space:]]*([0-9]{2})') AS clock
    FROM source
), listing AS (
    SELECT p.id, p.doc, COALESCE(d.name, '') AS driver_name,
        CASE WHEN booking[1]::int BETWEEN 1 AND 12 AND booking[2]::int BETWEEN 1 AND 31
             THEN booking[1]::int * 100 + booking[2]::int END AS booking_day,
        CASE WHEN created[1]::int BETWEEN 1 AND 12 AND created[2]::int BETWEEN 1 AND 31
             THEN created[1]::int * 100 + created[2]::int END AS created_day,
        COALESCE(doc->>'created_at', '') AS created_at,
        COALESCE((CASE
            WHEN strpos(COALESCE(doc->>'預約時間', ''), '下午') > 0 AND clock[1]::int < 12
                THEN clock[1]::int + 12
            WHEN strpos(COALESCE(doc->>'預約時間', ''), '上午') > 0 AND clock[1]::int = 12
                THEN 0
            ELSE clock[1]::int END) * 60 + clock[2]::int, 0) AS time_minutes,
        COALESCE(d.name, '') <> '' AS assigned,
        (COALESCE(doc->>'備註', '') || ' ' || COALESCE(doc->>'付費方式', ''))
            ~ '(發票|統編|統一編號)' AS invoice,
        (COALESCE(doc->>'接送車型', '') || ' ' || COALESCE(doc->>'備註', ''))
            ~* '(lm|阿法|alphard)' AS luxury,
        COALESCE(doc->>'服務項目', '') ~ '(單程|包車)' AS svc
    FROM parsed p LEFT JOIN drivers d ON d.order_id = p.id::text
)
"""


def date_number(value):
    match = re.fullmatch(r"\s*(\d{1,2})\s*[/／]\s*(\d{1,2})\s*", value or "")
    if not match:
        raise ValueError("invalid_date")
    month, day = map(int, match.groups())
    datetime(2000, month, day)
    return month * 100 + day


def list_options(args, default_date):
    sort = args.get("sort", "date")
    driver = args.get("driver", "all")
    if sort not in ("date", "created") or driver not in ("all", "assigned", "unassigned"):
        raise ValueError("invalid_filter")
    try:
        page = max(1, int(args.get("page", 1)))
    except (ValueError, TypeError):
        raise ValueError("invalid_page") from None
    tab = args.get("tab", default_date)
    options = {"sort": sort, "driver": driver, "page": page,
               "q": args.get("q", "").strip()[:200], "tab": tab,
               "invoice": args.get("invoice") == "1", "luxury": args.get("luxury") == "1",
               "svc": args.get("svc") == "1", "start": None, "end": None}
    if tab not in ("custom", "all"):
        options["start"] = options["end"] = date_number(tab)
    elif tab == "custom":
        for name in ("start", "end"):
            if args.get(name):
                options[name] = date_number(args[name])
    return options


def scope_clause(options):
    if options["q"]:
        # strpos treats percent signs and underscores as literal search characters.
        return "strpos(lower(COALESCE(doc->>'訂單號碼', '') || ' ' || COALESCE(doc->>'聯絡電話', '')), lower(%s)) > 0", [options["q"]]
    date_column = "created_day" if options["sort"] == "created" else "booking_day"
    start, end = options["start"], options["end"]
    if start is not None and end is not None:
        if start == end:
            return f"{date_column} = %s", [start]
        join = "OR" if start > end else "AND"
        return f"({date_column} >= %s {join} {date_column} <= %s)", [start, end]
    if start is not None:
        return f"{date_column} >= %s", [start]
    if end is not None:
        return f"{date_column} <= %s", [end]
    return "TRUE", []


def filter_clause(options):
    terms = []
    if options["driver"] == "assigned":
        terms.append("assigned")
    elif options["driver"] == "unassigned":
        terms.append("NOT assigned")
    terms.extend(name for name in ("invoice", "luxury", "svc") if options[name])
    return " AND ".join(terms) or "TRUE"


def get_order_page(get_db, options):
    scope, params = scope_clause(options)
    filters = filter_clause(options)
    ordering = "created_at DESC, id DESC" if options["sort"] == "created" else "booking_day ASC NULLS LAST, time_minutes ASC, id ASC"
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '15s'")
            cur.execute(LISTING_CTE + f"""
                SELECT count(*) AS total,
                    count(*) FILTER (WHERE assigned) AS assigned,
                    count(*) FILTER (WHERE NOT assigned) AS unassigned,
                    count(*) FILTER (WHERE invoice) AS invoice,
                    count(*) FILTER (WHERE luxury) AS luxury,
                    count(*) FILTER (WHERE svc) AS svc,
                    count(*) FILTER (WHERE {filters}) AS matched
                FROM listing WHERE {scope}
            """, params)
            counts = dict(cur.fetchone())
            total = counts.pop("matched")
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(options["page"], pages)
            cur.execute(LISTING_CTE + f"""
                SELECT id, doc, driver_name FROM listing
                WHERE ({scope}) AND ({filters})
                ORDER BY {ordering} LIMIT %s OFFSET %s
            """, params + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
            rows = cur.fetchall()
    orders, drivers = [], {}
    for row in rows:
        order = dict(row["doc"])
        order["_id"] = str(row["id"])
        orders.append(order)
        if row["driver_name"]:
            drivers[order["_id"]] = row["driver_name"]
    return {"orders": orders, "drivers": drivers, "counts": counts,
            "total": total, "page": page, "pages": pages, "page_size": PAGE_SIZE}


def get_order_summary(get_db, sort):
    if sort not in ("date", "created"):
        raise ValueError("invalid_sort")
    date_column = "created_day" if sort == "created" else "booking_day"
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout = '15s'")
            cur.execute(LISTING_CTE + f"""
                SELECT {date_column} AS date, count(*) AS total,
                    count(*) FILTER (WHERE invoice) AS invoice,
                    count(*) FILTER (WHERE luxury) AS luxury,
                    count(*) FILTER (WHERE svc) AS svc
                FROM listing WHERE {date_column} IS NOT NULL
                GROUP BY {date_column} ORDER BY {date_column}
            """)
            return [dict(row) for row in cur.fetchall()]
