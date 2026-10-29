import heapq
from types import SimpleNamespace

import numpy as np

from mobile_sensing.simulation.kernel import _Kernel


def snapshot_kernel(times, now=10, end=100, online=None):
    kernel = object.__new__(_Kernel)
    kernel.now_s, kernel.end_s = now, end
    kernel.vehicles, kernel.tasks, kernel.waiting_keys = {}, {}, set()
    kernel._waiting_snapshot = None
    kernel.online_pending = None if online is None else SimpleNamespace(release_s=online)
    kernel.queue = [((time, i), SimpleNamespace(time_s=time)) for i, time in enumerate(times)]
    heapq.heapify(kernel.queue)
    return kernel


def test_heap_snapshot_matches_full_scan_with_same_time_events_and_online_releases():
    rng = np.random.default_rng(1234)
    for size in (0, 1, 2, 3, 10, 1000):
        for online in (None, 10, 11, 1000):
            times = rng.integers(10, 20, size=size).tolist()
            kernel = snapshot_kernel(times, online=online)
            expected = min(
                [
                    100,
                    *(t for t in times if t > 10),
                    *([online] if online is not None and online > 10 else []),
                ]
            )
            assert kernel._snapshot().next_decision_s == expected
    assert snapshot_kernel([10] * 1000)._snapshot().next_decision_s == 100


def test_future_heap_root_needs_one_lookup_independent_of_queue_size():
    class CountedHeap(list):
        accesses = 0

        def __getitem__(self, index):
            self.accesses += 1
            return super().__getitem__(index)

    kernel = snapshot_kernel(range(11, 50011))
    kernel.queue = CountedHeap(kernel.queue)
    assert kernel._snapshot().next_decision_s == 11
    assert kernel.queue.accesses == 1
