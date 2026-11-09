'''
Explicit-lifecycle ``items`` sessions.

A session wraps a single ``items`` import behind a context manager, reusing the
exact same coroutine pipeline (``items_basecoro`` -> ``parse_basecoro`` ->
``basic_parse_basecoro``) and :class:`~ijson.common.ObjectBuilder` chain that
the regular ``items``/``items_async`` functions use.

The three ways a session can end are kept strictly separate:

* Real EOF: the whole document is consumed. Truncated JSON still raises
  :class:`~ijson.common.IncompleteJSONError`.
* Consumer-driven early exit (``break`` while iterating, or an explicit
  :meth:`close`): remaining parsing is simply abandoned, without ever turning
  the cancellation into an input-corruption error.
* An exception raised by the source, the parser or the consumer code: the
  original exception is preserved; failures while cleaning the internal
  coroutines can never mask it.

Closing is idempotent. Once a session is closed it yields no further data --
not even objects already produced and cached -- and neither reads from the
source again. The source stays owned by the caller unless ``close_source=True``
is given, in which case the matching ``close``/``aclose`` is invoked on it.

No background work (and in particular no background thread draining the
source) is ever started; cleanup only tears down the in-memory pipeline.
'''
import weakref

from ijson import common, utils
from ijson.utils35 import sendable_deque
from ijson.utils35 import _get_read


def _close_coro(coro):
    '''Closes a pipeline coroutine, swallowing every cleanup failure.'''
    try:
        coro.close()
    except BaseException:
        pass


def _sync_close_source(source):
    close = getattr(source, 'close', None)
    if close is None:
        return
    close()


async def _async_close_source(source):
    aclose = getattr(source, 'aclose', None)
    if aclose is not None:
        await aclose()
        return
    close = getattr(source, 'close', None)
    if close is not None:
        close()


def _flag_finalized(holder):
    holder[0] = True


class _SessionBase:
    '''Shared state and closing semantics for both session flavors.'''

    def __init__(self, backend, source, prefix, map_type, buf_size,
                 config, close_source):
        self.backend = backend
        self.source = source
        self.prefix = prefix
        self.buf_size = buf_size
        self.close_source = close_source
        self._closed = False
        self.events = sendable_deque()
        self.coro = utils.chain(
            self.events,
            *common._items_pipeline(backend, prefix, map_type, config)
        )
        # Deterministic evidence that the internal parsing coroutines and
        # their buffers are released on close rather than waiting for the
        # garbage collector. The holder is shared with the finalizer instead
        # of ``self`` so no session <-> finalizer reference cycle exists:
        # refcounting alone, with cyclic GC disabled, closes and deallocates
        # the whole coroutine chain.
        self._released = [False]
        self._finalizer = weakref.finalize(
            self.coro, _flag_finalized, self._released
        )

    @property
    def closed(self):
        '''True once the session has been closed (idempotently).'''
        return self._closed

    @property
    def coro_finalized(self):
        '''True once the internal parsing coroutine chain has been released.'''
        return self._released[0]

    def _discard_buffers(self):
        self.events.clear()


class ItemsSession(_SessionBase):
    '''
    A synchronous, context-managed ``items`` import.

    Iterate over the session to receive the objects found under ``prefix``;
    the parsing pipeline is only fed from the source while iteration asks for
    another object.
    '''

    def __init__(self, backend, source, prefix, map_type=None,
                 buf_size=64 * 1024, close_source=False, **config):
        source = common._get_source(source)
        if common.is_async_file(source):
            raise ValueError(
                "An asynchronous source requires an AsyncItemsSession; "
                "use ijson.items_session inside an async context"
            )
        super().__init__(backend, source, prefix, map_type, buf_size,
                         config, close_source)
        if common.is_file(source):
            self._data = iter(common.file_source(source, buf_size))
        elif common.is_iterable(source):
            self._data = iter(source)
        else:
            raise ValueError("Unknown source type: %r" % type(source))

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        while not self.events:
            data = next(self._data)
            try:
                self.coro.send(data)
            except StopIteration:
                if self.events:
                    break
                raise
        return self.events.popleft()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.close()
        else:
            # An exception is already in flight: cleanup failures, including
            # source close failures, must never mask the original one.
            try:
                self.close()
            except BaseException:
                pass
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            await self.aclose()
        else:
            try:
                await self.aclose()
            except BaseException:
                pass
        return False

    def close(self):
        '''
        Stops parsing and releases the pipeline. Idempotent.

        A source ``close`` failure (only possible with ``close_source=True``)
        propagates when the session is closed cleanly; it is swallowed when an
        exception is already in flight so the original one always wins.
        '''
        if self._closed:
            return
        self._closed = True
        self._discard_buffers()
        _close_coro(self.coro)
        self.coro = None
        # Drop the data iterator too: any buffer it holds must not survive
        # the session, and it must never touch the source again.
        self._data = None
        if self.close_source:
            _sync_close_source(self.source)

    async def aclose(self):
        '''Asynchronous counterpart of :meth:`close`.'''
        if self._closed:
            return
        self._closed = True
        self._discard_buffers()
        _close_coro(self.coro)
        self.coro = None
        self._data = None
        if self.close_source:
            await _async_close_source(self.source)


class AsyncItemsSession(_SessionBase):
    '''
    An asynchronous, context-managed ``items`` import.

    The session is an async iterator; the underlying coroutine pipeline is the
    synchronous one used everywhere else in ijson, while source reads are
    awaited. A :class:`asyncio.CancelledError` raised while awaiting a read
    propagates untouched -- it is a cancellation, not corrupt input.
    '''

    def __init__(self, backend, source, prefix, map_type=None,
                 buf_size=64 * 1024, close_source=False, **config):
        source = common._get_source(source)
        super().__init__(backend, source, prefix, map_type, buf_size,
                         config, close_source)
        self._read = None
        self._exhausted = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        if self._read is None:
            self._read = await _get_read(self.source)
        while not self.events:
            if self._exhausted:
                raise StopAsyncIteration
            data = await self._read(self.buf_size)
            try:
                self.coro.send(data)
            except StopIteration:
                self._exhausted = True
                if not self.events:
                    raise StopAsyncIteration
        return self.events.popleft()

    def __enter__(self):
        raise RuntimeError(
            "AsyncItemsSession must be used with 'async with', not 'with'"
        )

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException:
                pass
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            await self.aclose()
        else:
            try:
                await self.aclose()
            except BaseException:
                pass
        return False

    def close(self):
        '''
        Synchronous teardown. Idempotent.

        Only an asynchronous source whose ownership was transferred
        (``close_source=True``) and that exposes a synchronous ``close`` can
        be closed from here; an ``aclose``-only source requires
        :meth:`aclose`.
        '''
        if self._closed:
            return
        self._closed = True
        self._discard_buffers()
        _close_coro(self.coro)
        self.coro = None
        self._read = None
        if self.close_source and not hasattr(self.source, 'aclose'):
            _sync_close_source(self.source)

    async def aclose(self):
        '''Asynchronous teardown. Idempotent.'''
        if self._closed:
            return
        self._closed = True
        self._discard_buffers()
        _close_coro(self.coro)
        self.coro = None
        self._read = None
        if self.close_source:
            await _async_close_source(self.source)


def _make_items_session(backend):
    def items_session(source, prefix, map_type=None, buf_size=64 * 1024,
                      close_source=False, **config):
        '''
        Opens an explicit-lifecycle ``items`` session over ``source``.

        An asynchronous file-like source produces an
        :class:`AsyncItemsSession` (use ``async with`` and ``async for``);
        any other supported source produces a synchronous
        :class:`ItemsSession` (use ``with`` and ``for``).
        '''
        dispatched = common._get_source(source)
        if common.is_async_file(dispatched):
            return AsyncItemsSession(
                backend, source, prefix, map_type=map_type,
                buf_size=buf_size, close_source=close_source, **config
            )
        return ItemsSession(
            backend, source, prefix, map_type=map_type,
            buf_size=buf_size, close_source=close_source, **config
        )
    return items_session
