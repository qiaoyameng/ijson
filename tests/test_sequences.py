# -*- coding:utf-8 -*-
'''
Tests for the RFC 7464 JSON Text Sequences entry point.
'''
import io
import random
from decimal import Decimal

import pytest

from ijson import sequences
from ijson.sequences import SKIP, BadRecord, InvalidRecordError


RS_B = b'\x1e'

# A valid record, one with a corrupted escape inside a string, one holding a
# truncated number, and a complete object afterwards.
STREAM = (
    RS_B + b'{"a": 1}\n'
    + RS_B + b'{"bad": "\\uXYZW"}\n'
    + RS_B + b'1.\n'
    + RS_B + b'{"ok": true}\n'
)
KEPT = [{'a': 1}, {'ok': True}]
RS_OFFSETS = [i for i, byte in enumerate(STREAM) if byte == 0x1e]
BAD_RANGES = [
    (2, RS_OFFSETS[1], RS_OFFSETS[2]),
    (3, RS_OFFSETS[2], RS_OFFSETS[3]),
]


def _chunk(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def _collect(source, **kwargs):
    bad = []
    values = list(sequences.items(source, on_bad_record=bad.append, **kwargs))
    return values, bad


def test_whole_stream_recovery():
    values, bad = _collect(STREAM)
    assert values == KEPT
    assert [(b.number, b.start, b.end) for b in bad] == BAD_RANGES
    assert '\\uXXXX escape' in bad[0].reason
    assert bad[1].reason == 'Invalid JSON number: 1.'


@pytest.mark.parametrize('chunk_size', list(range(1, len(STREAM) + 1)))
def test_every_chunk_size_recovers_identically(chunk_size):
    values, bad = _collect(iter(_chunk(STREAM, chunk_size)))
    assert values == KEPT
    assert [(b.number, b.start, b.end, b.reason) for b in bad] == [
        (b.number, b.start, b.end, b.reason)
        for b in _collect(STREAM)[1]
    ]


def test_one_byte_file_source():
    values, bad = _collect(io.BytesIO(STREAM), buf_size=1)
    assert values == KEPT
    assert [(b.number, b.start, b.end) for b in bad] == BAD_RANGES


def test_arbitrary_chunkings_are_stable():
    rng = random.Random(0)
    for _ in range(50):
        chunks = []
        pos = 0
        while pos < len(STREAM):
            step = rng.randint(1, 7)
            chunks.append(STREAM[pos:pos + step])
            pos += step
        values, bad = _collect(iter(chunks))
        assert values == KEPT
        assert [(b.number, b.start, b.end) for b in bad] == BAD_RANGES


def test_strict_mode_raises_on_bad_record():
    with pytest.raises(InvalidRecordError) as exc:
        list(sequences.items(STREAM))
    err = exc.value
    assert err.record_number == 2
    assert (err.start, err.end) == (RS_OFFSETS[1], RS_OFFSETS[2])
    assert '\\uXXXX escape' in err.reason


def test_skip_is_silent():
    values = list(sequences.items(STREAM, on_bad_record=SKIP))
    assert values == KEPT


def test_on_bad_record_receives_namedtuple():
    seen = []
    values = list(sequences.items(STREAM, on_bad_record=seen.append))
    assert values == KEPT
    assert all(isinstance(b, BadRecord) for b in seen)
    assert seen[0].number == 2


def test_legal_newlines_inside_record():
    stream = (
        RS_B + b'{\n  "k": "a\\nb",\n  "n": [1,\n 2]\n}\n'
        + RS_B + b'  [true, null]  \n'
    )
    assert list(sequences.items(stream)) == [
        {'k': 'a\nb', 'n': [1, 2]},
        [True, None],
    ]


def test_raw_lf_in_string_does_not_split_record():
    # The raw LF is invalid JSON, but it must not be treated as a record
    # boundary: resync only happens at the following RS byte.
    stream = RS_B + b'"raw\nlf"\n' + RS_B + b'{"after": 1}\n'
    expected_values = [{'after': 1}]
    expected_range = [(1, 0, 10)]
    values, bad = _collect(stream)
    assert values == expected_values
    assert [(b.number, b.start, b.end) for b in bad] == expected_range
    for size in range(1, len(stream) + 1):
        values, bad = _collect(iter(_chunk(stream, size)))
        assert values == expected_values
        assert [(b.number, b.start, b.end) for b in bad] == expected_range


def test_utf8_spanning_chunks():
    stream = RS_B + '{"\u00e9": "\u4e2d\u6587"}'.encode('utf-8') + b'\n'
    expected = [{'\u00e9': '\u4e2d\u6587'}]
    for size in range(1, len(stream) + 1):
        chunks = iter(_chunk(stream, size))
        assert list(sequences.items(chunks)) == expected


def test_invalid_utf8_recovers_at_next_record():
    stream = RS_B + b'{"\xc3".\n' + RS_B + b'{"fine": 1}\n'
    expected = [{'fine': 1}]
    for size in range(1, len(stream) + 1):
        values, bad = _collect(iter(_chunk(stream, size)))
        assert values == expected
        assert [(b.number, b.start, b.end) for b in bad] == [(1, 0, 7)]


def test_top_level_scalars_require_termination():
    stream = (
        RS_B + b'1 2\n'
        + RS_B + b'nullx\n'
        + RS_B + b'true extra\n'
        + RS_B + b'42\n'
        + RS_B + b'"abc"\n'
        + RS_B + b'0.5\n'
    )
    values, bad = _collect(stream)
    assert values == [42, 'abc', Decimal('0.5')]
    assert [b.number for b in bad] == [1, 2, 3]


def test_blank_records_and_preamble_are_ignored():
    stream = (
        b'garbage before the first RS'
        + RS_B + b'\n'
        + RS_B + b'  \t \n'
        + RS_B + b'{"z": null}\n'
    )
    values, bad = _collect(stream)
    assert values == [{'z': None}]
    assert bad == []


def test_unterminated_final_record_is_bad():
    stream = RS_B + b'{"a": 1}\n' + RS_B + b'[1'
    values, bad = _collect(stream)
    assert values == [{'a': 1}]
    assert bad[0].number == 2
    assert bad[0].start == 10
    assert bad[0].end == len(stream)
    assert 'terminated' in bad[0].reason


def test_final_lf_terminated_record_is_complete():
    stream = RS_B + b'{"a": 1}\n'
    assert list(sequences.items(stream)) == [{'a': 1}]


def test_max_record_size_enforced_on_bytes():
    big_payload = b'{"x": "' + b'a' * 100 + b'"}\n'
    stream = RS_B + big_payload + RS_B + b'{"y": 1}\n'
    values, bad = _collect(stream, max_record_size=50)
    assert values == [{'y': 1}]
    assert len(bad) == 1
    assert (bad[0].number, bad[0].start, bad[0].end) == (1, 0, 111)
    assert '50' in bad[0].reason


def test_max_record_size_exact_fit_passes():
    payload = b'{"y": 1}\n'
    stream = RS_B + payload + RS_B + b'1\n'
    assert list(sequences.items(stream, max_record_size=len(payload))) == [
        {'y': 1}, 1
    ]


class _PacedReader:
    '''File that serves one requested byte at a time.'''

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, n):
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


def test_max_record_size_enforced_before_reading_rest():
    big_payload = b'{"x": "' + b'a' * 100 + b'"}\n'
    stream = RS_B + big_payload + RS_B + b'{"y": 1}\n'
    reader = _PacedReader(stream)
    values, bad = _collect(reader, buf_size=1, max_record_size=50)
    assert values == [{'y': 1}]
    assert [(b.number, b.start, b.end) for b in bad] == [(1, 0, 111)]
    # The stream is consumed up to the next record, but the oversized payload
    # itself is never buffered for parsing.
    assert reader.pos == len(stream)


def test_bad_record_bytes_never_parse_as_next_json():
    stream = RS_B + b'1.\n' + RS_B + b'2\n'
    values, bad = _collect(stream)
    assert values == [2]
    assert [(b.number, b.start, b.end) for b in bad] == [(1, 0, 4)]


def test_invalid_on_bad_record_value():
    with pytest.raises(ValueError):
        list(sequences.items(RS_B + b'{}\n', on_bad_record='bogus'))


def test_bytes_and_str_sources():
    stream = RS_B + b'{"a": 1}\n'
    assert list(sequences.items(stream)) == [{'a': 1}]
    assert list(sequences.items(stream.decode('utf-8'))) == [{'a': 1}]


def test_async_source_rejected():
    class AsyncSource:
        async def read(self, n):
            return b''

    with pytest.raises(ValueError):
        list(sequences.items(AsyncSource()))
