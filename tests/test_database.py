import unittest
from unittest.mock import MagicMock, patch

import psycopg2

from database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.factory = patch('database.ThreadedConnectionPool').start()
        self.addCleanup(patch.stopall)
        self.pool = self.factory.return_value
        self.conn = self.pool.getconn.return_value
        self.conn.closed = False
        self.db = Database('test-only')

    def test_lazy_pool_reused_and_transactions_committed(self):
        self.factory.assert_not_called()
        for _ in range(2):
            with self.db.connection() as conn:
                self.assertIs(conn, self.conn)
        self.factory.assert_called_once()
        self.assertEqual(self.conn.commit.call_count, 2)
        self.assertEqual(self.pool.putconn.call_count, 2)

    def test_failed_write_rolls_back_without_retry(self):
        with self.assertRaises(ValueError):
            with self.db.connection():
                raise ValueError('write failed')
        self.conn.rollback.assert_called_once()
        self.conn.commit.assert_not_called()
        self.pool.getconn.assert_called_once()

    def test_stale_connection_replaced_before_work(self):
        stale = MagicMock()
        stale.cursor.return_value.__enter__.return_value.execute.side_effect = psycopg2.OperationalError('gone')
        self.pool.getconn.side_effect = [stale, self.conn]
        with self.db.connection() as conn:
            self.assertIs(conn, self.conn)
        self.pool.putconn.assert_any_call(stale, close=True)

    def test_commit_failure_is_not_retried_and_connection_discarded(self):
        self.conn.commit.side_effect = psycopg2.OperationalError('lost during commit')
        with self.assertRaises(psycopg2.OperationalError):
            with self.db.connection():
                pass
        self.pool.getconn.assert_called_once()
        self.pool.putconn.assert_called_with(self.conn, close=True)
