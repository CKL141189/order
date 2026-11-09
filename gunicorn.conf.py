# Keep one process on the small instance, but do not serialize database waits.
worker_class = "gthread"
workers = 1
threads = 4
