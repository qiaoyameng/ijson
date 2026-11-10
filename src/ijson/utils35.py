'''
Python3.5+ specific utilities
'''
import collections

from ijson import utils, common, compat


class utf8reader_async(compat.utf8reader):
    """
    Takes a utf8-encoded string asynchronous reader and asynchronously reads
    bytes out of it
    """
    async def read(self, n):
        data = await self.str_reader.read(n)
        return data.encode('utf-8')

async def _get_read(f):
    """Returns an awaitable read function that reads the requested type"""
    if type(await f.read(0)) == bytes:
        return f.read
    # Text readers are consumed directly by the python backend instead of
    # being re-encoded to UTF-8; keep the historical deprecation warning.
    compat._warn_and_return(None)
    return f.read

class sendable_deque(collections.deque):
    '''Like utils.sendable_list, but for deque objects'''
    send = collections.deque.append

class async_iterable:
    '''
    A utility class that implements an async iterator returning values
    dispatched by a coroutine pipeline after *it* has received values coming
    from an async file-like object.
    '''

    def __init__(self, f, buf_size, *coro_pipeline, path_state=None):
        self.events = sendable_deque()
        self.coro = utils.chain(self.events, *coro_pipeline)
        self.coro_finished = False
        self.f = f
        self.buf_size = buf_size
        self.read = None
        self.path_state = path_state

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.read:
            self.read = await _get_read(self.f)
        if self.events:
            return self.events.popleft()
        if self.coro_finished:
            raise StopAsyncIteration
        while True:
            data = await self.read(self.buf_size)
            try:
                self.coro.send(data)
                if self.events:
                    return self.events.popleft()
            except common.JSONError as ex:
                if self.path_state is not None:
                    ex.with_location(prefix=self.path_state.prefix)
                raise
            except StopIteration:
                self.coro_finished = True
                if self.events:
                    return self.events.popleft()
                raise StopAsyncIteration


def _make_basic_parse_async(backend):
    def basic_parse_async(f, buf_size=64*1024, **config):
        return async_iterable(f, buf_size,
            *common._basic_parse_pipeline(backend, config)
        )
    return basic_parse_async

def _make_parse_async(backend):
    def parse_async(f, buf_size=64*1024, **config):
        state = common._PathState()
        return async_iterable(f, buf_size,
            (common.path_tracker_coro, (state,), {}),
            *common._parse_pipeline(backend, config),
            path_state=state,
        )
    return parse_async

def _make_items_async(backend):
    def items_async(f, prefix, map_type=None, buf_size=64*1024, **config):
        state = common._PathState()
        return async_iterable(f, buf_size,
            (common.path_tracker_coro, (state,), {}),
            *common._items_pipeline(backend, prefix, map_type, config),
            path_state=state,
        )
    return items_async

def _make_kvitems_async(backend):
    def kvitems_async(f, prefix, map_type=None, buf_size=64*1024, **config):
        state = common._PathState()
        return async_iterable(f, buf_size,
            (common.path_tracker_coro, (state,), {}),
            *common._kvitems_pipeline(backend, prefix, map_type, config),
            path_state=state,
        )
    return kvitems_async
