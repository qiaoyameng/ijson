'''
Tests for the python backend's optional ``number_factory`` option.
'''
import io
from decimal import Decimal

import pytest

import ijson
from ijson import common, utils

from .support.async_ import AsyncReader
from .support._async_common import _aiorun


def _skip_if_unsupported(backend):
    if not backend.capabilities.number_factory:
        pytest.skip("backend does not support number_factory")


def _recording_factory():
    calls = []

    def factory(path, raw):
        # The path always arrives as an immutable tuple
        assert isinstance(path, tuple)
        calls.append((path, raw))
        return common.integer_or_decimal(raw)

    return factory, calls


def test_capability_flag(backend):
    _skip_if_unsupported(backend)
    assert backend.capabilities.number_factory is True


def test_number_factory_called_once_per_number_across_chunks(backend):
    # A number split across many chunks triggers a single callback
    _skip_if_unsupported(backend)
    factory, calls = _recording_factory()
    chunks = (b'1', b'e', b'3')
    reader = ijson.from_iter(iter(chunks))
    events = list(backend.basic_parse(reader, number_factory=factory))
    assert events == [('number', Decimal('1e3'))]
    assert calls == [((), '1e3')]


def test_number_factory_receives_raw_literal(backend):
    # Integer, fraction and exponent spellings are preserved verbatim
    _skip_if_unsupported(backend)
    factory, calls = _recording_factory()
    json = b'[0, -0, 1.0, 1E+2, -1.5e-3, 10000000000]'
    list(backend.basic_parse(io.BytesIO(json), number_factory=factory))
    assert [raw for _, raw in calls] == [
        '0', '-0', '1.0', '1E+2', '-1.5e-3', '10000000000'
    ]
    assert all(path == (index,) for index, (path, _) in enumerate(calls))


def test_booleans_and_null_never_reach_the_factory(backend):
    _skip_if_unsupported(backend)
    factory, calls = _recording_factory()
    json = b'{"t": true, "f": false, "n": null, "x": 1}'
    events = list(backend.basic_parse(io.BytesIO(json), number_factory=factory))
    assert ('boolean', True) in events
    assert ('boolean', False) in events
    assert ('null', None) in events
    assert calls == [(('x',), '1')]


def test_path_contains_keys_and_array_indices(backend):
    _skip_if_unsupported(backend)
    factory, calls = _recording_factory()
    json = b'{"docs": [{"id": 1}, {"id": 2}], "matrix": [[10, 20]], "root": 3}'
    list(backend.basic_parse(io.BytesIO(json), number_factory=factory))
    assert calls == [
        (('docs', 0, 'id'), '1'),
        (('docs', 1, 'id'), '2'),
        (('matrix', 0, 0), '10'),
        (('matrix', 0, 1), '20'),
        (('root',), '3'),
    ]


def test_paths_are_immutable_and_independent(backend):
    _skip_if_unsupported(backend)
    seen = []

    def factory(path, raw):
        seen.append(path)
        with pytest.raises(TypeError):
            path[0] = 'mutated'
        return int(raw)

    list(backend.basic_parse(io.BytesIO(b'[1, 2, 3]'), number_factory=factory))
    assert seen == [(0,), (1,), (2,)]


def test_amount_decimal_count_integer_sensor_float(backend):
    # Domain scenario: amounts are Decimal, counts strict int, sensors float
    _skip_if_unsupported(backend)

    class CountMustBeInteger(TypeError):
        pass

    def factory(path, raw):
        if path and path[0] == 'amounts':
            return Decimal(raw)
        if path and path[0] == 'counts':
            if '.' in raw or 'e' in raw or 'E' in raw:
                raise CountMustBeInteger('counts must be integers: %s' % raw)
            return int(raw)
        if '.' in raw or 'e' in raw or 'E' in raw:
            return float(raw)
        return int(raw)

    json = b'{"amounts": [12.50, 0.1], "counts": [3, 7], "sensors": [1.5, 2e3]}'
    result = list(backend.items(io.BytesIO(json), '', number_factory=factory))
    assert result == [{
        'amounts': [Decimal('12.50'), Decimal('0.1')],
        'counts': [3, 7],
        'sensors': [1.5, 2000.0],
    }]
    assert all(isinstance(value, int) for value in result[0]['counts'])
    assert all(isinstance(value, float) for value in result[0]['sensors'])

    with pytest.raises(common.NumberFactoryError) as exc_info:
        list(backend.items(
            io.BytesIO(b'{"counts": [1.0]}'),
            '',
            number_factory=factory,
        ))
    error = exc_info.value
    assert error.path == ('counts', 0)
    assert error.number == '1.0'
    assert isinstance(error.__cause__, CountMustBeInteger)


def test_factory_error_is_not_a_json_syntax_error(backend):
    _skip_if_unsupported(backend)

    def factory(path, raw):
        raise RuntimeError('boom')

    with pytest.raises(common.NumberFactoryError) as exc_info:
        list(backend.basic_parse(
            io.BytesIO(b'{"a": {"b": [42]}}'),
            number_factory=factory,
        ))
    error = exc_info.value
    assert not isinstance(error, common.JSONError)
    assert error.path == ('a', 'b', 0)
    assert error.number == '42'
    assert isinstance(error.__cause__, RuntimeError)
    assert str(error.__cause__) == 'boom'


def test_invalid_numbers_never_reach_the_factory(backend):
    _skip_if_unsupported(backend)
    factory, calls = _recording_factory()
    invalid_jsons = (
        b'1e', b'0.1e', b'0E', b'1.', b'.1', b'-.1', b'+1', b'01', b'1.e5'
    )
    for invalid in invalid_jsons:
        with pytest.raises(common.JSONError):
            list(backend.basic_parse(io.BytesIO(invalid), number_factory=factory))
    assert calls == []


def test_number_factory_and_use_float_are_mutually_exclusive(backend):
    _skip_if_unsupported(backend)
    factory, _ = _recording_factory()
    with pytest.raises(ValueError, match='mutually exclusive'):
        list(backend.basic_parse(
            io.BytesIO(b'1'),
            use_float=True,
            number_factory=factory,
        ))


def test_number_factory_must_be_callable(backend):
    _skip_if_unsupported(backend)
    with pytest.raises(ValueError, match='callable'):
        list(backend.basic_parse(io.BytesIO(b'1'), number_factory=42))


def test_default_behavior_without_factory_is_unchanged(backend):
    _skip_if_unsupported(backend)
    events = list(backend.basic_parse(io.BytesIO(b'[1, 1.0, 1E2]')))
    assert events == [
        ('start_array', None),
        ('number', 1),
        ('number', Decimal('1.0')),
        ('number', Decimal('1E2')),
        ('end_array', None),
    ]


def test_parse_coro_number_factory(backend):
    _skip_if_unsupported(backend)
    sink = utils.sendable_list()
    coro = backend.parse_coro(sink, number_factory=lambda path, raw: Decimal(raw))
    coro.send(b'{"a": 1.5}')
    coro.close()
    assert ('a', 'number', Decimal('1.5')) in sink


def test_items_coro_number_factory(backend):
    _skip_if_unsupported(backend)
    sink = utils.sendable_list()
    coro = backend.items_coro(sink, 'item', number_factory=lambda path, raw: int(raw))
    coro.send(b'{"item": {"n": 5}}')
    coro.close()
    assert sink == [{'n': 5}]


def test_kvitems_coro_number_factory(backend):
    _skip_if_unsupported(backend)
    sink = utils.sendable_list()
    coro = backend.kvitems_coro(sink, '', number_factory=lambda path, raw: Decimal(raw))
    coro.send(b'{"a": 1.5}')
    coro.close()
    assert sink == [('a', Decimal('1.5'))]


def test_async_entries_number_factory(backend):
    _skip_if_unsupported(backend)

    def factory(path, raw):
        return Decimal(raw) if '.' in raw else int(raw)

    json = b'{"items": [1, 2.5]}'

    async def run():
        basic = [event async for event in
                 backend.basic_parse_async(AsyncReader(json), number_factory=factory)]
        assert ('number', Decimal('2.5')) in basic
        items = [value async for value in
                 backend.items_async(AsyncReader(json), 'items', number_factory=factory)]
        assert items == [[1, Decimal('2.5')]]
        kvitems = [pair async for pair in
                   backend.kvitems_async(AsyncReader(json), '', number_factory=factory)]
        assert kvitems == [('items', [1, Decimal('2.5')])]
        parsed = [event async for event in
                  backend.parse_async(AsyncReader(json), number_factory=factory)]
        assert ('items.item', 'number', Decimal('2.5')) in parsed

    _aiorun(run())


def test_multiple_values_paths_reset(backend):
    _skip_if_unsupported(backend)
    factory, calls = _recording_factory()
    list(backend.basic_parse(
        io.BytesIO(b'{"a": 1} {"b": 2}'),
        multiple_values=True,
        number_factory=factory,
    ))
    assert calls == [(('a',), '1'), (('b',), '2')]
