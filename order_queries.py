"""Bounded order reads and date summaries over the existing JSON records."""

from datetime import datetime
import re

import psycopg2.extras


PAGE_SIZE = 100

# Only compact stored metadata participates in filtering and counting.
LISTING_CTE = r"""
WITH listing AS (
    SELECT o.id, COALESCE(d.name, '') AS driver_name,
        (listing_meta->>'booking_day')::int AS booking_day,
        (listing_meta->>'created_day')::int AS created_day,
        listing_meta->>'created_at' AS created_at,
        (listing_meta->>'time_minutes')::int AS time_minutes,
        listing_meta->>'search_text' AS search_text,
        (listing_meta->>'invoice')::boolean AS invoice,
        (listing_meta->>'luxury')::boolean AS luxury,
        (listing_meta->>'svc')::boolean AS svc,
        COALESCE(d.name, '') <> '' AS assigned
    FROM orders o LEFT JOIN drivers d ON d.order_id = o.id::text
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
        return "strpos(search_text, lower(%s)) > 0", [options["q"]]
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
            cur.execute(LISTING_CTE + f""",
                scoped AS MATERIALIZED (SELECT * FROM listing WHERE {scope}),
                counts AS (
                SELECT count(*) AS total,
                    count(*) FILTER (WHERE assigned) AS assigned,
                    count(*) FILTER (WHERE NOT assigned) AS unassigned,
                    count(*) FILTER (WHERE invoice) AS invoice,
                    count(*) FILTER (WHERE luxury) AS luxury,
                    count(*) FILTER (WHERE svc) AS svc,
                    count(*) FILTER (WHERE {filters}) AS matched
                FROM scoped
                ), pagination AS (
                    SELECT greatest(1, (matched + {PAGE_SIZE - 1}) / {PAGE_SIZE}) AS pages,
                        least(%s, greatest(1, (matched + {PAGE_SIZE - 1}) / {PAGE_SIZE})) AS page
                    FROM counts
                )
                SELECT counts.*, pagination.page, pagination.pages,
                    cards.id, payload.data::jsonb AS doc, cards.driver_name
                FROM counts CROSS JOIN pagination
                LEFT JOIN LATERAL (
                    SELECT id, driver_name,
                        row_number() OVER (ORDER BY {ordering}) AS position
                    FROM scoped WHERE {filters}
                    ORDER BY {ordering} LIMIT {PAGE_SIZE}
                    OFFSET ((pagination.page - 1) * {PAGE_SIZE})
                ) cards ON TRUE
                LEFT JOIN orders payload ON payload.id = cards.id
                ORDER BY cards.position
            """, params + [min(options["page"], 2147483647)])
            rows = cur.fetchall()
    # The left join keeps one metadata row even when no orders match.
    meta = rows[0]
    counts = {key: meta[key] for key in ("total", "assigned", "unassigned", "invoice", "luxury", "svc")}
    orders, drivers = [], {}
    for row in rows:
        if row["id"] is None:
            continue
        order = dict(row["doc"])
        order["_id"] = str(row["id"])
        orders.append(order)
        if row["driver_name"]:
            drivers[order["_id"]] = row["driver_name"]
    return {"orders": orders, "drivers": drivers, "counts": counts,
            "total": meta["matched"], "page": meta["page"], "pages": meta["pages"], "page_size": PAGE_SIZE}


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
