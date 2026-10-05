"""Tests for ijson.validation, the event-level schema validator"""

import pytest

from ijson.backends.python import basic_parse
from ijson.utils import chain, sendable_list
from ijson.validation import (
    EventValidator,
    SchemaError,
    ValidationAbortedError,
    ValidationError,
    validator_coro,
)


class SmallChunkReader(object):
    """
    A file-like object that only serves data in small chunks, no matter how
    much the parser asks for, and records how many bytes were delivered.
    """
    def __init__(self, data, chunk_size=8):
        self.data = data
        self.chunk_size = chunk_size
        self.pos = 0
        self.bytes_read = 0

    def read(self, size=-1):
        if size is None or size < 0 or size > self.chunk_size:
            size = self.chunk_size
        chunk = self.data[self.pos:self.pos + size]
        self.pos += len(chunk)
        self.bytes_read += len(chunk)
        return chunk


def validate(data, schema, **config):
    validator = EventValidator(schema, **config)
    for event, value in basic_parse(data):
        validator.event(event, value)
    return validator


def test_valid_document_passes():
    schema = {
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'minimum': 0},
                'name': {'type': 'string'},
            },
            'required': ['id'],
            'additionalProperties': False,
        },
    }
    validate(b'[{"id": 1, "name": "a"}, {"id": 2}]', schema)


# -- schema subset enforcement (before any input is read) -------------------

@pytest.mark.parametrize('schema', [
    {'pattern': '^a'},
    {'type': 'object', 'properties': {'x': {'format': 'email'}}},
    {'type': 'array', 'items': {'allOf': [{'type': 'number'}]}},
    {'type': 'array', 'items': [{'type': 'number'}]},  # tuple form of items
    {'type': 'object', 'additionalProperties': {'enum': [1, 2]}},
    {'type': 'unknown'},
    {'type': []},
    {'required': 'id'},
    {'minItems': -1},
    {'uniqueItems': 1},
    {'exclusiveMinimum': True},
    True,
])
def test_unsupported_schema_rejected_before_reading(schema):
    with pytest.raises(SchemaError):
        EventValidator(schema)


def test_supported_schema_is_accepted():
    EventValidator({
        'type': 'object',
        'properties': {'x': {'type': ['number', 'null'], 'exclusiveMaximum': 5}},
        'required': ['x'],
        'additionalProperties': {'type': 'string'},
    })


# -- early errors on streaming input -----------------------------------------

def test_early_type_error_does_not_consume_whole_input():
    # A huge array that only a streaming reader would refuse to buffer
    data = b'[' + b'1,' * 100000 + b'1]'
    reader = SmallChunkReader(data, chunk_size=8)
    validator = EventValidator({'type': 'object'})
    with pytest.raises(ValidationError) as excinfo:
        for event, value in basic_parse(reader):
            validator.event(event, value)
    assert excinfo.value.rule == 'type'
    assert excinfo.value.path == '$'
    # the error surfaces after the first events; most of the input is unread
    assert reader.bytes_read < 1024
    assert reader.bytes_read < len(data)


def test_deep_error_reports_structural_path():
    schema = {
        'type': 'object',
        'properties': {
            'items': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {'name': {'type': 'string'}},
                },
            },
        },
    }
    with pytest.raises(ValidationError) as excinfo:
        validate(b'{"items": [{"name": "ok"}, {"name": 1}]}', schema)
    assert excinfo.value.path == '$.items[1].name'
    assert excinfo.value.rule == 'type'


# -- individual rules ----------------------------------------------------------

@pytest.mark.parametrize('data, schema, path, rule', [
    (b'1.5', {'type': 'integer'}, '$', 'type'),
    (b'"x"', {'type': ['number', 'null']}, '$', 'type'),
    (b'0', {'minimum': 1}, '$', 'minimum'),
    (b'10', {'maximum': 1}, '$', 'maximum'),
    (b'5', {'exclusiveMinimum': 5}, '$', 'exclusiveMinimum'),
    (b'5', {'exclusiveMaximum': 5}, '$', 'exclusiveMaximum'),
    (b'[]', {'type': 'array', 'minItems': 1}, '$', 'minItems'),
    (b'[1, 2, 3]', {'type': 'array', 'maxItems': 2}, '$', 'maxItems'),
    (b'{"a": 1}', {'type': 'object', 'required': ['a', 'b']}, '$', 'required'),
    (b'{"a": 1, "b": 2}',
     {'type': 'object', 'properties': {'a': {}}, 'additionalProperties': False},
     '$.b', 'additionalProperties'),
    (b'{"a": "x"}',
     {'type': 'object', 'additionalProperties': {'type': 'number'}},
     '$.a', 'type'),
])
def test_rule_violations(data, schema, path, rule):
    with pytest.raises(ValidationError) as excinfo:
        validate(data, schema)
    assert excinfo.value.path == path
    assert excinfo.value.rule == rule


def test_integer_type_accepts_integral_floats():
    validate(b'1.0', {'type': 'integer'})
    validate(b'1', {'type': 'integer'})


def test_required_checked_when_object_closes():
    validator = EventValidator({'type': 'object', 'required': ['a', 'b']})
    validator.event('start_map', None)
    validator.event('map_key', 'a')
    validator.event('number', 1)
    # no error while the object is still open
    with pytest.raises(ValidationError) as excinfo:
        validator.event('end_map', None)
    assert excinfo.value.rule == 'required'
    assert 'b' in str(excinfo.value)


# -- uniqueItems ----------------------------------------------------------------

@pytest.mark.parametrize('data', [
    b'[1, 1.0]',              # 1 and 1.0 are the same JSON number
    b'[1, 1.00e0]',
    b'["a", "a"]',
    b'[[1, 2], [1, 2]]',
    b'[{"a": 1, "b": 2}, {"b": 2, "a": 1}]',  # key order is irrelevant
    # duplicates nested inside items, with different key order at both levels
    b'[[{"a": 1, "b": {"x": 1, "y": 2}}], [{"b": {"y": 2, "x": 1}, "a": 1}]]',
])
def test_unique_items_detects_duplicates_by_json_value(data):
    with pytest.raises(ValidationError) as excinfo:
        validate(data, {'type': 'array', 'uniqueItems': True})
    assert excinfo.value.rule == 'uniqueItems'


@pytest.mark.parametrize('data', [
    b'[true, 1]',       # true is not the number 1
    b'[false, 0]',
    b'[1, "1"]',
    b'[null, 0]',
    b'[{"a": 1, "b": 2}, {"a": 1, "b": 3}]',
    b'[[1, 2], [2, 1]]',
])
def test_unique_items_allows_distinct_values(data):
    validate(data, {'type': 'array', 'uniqueItems': True})


def test_unique_items_memory_budget_aborts_validation():
    data = b'["' + b'a' * 100 + b'", "' + b'b' * 100 + b'"]'
    schema = {'type': 'array', 'uniqueItems': True}
    # fits in a generous budget
    validate(data, schema)
    # a tight budget aborts instead of silently disabling the check
    with pytest.raises(ValidationAbortedError) as excinfo:
        validate(data, schema, unique_memory_budget=100)
    assert excinfo.value.rule == 'uniqueItems'
    assert isinstance(excinfo.value, ValidationError)
    assert 'cannot continue' in str(excinfo.value)


# -- coroutine integration ------------------------------------------------------

def test_validator_coro_forwards_events_after_validating():
    events = sendable_list()
    coro = chain(events, (validator_coro, ({'type': 'array', 'maxItems': 2},), {}))
    for event, value in basic_parse(b'[1, 2]'):
        coro.send((event, value))
    assert list(basic_parse(b'[1, 2]')) == events


def test_validator_coro_raises_on_invalid_events():
    coro = chain(sendable_list(), (validator_coro, ({'type': 'object'},), {}))
    with pytest.raises(ValidationError):
        coro.send(('start_array', None))
