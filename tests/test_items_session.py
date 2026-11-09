"""
Tests for the explicit-lifecycle ``items_session`` API.

The sources used here hand the data out in small, countable chunks and keep
counters of every read and close. Every cleanup assertion therefore relies on
explicit counts and on :class:`weakref.finalize` firing deterministically
(cyclic GC is disabled) instead of hoping the garbage collector eventually
releases the internal pipeline.
"""
import asyncio
import gc
import io
import threading

import pytest

import ijson
from ijson import IncompleteJSONError
from ijson.sessions import AsyncItemsSession, ItemsSession


JSON = b'{"item": [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}, {"a": 5}]}'
EXPECTED = [{"a": i} for i in range(1, 6)]
PREFIX = "item.item"
SMALL_BUF = 4


class CountingSyncSource:
    """File-like byte source serving fixed-size chunks with counters."""

    def __init__(self, data, chunk_size=SMALL_BUF, fail_on_read=None):
        self._data = io.BytesIO(data)
        self.chunk_size = chunk_size
        self.fail_on_read = fail_on_read
        self.read_count = 0
        self.closed_count = 0

    def read(self, n=-1):
        self.read_count += 1
        if self.fail_on_read is not None and self.read_count == self.fail_on_read:
            raise OSError("boom from sync source")
        return self._data.read(min(n, self.chunk_size))

    def close(self):
        self.closed_count += 1


class CountingAsyncSource:
    """Async file-like byte source with read/close counters and block points."""

    def __init__(self, data, chunk_size=SMALL_BUF, fail_on_read=None):
        self._data = io.BytesIO(data)
        self.chunk_size = chunk_size
        self.fail_on_read = fail_on_read
        self.read_count = 0
        self.closed_count = 0
        self.aclosed_count = 0
        self.block_event = asyncio.Event()

    async def read(self, n=-1):
        self.read_count += 1
        if self.fail_on_read is not None and self.read_count == self.fail_on_read:
            raise OSError("boom from async source")
        await self.block_event.wait()
        await asyncio.sleep(0)
        return self._data.read(min(n, self.chunk_size))

    async def aclose(self):
        self.aclosed_count += 1

    def close(self):
        self.closed_count += 1


def test_dispatch_and_pure_python_backend():
    assert ijson.backend_name == "python"
    sync_session = ijson.items_session(CountingSyncSource(JSON), PREFIX)
    assert isinstance(sync_session, ItemsSession)
    sync_session.close()
    async_session = ijson.items_session(CountingAsyncSource(JSON), PREFIX)
    assert isinstance(async_session, AsyncItemsSession)
    asyncio.run(async_session.aclose())


def test_full_exhaustion_completes_and_cleans_up():
    source = CountingSyncSource(JSON)
    gc.disable()
    try:
        with ijson.items_session(source, PREFIX, buf_size=SMALL_BUF) as session:
            assert not session.closed
            results = list(session)
        # Real EOF: every chunk of the source was read, nothing left buffered
        assert results == EXPECTED
        assert session.closed
        assert source.read_count > 1
        reads_at_exit = source.read_count
        # Closed sessions never touch the source again, nor emit cached items
        assert list(session) == []
        assert source.read_count == reads_at_exit
        # Deterministic release without the cyclic garbage collector
        assert session.coro_finalized
        assert len(session.events) == 0
        # The caller still owns the source by default
        assert source.closed_count == 0
    finally:
        gc.enable()


def test_truncated_json_at_real_eof_raises_incomplete():
    source = CountingSyncSource(b'{"item": [{"a": 1}, {"a":')
    with pytest.raises(IncompleteJSONError):
        with ijson.items_session(source, PREFIX, buf_size=SMALL_BUF) as session:
            list(session)
    # The error path still closes the session and releases the pipeline
    assert session.closed
    assert session.coro_finalized
    assert source.closed_count == 0


def test_early_consumer_exit_is_not_corruption():
    source = CountingSyncSource(JSON)
    gc.disable()
    try:
        with ijson.items_session(source, PREFIX, buf_size=SMALL_BUF) as session:
            first = next(session)
            reads_while_using = source.read_count
        assert first == {"a": 1}
        assert session.closed
        # No draining to EOF: the source was abandoned mid-stream
        assert source.read_count == reads_while_using
        assert reads_while_using < len(JSON) // SMALL_BUF + 1
        assert session.coro_finalized
        assert list(session) == []
        assert source.read_count == reads_while_using
    finally:
        gc.enable()


def test_close_is_idempotent_and_releases_pipeline_without_gc():
    source = CountingSyncSource(JSON)
    gc.disable()
    try:
        session = ijson.items_session(source, PREFIX, buf_size=SMALL_BUF)
        assert next(session) == {"a": 1}
        for _ in range(3):
            session.close()
        assert session.closed
        assert session.coro_finalized
        assert source.closed_count == 0
        with pytest.raises(StopIteration):
            next(session)
        reads_after_close = source.read_count
        assert list(session) == []
        assert source.read_count == reads_after_close
    finally:
        gc.enable()


def test_source_ownership_transfer_on_close():
    source = CountingSyncSource(JSON)
    with ijson.items_session(
        source, PREFIX, buf_size=SMALL_BUF, close_source=True
    ) as session:
        next(session)
    assert source.closed_count == 1
    assert session.closed
    # Idempotent: the source is not closed a second time
    session.close()
    assert source.closed_count == 1


def test_source_error_preserves_original_exception_and_still_cleans_up():
    source = CountingSyncSource(JSON, fail_on_read=3)
    gc.disable()
    try:
        with pytest.raises(OSError, match="boom from sync source"):
            with ijson.items_session(
                source, PREFIX, buf_size=SMALL_BUF
            ) as session:
                list(session)
        assert session.closed
        assert session.coro_finalized
        reads = source.read_count
        assert list(session) == []
        assert source.read_count == reads
    finally:
        gc.enable()


def test_consumer_exception_is_preserved():
    class Boom(Exception):
        pass

    source = CountingSyncSource(JSON)
    gc.disable()
    try:
        with pytest.raises(Boom):
            with ijson.items_session(source, PREFIX, buf_size=SMALL_BUF) as session:
                for obj in session:
                    assert obj == EXPECTED[0]
                    raise Boom("consumer failure")
        assert session.closed
        assert session.coro_finalized
    finally:
        gc.enable()


def test_source_close_failure_clean_exit_propagates_but_error_exit_swallows():
    class FailingCloseSource(CountingSyncSource):
        def close(self):
            super().close()
            raise OSError("boom while closing source")

    # Clean exit: the close failure is visible to the caller
    good_source = FailingCloseSource(JSON)
    with pytest.raises(OSError, match="boom while closing source"):
        with ijson.items_session(
            good_source, PREFIX, buf_size=SMALL_BUF, close_source=True
        ) as session:
            list(session)
    assert session.closed
    assert good_source.closed_count == 1

    # Error exit: the original read failure wins, the close failure is swallowed
    bad_source = FailingCloseSource(JSON, fail_on_read=2)
    with pytest.raises(OSError, match="boom from sync source"):
        with ijson.items_session(
            bad_source, PREFIX, buf_size=SMALL_BUF, close_source=True
        ) as session:
            list(session)
    assert session.closed
    assert bad_source.closed_count == 1


def test_no_background_thread_is_started():
    source = CountingSyncSource(JSON)
    threads_before = threading.active_count()
    with ijson.items_session(source, PREFIX, buf_size=SMALL_BUF) as session:
        next(session)
        assert threading.active_count() == threads_before
    assert threading.active_count() == threads_before


def test_iterable_source_supported():
    chunks = [JSON[i:i + SMALL_BUF] for i in range(0, len(JSON), SMALL_BUF)]
    with ijson.items_session(iter(chunks), PREFIX) as session:
        assert list(session) == EXPECTED
    assert session.closed
    assert session.coro_finalized


def test_async_source_rejected_by_sync_constructor():
    with pytest.raises(ValueError):
        ItemsSession(
            dict(ijson.backend), CountingAsyncSource(JSON), PREFIX,
            buf_size=SMALL_BUF
        )


async def _async_full_exhaustion():
    source = CountingAsyncSource(JSON)
    source.block_event.set()
    async with ijson.items_session(
        source, PREFIX, buf_size=SMALL_BUF
    ) as session:
        assert isinstance(session, AsyncItemsSession)
        results = [obj async for obj in session]
    assert results == EXPECTED
    assert session.closed
    assert session.coro_finalized
    reads = source.read_count
    assert [obj async for obj in session] == []
    assert source.read_count == reads
    assert source.aclosed_count == 0


def test_async_full_exhaustion():
    asyncio.run(_async_full_exhaustion())


async def _async_early_exit():
    source = CountingAsyncSource(JSON)
    source.block_event.set()
    gc.disable()
    try:
        async with ijson.items_session(
            source, PREFIX, buf_size=SMALL_BUF
        ) as session:
            first = await session.__anext__()
            reads_while_using = source.read_count
        assert first == {"a": 1}
        assert session.closed
        assert source.read_count == reads_while_using
        assert session.coro_finalized
        with pytest.raises(StopAsyncIteration):
            await session.__anext__()
        assert source.read_count == reads_while_using
    finally:
        gc.enable()


def test_async_early_exit():
    asyncio.run(_async_early_exit())


async def _async_source_error():
    source = CountingAsyncSource(JSON, fail_on_read=2)
    source.block_event.set()
    gc.disable()
    try:
        with pytest.raises(OSError, match="boom from async source"):
            async with ijson.items_session(
                source, PREFIX, buf_size=SMALL_BUF
            ) as session:
                async for _ in session:
                    pass
        assert session.closed
        assert session.coro_finalized
    finally:
        gc.enable()


def test_async_source_error_preserved():
    asyncio.run(_async_source_error())


async def _async_cancelled_during_read():
    source = CountingAsyncSource(JSON)
    # block_event stays unset: the next read parks on an awaitable
    session = ijson.items_session(source, PREFIX, buf_size=SMALL_BUF)

    async def consume():
        try:
            async for _ in session:
                pass
        finally:
            # Mirrors what ``async with`` guarantees on cancellation
            await session.aclose()

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    assert source.read_count == 1  # parked inside the first await read()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation is not reported as corrupt input; cleanup still happened
    assert session.closed
    assert session.coro_finalized
    assert source.read_count == 1  # never resumed, never drained to EOF
    with pytest.raises(StopAsyncIteration):
        await session.__anext__()
    assert source.read_count == 1


def test_async_cancellation_during_read():
    asyncio.run(_async_cancelled_during_read())


async def _async_cancelled_inside_async_with():
    source = CountingAsyncSource(JSON)  # block_event unset: reads park
    holder = {}

    async def consume():
        async with ijson.items_session(
            source, PREFIX, buf_size=SMALL_BUF
        ) as session:
            holder["session"] = session
            async for _ in session:
                pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0)
    assert source.read_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session = holder["session"]
    assert session.closed
    assert session.coro_finalized
    assert source.read_count == 1


def test_async_cancellation_inside_async_with():
    asyncio.run(_async_cancelled_inside_async_with())


async def _async_ownership_transfer():
    source = CountingAsyncSource(JSON)
    source.block_event.set()
    async with ijson.items_session(
        source, PREFIX, buf_size=SMALL_BUF, close_source=True
    ) as session:
        await session.__anext__()
    assert source.aclosed_count == 1
    await session.aclose()  # idempotent
    assert source.aclosed_count == 1


def test_async_ownership_transfer_calls_aclose():
    asyncio.run(_async_ownership_transfer())


def test_async_session_rejects_plain_with():
    session = ijson.items_session(CountingAsyncSource(JSON), PREFIX)
    with pytest.raises(RuntimeError):
        with session:
            pass
    asyncio.run(session.aclose())
    assert session.closed
    assert session.coro_finalized
