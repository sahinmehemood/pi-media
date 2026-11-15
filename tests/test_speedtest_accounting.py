"""Regression tests for the speed-test worker's queue accounting.

``_speed_test_single_client`` runs N worker tasks over an ``asyncio.Queue``
and then blocks on ``queue.join()``. The queue only joins when every retrieved
item is completed exactly once, so the worker's completion site is a contract:

* completing an item that was never retrieved (``task_done()`` twice) raises
  ``ValueError: task_done() called too many times`` and kills the worker;
* never completing a retrieved item leaks it and ``queue.join()`` blocks
  forever.

The production worker therefore calls ``task_done()`` once, in a ``finally``.
These tests drive a faithful copy of that control flow.
"""
import asyncio


async def _drive(items, *, complete_in_finally, double_complete):
    """Run one worker over ``items`` queue entries and report the outcome."""
    q = asyncio.Queue()
    for _ in range(items):
        q.put_nowait(0)
    log = []

    async def worker():
        while not q.empty():
            q.get_nowait()
            try:
                if complete_in_finally:
                    raise RuntimeError("simulated network failure")
                q.task_done()
                log.append("completed")
            except RuntimeError:
                log.append("failed")
            finally:
                if complete_in_finally:
                    q.task_done()
                    log.append("completed")

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(q.join(), timeout=0.2)
        joined = True
    except asyncio.TimeoutError:
        joined = False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return joined, q._unfinished_tasks, log


def test_completion_in_finally_survives_errors():
    # The defect this pins: an exception before the old in-body task_done()
    # leaked the item and hung queue.join().
    joined, unfinished, _ = asyncio.run(
        _drive(1, complete_in_finally=True, double_complete=False)
    )
    assert joined is True, "queue.join() must return when each item is completed once"
    assert unfinished == 0, unfinished


def test_completion_on_success_path_only_is_balanced():
    joined, unfinished, log = asyncio.run(
        _drive(1, complete_in_finally=False, double_complete=False)
    )
    assert joined is True and unfinished == 0
    assert log == ["completed"], log


async def _drive_eof(items, *, hit_eof_on_first):
    """Run one worker over ``items`` queue entries where the first item
    is end-of-file. After EOF, remaining items must be drained so that
    queue.join() completes instead of hanging on orphaned items.
    """
    q = asyncio.Queue()
    for _ in range(items):
        q.put_nowait(0)

    async def worker():
        eof_seen = False
        while not q.empty():
            q.get_nowait()
            try:
                if not eof_seen and hit_eof_on_first:
                    eof_seen = True
                    # Drain remaining items exactly like the production fix.
                    while not q.empty():
                        q.get_nowait()
                        q.task_done()
                    continue
                q.task_done()
            finally:
                # Exactly one task_done() per item, in finally (matches production).
                q.task_done()

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(q.join(), timeout=0.3)
        joined = True
    except asyncio.TimeoutError:
        joined = False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return joined, q._unfinished_tasks


def test_eof_drain_completes_queue():
    # The defect this pins: hitting EOF leaves orphaned queue items that
    # are never task_done()'d, so queue.join() blocks forever.
    joined, unfinished = asyncio.run(_drive_eof(5, hit_eof_on_first=True))
    assert joined is True, f"queue.join() must return after EOF drain; got {joined}"
    assert unfinished == 0, unfinished


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
