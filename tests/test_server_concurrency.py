from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import runpy
from threading import Event, Lock
import unittest
from unittest.mock import patch

import app as order_app


class ServerConcurrencyTests(unittest.TestCase):
    def test_reads_overlap_but_writes_remain_serialized(self):
        first_started, second_started, release = Event(), Event(), Event()
        def request(method, started=None, wait=False):
            with order_app.app.test_request_context("/orders", method=method):
                order_app.app.preprocess_request()
                if started:
                    started.set()
                if wait:
                    release.wait(3)
            return True
        with patch.object(order_app, "_db_initialized", True):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(request, "POST", first_started, True)
                self.assertTrue(first_started.wait(1))
                second = pool.submit(request, "POST", second_started)
                read = pool.submit(request, "GET")
                try:
                    self.assertTrue(read.result(timeout=1))
                    self.assertFalse(second_started.wait(0.1), "Writes must not overwrite concurrent updates")
                finally:
                    release.set()
                    first.result(timeout=2)
                    second.result(timeout=2)

    def test_write_guard_is_released_after_errors(self):
        with patch.object(order_app, "_db_initialized", True):
            with self.assertRaises(RuntimeError):
                with order_app.app.test_request_context("/orders", method="POST"):
                    order_app.app.preprocess_request()
                    raise RuntimeError("test failure")
        self.assertFalse(order_app._write_lock.locked())

    def test_deployment_allows_overlapping_database_requests(self):
        path = Path(__file__).resolve().parents[1] / "gunicorn.conf.py"
        self.assertTrue(path.exists(), "Gunicorn needs an explicit concurrency configuration")
        config = runpy.run_path(str(path))
        self.assertEqual(config["worker_class"], "gthread")
        self.assertEqual(config["workers"], 1)
        self.assertGreaterEqual(config["threads"], 2)
        self.assertLessEqual(config["threads"], 4)

    def test_concurrent_first_requests_initialize_database_only_once(self):
        started, duplicate, release = Event(), Event(), Event()
        lock = Lock()
        calls = 0
        def initialize():
            nonlocal calls
            with lock:
                calls += 1
                if calls > 1:
                    duplicate.set()
            started.set()
            release.wait(3)
        with patch.object(order_app, "_db_initialized", False), patch.object(order_app, "init_db", initialize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(order_app.ensure_db)
                self.assertTrue(started.wait(1))
                second = pool.submit(order_app.ensure_db)
                try:
                    self.assertFalse(duplicate.wait(0.1), "Initialization must be serialized")
                finally:
                    release.set()
                    first.result(timeout=2)
                    second.result(timeout=2)
        self.assertEqual(calls, 1)
