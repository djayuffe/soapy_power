"""Thread pools with bounded submissions and public-API backpressure."""
import os
import threading
import concurrent.futures


class ThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """Bound pending work without replacing the executor's internal queue."""
    def __init__(self, max_workers=0, thread_name_prefix='', max_queue_size=0):
        if max_workers < 0 or max_queue_size < -1:
            raise ValueError('invalid worker or queue limit')
        super().__init__(max_workers=max_workers or os.cpu_count() or 1,
                         thread_name_prefix=thread_name_prefix)
        self.max_queue_size = max_queue_size or self._max_workers * 10
        self._slots = (threading.BoundedSemaphore(self._max_workers + self.max_queue_size)
                       if self.max_queue_size > 0 else None)
        self.max_queue_size_reached = 0
        self._pending = 0
        self._pending_lock = threading.Lock()

    def _finished(self, future):
        with self._pending_lock:
            self._pending -= 1
        if self._slots:
            self._slots.release()

    def submit(self, fn, *args, **kwargs):
        if self._slots:
            self._slots.acquire()
        with self._pending_lock:
            self._pending += 1
            queued = max(0, self._pending - self._max_workers)
            self.max_queue_size_reached = max(self.max_queue_size_reached, queued)
        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            self._finished(None)
            raise
        future.add_done_callback(self._finished)
        return future
