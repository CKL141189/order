"""Lazy per-worker connection reuse with bounded checkout and no write retries."""
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock

import psycopg2
from psycopg2.pool import ThreadedConnectionPool


class Database:
    def __init__(self, url):
        self.url = url
        self._pool = None
        self._lock = Lock()
        self._slots = BoundedSemaphore(4)

    def _get_pool(self):
        with self._lock:
            if self._pool is None:
                self._pool = ThreadedConnectionPool(1, 4, self.url, connect_timeout=10)
            return self._pool

    @contextmanager
    def connection(self):
        if not self._slots.acquire(timeout=15):
            raise psycopg2.OperationalError('Database connection checkout timed out')
        conn = None
        broken = False
        try:
            pool = self._get_pool()
            # A connection may have been closed by the server while idle. Retry
            # only this health check, never application work or an uncertain commit.
            for attempt in range(2):
                conn = pool.getconn()
                try:
                    with conn.cursor() as cur:
                        cur.execute('SELECT 1')
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError):
                    pool.putconn(conn, close=True)
                    conn = None
                    if attempt:
                        raise
            try:
                yield conn
                conn.commit()
            except BaseException as exc:
                broken = isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
                try:
                    conn.rollback()
                except Exception:
                    broken = True
                raise
        finally:
            try:
                if conn is not None:
                    pool.putconn(conn, close=broken or bool(conn.closed))
            finally:
                self._slots.release()
