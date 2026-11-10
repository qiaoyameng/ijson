"""Tests for the segmented string events interface of the python backend"""

import asyncio
import io

import pytest

import ijson
from ijson import common
from ijson.backends import python as python_backend

from .test_base import (
    EMBEDDED_NUL_JSON,
    JSON,
    STRINGS_JSON,
    SURROGATE_PAIRS_JSON,
)

LONG_ESCAPES_JSON = b'"' + b'\\u0041' * 1000 + b'"'
LONG_MIXED_JSON = (
    b'{"esc": "' + b'\\n\\t\\\\\\"' * 100 + b'", "raw": "'
    + '💩🎉é'.encode() * 100 + b'"}'
)
PARITY_JSONS = [
    JSON,
    STRINGS_JSON,
    SURROGATE_PAIRS_JSON,
    EMBEDDED_NUL_JSON,
    LONG_ESCAPES_JSON,
    LONG_MIXED_JSON,
    b'"\\uD83D"',
    b'"\\uD83D\\u0041"',
    b'"\\uDCA9"',
    b'{"": "", "k": [""]}',
]


def _segmented(json, **kwargs):
    kwargs.setdefault('string_chunk_size', 7)
    return ijson.basic_parse_segmented(io.BytesIO(json), **kwargs)


def _adapted(json, **kwargs):
    return list(ijson.segmented_to_events(_segmented(json, **kwargs)))


@pytest.mark.parametrize('json', PARITY_JSONS)
def test_adapted_matches_basic_parse(json):
    assert _adapted(json) == list(ijson.basic_parse(json))


@pytest.mark.parametrize('json', PARITY_JSONS)
def test_adapted_matches_basic_parse_bytewise(json):
    events = _adapted(json, buf_size=1)
    assert events == list(ijson.basic_parse(io.BytesIO(json), buf_size=1))


@pytest.mark.parametrize('chunk_size', [1, 2, 7, 64])
def test_chunks_are_bounded_and_non_empty(chunk_size):
    events = _segmented(LONG_MIXED_JSON, string_chunk_size=chunk_size)
    chunks = [v for e, v in events if e == 'string_chunk']
    assert chunks
    assert all(0 < len(c) <= chunk_size for c in chunks)
    if chunk_size == 1:
        assert all(len(c) == 1 for c in chunks)


def test_chunks_reconstruct_strings():
    events = _segmented(LONG_MIXED_JSON, string_chunk_size=3)
    strings = []
    current = None
    for event, value in events:
        if event == 'start_string':
            assert current is None
            current = []
        elif event == 'string_chunk':
            current.append(value)
        elif event == 'end_string':
            strings.append(''.join(current))
            current = None
    assert strings == ['\n\t\\"' * 100, '💩🎉é' * 100]


def test_surrogate_pair_never_split():
    events = list(_segmented(SURROGATE_PAIRS_JSON, buf_size=1, string_chunk_size=1))
    assert events == [
        ('start_string', None),
        ('string_chunk', '💩'),
        ('end_string', None),
    ]


def test_empty_string_has_no_chunks():
    assert list(ijson.basic_parse_segmented(b'{"": ""}')) == [
        ('start_map', None),
        ('map_key', ''),
        ('start_string', None),
        ('end_string', None),
        ('end_map', None),
    ]


def _events_until_error(json, error, **kwargs):
    events = []
    with pytest.raises(error):
        for event in _segmented(json, **kwargs):
            events.append(event)
    return events


@pytest.mark.parametrize('json', [
    b'"\\x"',            # invalid escape
    b'"\\u12g4"',        # invalid hex digits
    b'"\\uD83D\\uzzzz"', # invalid hex digits in low surrogate
    b'"raw\x01char"',    # control character
])
def test_invalid_strings_raise(json):
    events = _events_until_error(json, common.JSONError)
    assert ('start_string', None) in events
    # no fake end_string is emitted to pretend completion
    assert ('end_string', None) not in events


@pytest.mark.parametrize('json', [
    b'"abc',
    b'"abc\\',
    b'"abc\\u12',
    b'"\\uD83D',
    b'"\\uD83D\\',
    b'"\\uD83D\\uDC',
])
def test_truncated_strings_raise(json):
    events = _events_until_error(json, common.IncompleteJSONError)
    assert ('end_string', None) not in events


def test_truncated_document_raises():
    with pytest.raises(common.IncompleteJSONError):
        list(_segmented(b'{"key": "value"'))


def test_map_key_length_limit():
    json = b'{"abcdefghij": 1}'
    with pytest.raises(common.JSONError):
        list(ijson.basic_parse_segmented(json, map_key_max_length=5))
    events = list(ijson.basic_parse_segmented(json, map_key_max_length=10))
    assert ('map_key', 'abcdefghij') in events


def test_map_key_limit_does_not_affect_values():
    json = b'{"k": "a very long value indeed"}'
    events = list(ijson.basic_parse_segmented(
        json, map_key_max_length=1, string_chunk_size=100
    ))
    assert ('string_chunk', 'a very long value indeed') in events


def test_invalid_chunk_size():
    with pytest.raises(ValueError):
        list(ijson.basic_parse_segmented(b'"x"', string_chunk_size=0))


class _CountingReader:
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        return self.data.read(n)


def test_early_close_stops_reading():
    json = b'["' + b'x' * 10000 + b'", 1, 2, 3]'
    reader = _CountingReader(json)
    gen = ijson.basic_parse_segmented(reader, buf_size=16, string_chunk_size=8)
    for _ in zip(range(4), gen):
        pass
    gen.close()
    reads_after_close = reader.reads
    # the source is no longer read, and was not fully consumed
    assert reader.reads == reads_after_close
    assert reads_after_close < len(json) // 16


class _AsyncCountingReader:
    def __init__(self, data):
        self.data = io.BytesIO(data)
        self.reads = 0

    async def read(self, n=-1):
        self.reads += 1
        await asyncio.sleep(0)
        return self.data.read(n)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_async_matches_sync():
    async def collect():
        events = []
        reader = _AsyncCountingReader(LONG_MIXED_JSON)
        async for event in ijson.basic_parse_segmented(
            reader, buf_size=3, string_chunk_size=5
        ):
            events.append(event)
        return events

    assert _run(collect()) == list(_segmented(LONG_MIXED_JSON, string_chunk_size=5))


def test_async_adapter_matches_basic_parse():
    async def collect():
        events = []
        reader = _AsyncCountingReader(JSON)
        segmented = ijson.basic_parse_segmented(reader, buf_size=1)
        async for event in ijson.asegmented_to_events(segmented):
            events.append(event)
        return events

    assert _run(collect()) == list(ijson.basic_parse(JSON))


def test_async_early_stop_stops_reading():
    json = b'["' + b'x' * 10000 + b'", 1, 2, 3]'
    reader = _AsyncCountingReader(json)

    async def consume():
        count = 0
        async for _ in ijson.basic_parse_segmented(
            reader, buf_size=16, string_chunk_size=8
        ):
            count += 1
            if count == 4:
                break
        reads_at_break = reader.reads
        await asyncio.sleep(0.01)
        return reads_at_break

    reads_at_break = _run(consume())
    assert reader.reads == reads_at_break
    assert reads_at_break < len(json) // 16


def test_python_backend_entry_points():
    assert python_backend.basic_parse_segmented is ijson.basic_parse_segmented
    events = python_backend.basic_parse_segmented_gen(
        io.BytesIO(b'"abc"'), string_chunk_size=2
    )
    assert list(events) == [
        ('start_string', None),
        ('string_chunk', 'ab'),
        ('string_chunk', 'c'),
        ('end_string', None),
    ]
