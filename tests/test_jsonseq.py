import io

import pytest

import ijson
from ijson.jsonseq import InvalidRecordError, RecordTooLargeError, RS


CORRUPTED_STREAM = (
    RS + b'{"bad": "\x5cx"}\n'
    + RS + b'12e\n'
    + RS + b'{"ok": true}\n'
)


def chunked(data, size):
    for index in range(0, len(data), size):
        yield data[index:index + size]


def parse(data, chunk_size=None, **kwargs):
    errors = []
    if chunk_size is None:
        source = data
    else:
        source = chunked(data, chunk_size)
    values = list(ijson.json_sequence(
        source, on_bad_record=errors.append, **kwargs
    ))
    return values, errors


@pytest.mark.parametrize('chunk_size', [1, 2, 3, 4, 7, 16, 64])
def test_recovery_position(backend, chunk_size):
    values, errors = parse(
        CORRUPTED_STREAM, chunk_size, backend=backend.backend_name
    )

    assert values == [{'ok': True}]
    assert [(e.record_number, e.start, e.end) for e in errors] == [
        (1, 1, 15),
        (2, 16, 20),
    ]
    assert 'Invalid \\escape' in errors[0].reason
    assert '12e' in errors[1].reason


def test_recovery_position_bytes_and_iterable_match(backend):
    expected_values, expected_errors = parse(
        CORRUPTED_STREAM, backend=backend.backend_name
    )

    for chunk_size in range(1, len(CORRUPTED_STREAM) + 1):
        values, errors = parse(
            CORRUPTED_STREAM, chunk_size, backend=backend.backend_name
        )
        assert values == expected_values
        assert [(e.record_number, e.start, e.end, str(e.reason))
                for e in errors] == [
                    (e.record_number, e.start, e.end, str(e.reason))
                    for e in expected_errors
                ]


def test_recovery_position_file_read_sizes_match(backend):
    expected_values, expected_errors = parse(
        CORRUPTED_STREAM, backend=backend.backend_name
    )

    for buf_size in (1, 2, 3, 7, 64):
        errors = []
        values = list(ijson.json_sequence(
            io.BytesIO(CORRUPTED_STREAM),
            buf_size=buf_size,
            on_bad_record=errors.append,
            backend=backend.backend_name,
        ))
        assert values == expected_values
        assert [(e.record_number, e.start, e.end, str(e.reason))
                for e in errors] == [
                    (e.record_number, e.start, e.end, str(e.reason))
                    for e in expected_errors
                ]


def test_strict_termination_stops_at_bad_record(backend):
    with pytest.raises(InvalidRecordError) as exc_info:
        list(ijson.json_sequence(
            CORRUPTED_STREAM, backend=backend.backend_name
        ))

    error = exc_info.value
    assert error.record_number == 1
    assert (error.start, error.end) == (1, 15)
    assert 'Invalid \\escape' in str(error)


def test_backend_module_exposes_sequence_entry_point(backend):
    assert list(backend.json_sequence(
        RS + b'{"ok": true}\n',
        backend=backend.backend_name,
    )) == [{'ok': True}]


def test_skip_bad_records_without_callback(backend):
    assert list(ijson.json_sequence(
        CORRUPTED_STREAM,
        backend=backend.backend_name,
        skip_bad_records=True,
    )) == [{'ok': True}]


def test_callback_and_strict_skip_option_are_mutually_exclusive(backend):
    with pytest.raises(ValueError):
        list(ijson.json_sequence(
            CORRUPTED_STREAM,
            backend=backend.backend_name,
            on_bad_record=lambda error: None,
            skip_bad_records=False,
        ))


def test_legal_newlines_inside_record(backend):
    stream = RS + b'{\n  "a": 1,\n  "b": 2\n}\n' + RS + b'true\n'

    for chunk_size in (1, 3, 64):
        values, errors = parse(
            stream, chunk_size, backend=backend.backend_name
        )
        assert values == [{'a': 1, 'b': 2}, True]
        assert errors == []


def test_utf8_is_handled_across_chunks(backend):
    stream = (
        RS + b'{"text": "\xc3\xa9", "n": 1}\n'
        + RS + b'null\n'
    )

    for chunk_size in (1, 2, 3, 64):
        values, errors = parse(
            stream, chunk_size, backend=backend.backend_name
        )
        assert values == [{'text': 'é', 'n': 1}, None]
        assert errors == []


def test_invalid_utf8_does_not_bleed_into_next_record(backend):
    stream = RS + b'"\xa8"\n' + RS + b'{"ok": true}\n'

    for chunk_size in (1, 2, 64):
        values, errors = parse(
            stream, chunk_size, backend=backend.backend_name
        )
        assert values == [{'ok': True}]
        assert len(errors) == 1
        assert errors[0].record_number == 1
        assert (errors[0].start, errors[0].end) == (1, 5)


def test_missing_initial_rs_reports_source_range(backend):
    stream = b'garbage' + RS + b'{"ok": true}\n'

    values, errors = parse(stream, 2, backend=backend.backend_name)
    assert values == [{'ok': True}]
    assert len(errors) == 1
    assert errors[0].record_number == 1
    assert (errors[0].start, errors[0].end) == (0, 7)


def test_strict_missing_initial_rs_raises(backend):
    with pytest.raises(InvalidRecordError) as exc_info:
        list(ijson.json_sequence(b'garbage', backend=backend.backend_name))

    error = exc_info.value
    assert error.record_number == 1
    assert (error.start, error.end) == (0, 7)


def test_empty_records_are_skipped(backend):
    stream = RS + RS + b'{"ok": true}\n' + RS

    values, errors = parse(stream, 1, backend=backend.backend_name)
    assert values == [{'ok': True}]
    assert [(e.record_number, e.start, e.end) for e in errors] == [
        (1, 2, 2),
        (3, 16, 16),
    ]


def test_top_level_scalars_terminate_at_record_boundary(backend):
    stream = RS + b'42\n' + RS + b'"text"\n' + RS + b'[1, 2]\n'

    for chunk_size in (1, 2, 64):
        values, errors = parse(
            stream, chunk_size, backend=backend.backend_name
        )
        assert values == [42, 'text', [1, 2]]
        assert errors == []


def test_rs_terminates_records_without_line_feed(backend):
    stream = RS + b'42' + RS + b'{"ok": true}' + RS + b'true'

    for chunk_size in (1, 2, 64):
        values, errors = parse(
            stream, chunk_size, backend=backend.backend_name
        )
        assert values == [42, {'ok': True}, True]
        assert errors == []


def test_number_followed_by_more_data_in_record_is_invalid(backend):
    stream = RS + b'1 2\n' + RS + b'{"ok": true}\n'

    values, errors = parse(stream, 1, backend=backend.backend_name)
    assert values == [{'ok': True}]
    assert len(errors) == 1
    assert errors[0].record_number == 1
    assert (errors[0].start, errors[0].end) == (1, 5)


def test_max_record_size_applies_before_more_reads(backend):

    class TrackingReader:
        def __init__(self):
            self.buffer = io.BytesIO(RS + b'123456')
            self.read_sizes = []

        def read(self, size):
            self.read_sizes.append(size)
            return self.buffer.read(size)

    reader = TrackingReader()
    with pytest.raises(RecordTooLargeError) as exc_info:
        list(ijson.json_sequence(
            reader, buf_size=1, max_record_size=5,
            backend=backend.backend_name,
        ))

    error = exc_info.value
    assert (error.record_number, error.start, error.end) == (1, 1, 7)
    assert error.max_record_size == 5
    assert reader.read_sizes == [0, 1, 1, 1, 1, 1, 1, 1]


def test_max_record_size_is_always_fatal(backend):
    stream = RS + b'123456' + RS + b'{"ok": true}\n'

    with pytest.raises(RecordTooLargeError):
        list(ijson.json_sequence(
            io.BytesIO(stream),
            max_record_size=5,
            backend=backend.backend_name,
            on_bad_record=lambda error: pytest.fail(
                'oversized records cannot be skipped'
            ),
        ))


def test_oversized_preamble_is_always_fatal(backend):
    stream = b'123456' + RS + b'{"ok": true}\n'

    with pytest.raises(RecordTooLargeError):
        list(ijson.json_sequence(
            io.BytesIO(stream),
            max_record_size=5,
            backend=backend.backend_name,
            on_bad_record=lambda error: pytest.fail(
                'oversized preamble cannot be skipped'
            ),
        ))


def test_exact_max_record_size_is_allowed(backend):
    stream = RS + b'12345'

    assert list(ijson.json_sequence(
        io.BytesIO(stream), buf_size=1, max_record_size=5,
        backend=backend.backend_name,
    )) == [12345]


def test_async_recovery_position(backend):
    async def chunks(size):
        for index in range(0, len(CORRUPTED_STREAM), size):
            yield CORRUPTED_STREAM[index:index + size]

    async def run(size, backend_name):
        errors = []
        values = []
        async for value in ijson.json_sequence_async(
            chunks(size), on_bad_record=errors.append, backend=backend_name
        ):
            values.append(value)
        return values, errors

    import asyncio

    for size in (1, 4, 64):
        values, errors = asyncio.run(run(size, backend.backend_name))
        assert values == [{'ok': True}]
        assert [(e.record_number, e.start, e.end) for e in errors] == [
            (1, 1, 15),
            (2, 16, 20),
        ]
