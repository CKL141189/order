"""Versioned, database-maintained listing metadata; raw order text is untouched."""

METADATA_FUNCTION = r"""
CREATE OR REPLACE FUNCTION order_listing_metadata_v1(raw text)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    doc jsonb := raw::jsonb;
    booking text[];
    created text[];
    clock text[];
    hour integer;
BEGIN
    booking := regexp_match(COALESCE(doc->>'預約日期', ''),
        '([0-9]{1,2})[[:space:]]*[/／][[:space:]]*([0-9]{1,2})');
    created := regexp_match(COALESCE(doc->>'created_at', ''),
        '^[0-9]{4}/([0-9]{1,2})/([0-9]{1,2})');
    clock := regexp_match(COALESCE(doc->>'預約時間', ''),
        '([0-9]{1,2})[[:space:]]*[:：][[:space:]]*([0-9]{2})');
    hour := clock[1]::int;
    IF strpos(COALESCE(doc->>'預約時間', ''), '下午') > 0 AND hour < 12 THEN
        hour := hour + 12;
    ELSIF strpos(COALESCE(doc->>'預約時間', ''), '上午') > 0 AND hour = 12 THEN
        hour := 0;
    END IF;
    RETURN jsonb_build_object(
        'booking_day', CASE WHEN booking[1]::int BETWEEN 1 AND 12 AND booking[2]::int BETWEEN 1 AND 31
            THEN booking[1]::int * 100 + booking[2]::int END,
        'created_day', CASE WHEN created[1]::int BETWEEN 1 AND 12 AND created[2]::int BETWEEN 1 AND 31
            THEN created[1]::int * 100 + created[2]::int END,
        'created_at', COALESCE(doc->>'created_at', ''),
        'time_minutes', COALESCE(hour * 60 + clock[2]::int, 0),
        'search_text', lower(COALESCE(doc->>'訂單號碼', '') || ' ' || COALESCE(doc->>'聯絡電話', '')),
        'invoice', (COALESCE(doc->>'備註', '') || ' ' || COALESCE(doc->>'付費方式', '')) ~ '(發票|統編|統一編號)',
        'luxury', (COALESCE(doc->>'接送車型', '') || ' ' || COALESCE(doc->>'備註', '')) ~* '(lm|阿法|alphard)',
        'svc', COALESCE(doc->>'服務項目', '') ~ '(單程|包車)');
END $$
"""


def ensure_listing_schema(cur):
    # Adding a STORED column backfills existing rows atomically. Future inserts,
    # edits and restores are maintained by PostgreSQL, including external writes.
    cur.execute(METADATA_FUNCTION)
    cur.execute("""ALTER TABLE orders ADD COLUMN IF NOT EXISTS listing_meta jsonb
        GENERATED ALWAYS AS (order_listing_metadata_v1(data)) STORED""")
    for column in ("booking_day", "created_day"):
        cur.execute(f"""CREATE INDEX IF NOT EXISTS orders_{column}_idx
            ON orders (((listing_meta->>'{column}')::integer))""")
