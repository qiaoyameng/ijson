# coding: utf-8
'''
Tests for ijson.validation: event-level schema validation.
'''
import json

import pytest

import ijson
from ijson import utils
from ijson.validation import (
    EventValidator,
    UnsupportedSchemaError,
    ValidationBudgetExceeded,
    ValidationError,
    validate_events,
    validator_basecoro,
)

backend = ijson.get_backend('python')


class ChunkedReader(object):
    '''
    A file-like object that only serves small chunks and remembers how many
    bytes were consumed, to prove that validation errors surface before the
    whole input is read.
    '''
    def __init__(self, data, chunk_size=7):
        self._data = data
        self._chunk_size = chunk_size
        self._pos = 0
        self.bytes_read = 0

    def read(self, size=-1):
        if size < 0:
            size = self._chunk_size
        size = min(size, self._chunk_size, len(self._data) - self._pos)
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        self.bytes_read += len(chunk)
        return chunk


def parse_events(reader):
    return backend.parse(reader)


def validate(reader, schema, **kwargs):
    return list(validate_events(parse_events(reader), schema, **kwargs))


PERSON_SCHEMA = {
    'type': 'object',
    'properties': {
        'name': {'type': 'string'},
        'age': {'type': 'integer', 'minimum': 0, 'maximum': 150},
        'tags': {
            'type': 'array',
            'items': {'type': 'string'},
            'minItems': 1,
            'maxItems': 4,
            'uniqueItems': True,
        },
    },
    'required': ['name'],
    'additionalProperties': False,
}


def test_valid_document_passes_events_through():
    document = {'name': 'Alice', 'age': 30, 'tags': ['a', 'b']}
    raw = json.dumps(document).encode('utf-8')
    expected = list(backend.parse(ChunkedReader(raw)))
    assert validate(ChunkedReader(raw), PERSON_SCHEMA) == expected


def test_scalar_root_validated():
    assert validate(ChunkedReader(b'42'), {'type': 'integer'})
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'"oops"'), {'type': 'integer'})
    assert exc_info.value.rule == 'type'
    assert exc_info.value.path == ''


def test_type_error_stops_reading_early():
    # The type error is near the start of a large document; the validator
    # must raise before the whole input is consumed.
    document = (
        b'{"name": 123, "padding": "' + b'x' * 10000 + b'"}'
    )
    reader = ChunkedReader(document)
    with pytest.raises(ValidationError) as exc_info:
        validate(reader, PERSON_SCHEMA)
    assert exc_info.value.rule == 'type'
    assert exc_info.value.path == 'name'
    assert reader.bytes_read < len(document)


def test_unsupported_keyword_rejected_before_reading():
    reader = ChunkedReader(b'{"name": "Alice"}')
    schema = dict(PERSON_SCHEMA, properties={
        'name': {'type': 'string', 'pattern': '^[a-z]+$'},
    })
    with pytest.raises(UnsupportedSchemaError):
        validate_events(parse_events(reader), schema)
    assert reader.bytes_read == 0


@pytest.mark.parametrize('schema', [
    {'type': 'object', 'format': 'email'},
    {'type': 'object', 'properties': {'a': {'items': [{'type': 'string'}]}}},
    {'type': 'array', 'items': {'$ref': '#/definitions/x'}},
    {'type': 'object', 'additionalProperties': {'enum': [1, 2]}},
    {'type': 'falcon'},
])
def test_unsupported_constructs_rejected_at_construction(schema):
    with pytest.raises(UnsupportedSchemaError):
        EventValidator(schema)


def test_required_checked_when_object_closes():
    reader = ChunkedReader(b'{"age": 30}')
    with pytest.raises(ValidationError) as exc_info:
        validate(reader, PERSON_SCHEMA)
    assert exc_info.value.rule == 'required'
    assert exc_info.value.path == ''
    assert 'name' in str(exc_info.value)
    # The error is raised at the closing brace of the object.
    assert reader.bytes_read == len(b'{"age": 30}')


def test_required_nested_path():
    schema = {
        'type': 'object',
        'properties': {
            'item': {'type': 'object', 'required': ['a', 'b']},
        },
    }
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"item": {"a": 1}}'), schema)
    assert exc_info.value.rule == 'required'
    assert exc_info.value.path == 'item'


def test_additional_properties_rejected():
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"name": "A", "extra": 1}'), PERSON_SCHEMA)
    assert exc_info.value.rule == 'additionalProperties'
    assert exc_info.value.path == 'extra'


def test_additional_properties_schema_applied():
    schema = {
        'type': 'object',
        'properties': {'name': {'type': 'string'}},
        'additionalProperties': {'type': 'integer'},
    }
    assert validate(ChunkedReader(b'{"name": "A", "x": 1}'), schema)
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"name": "A", "x": "no"}'), schema)
    assert exc_info.value.rule == 'type'
    assert exc_info.value.path == 'x'


def test_items_type_and_path():
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"name": "A", "tags": ["ok", 5]}'),
                 PERSON_SCHEMA)
    assert exc_info.value.rule == 'type'
    assert exc_info.value.path == 'tags.item'


def test_array_length_bounds():
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"name": "A", "tags": []}'), PERSON_SCHEMA)
    assert exc_info.value.rule == 'minItems'
    assert exc_info.value.path == 'tags'
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(
            b'{"name": "A", "tags": ["a", "b", "c", "d", "e"]}'),
            PERSON_SCHEMA)
    assert exc_info.value.rule == 'maxItems'
    assert exc_info.value.path == 'tags'


def test_number_range():
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"name": "A", "age": -1}'), PERSON_SCHEMA)
    assert exc_info.value.rule == 'minimum'
    assert exc_info.value.path == 'age'
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'{"name": "A", "age": 200}'), PERSON_SCHEMA)
    assert exc_info.value.rule == 'maximum'
    assert exc_info.value.path == 'age'


def test_boolean_is_not_a_number():
    schema = {'type': 'array', 'items': {'type': 'number'}}
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'[1, true]'), schema)
    assert exc_info.value.rule == 'type'


def test_integer_type_accepts_integral_decimals():
    assert validate(ChunkedReader(b'[1, 2.0]'),
                    {'type': 'array', 'items': {'type': 'integer'}})
    with pytest.raises(ValidationError):
        validate(ChunkedReader(b'[1.5]'),
                 {'type': 'array', 'items': {'type': 'integer'}})


def test_unique_items_number_semantics():
    schema = {'type': 'array', 'uniqueItems': True}
    # 1 and 1.0 are the same JSON number.
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(b'[1, 1.0]'), schema)
    assert exc_info.value.rule == 'uniqueItems'
    # ... but true is not the number 1, and "1" is not the number 1.
    assert validate(ChunkedReader(b'[1, true, "1", 1.5]'), schema)


def test_unique_items_nested_objects_different_key_order():
    schema = {
        'type': 'object',
        'properties': {
            'items': {'type': 'array', 'uniqueItems': True},
        },
    }
    document = json.dumps({
        'items': [
            {'a': 1, 'b': {'x': [1, 2], 'y': None}},
            {'b': {'y': None, 'x': [1, 2]}, 'a': 1},
        ],
    }).encode('utf-8')
    with pytest.raises(ValidationError) as exc_info:
        validate(ChunkedReader(document), schema)
    assert exc_info.value.rule == 'uniqueItems'
    assert exc_info.value.path == 'items'


def test_unique_items_distinct_nested_objects_pass():
    schema = {'type': 'array', 'uniqueItems': True}
    document = json.dumps([
        {'a': 1, 'b': {'x': 1}},
        {'a': 1, 'b': {'x': 2}},
        {'a': 1, 'b': {'x': 1, 'y': 0}},
        [1, 2],
        [2, 1],
    ]).encode('utf-8')
    assert validate(ChunkedReader(document), schema)


def test_unique_items_budget_exceeded_fails_loudly():
    schema = {'type': 'array', 'uniqueItems': True}
    document = json.dumps(list(range(50))).encode('utf-8')
    # With a generous budget the document validates fine.
    assert validate(ChunkedReader(document), schema)
    # With a tiny budget validation cannot continue and must say so
    # instead of silently dropping the uniqueness check.
    with pytest.raises(ValidationBudgetExceeded) as exc_info:
        validate(ChunkedReader(document), schema, unique_items_budget=10)
    assert exc_info.value.path == ''
    assert 'cannot continue' in str(exc_info.value)


def test_unique_items_budget_freed_when_array_closes():
    schema = {
        'type': 'object',
        'properties': {
            'first': {'type': 'array', 'uniqueItems': True},
            'second': {'type': 'array', 'uniqueItems': True},
        },
    }
    document = json.dumps({
        'first': list(range(20)),
        'second': list(range(20)),
    }).encode('utf-8')
    # Each array fits the budget on its own; together they would exceed it
    # if the budget were not released when an array closes.
    assert validate(ChunkedReader(document), schema, unique_items_budget=30)


def test_validator_basecoro_forwards_events():
    document = b'{"name": "Alice", "age": 30}'
    events = utils.sendable_list()
    coro = validator_basecoro(events, PERSON_SCHEMA)
    for event in backend.parse(ChunkedReader(document)):
        coro.send(event)
    assert list(events) == list(backend.parse(ChunkedReader(document)))

    events = utils.sendable_list()
    coro = validator_basecoro(events, PERSON_SCHEMA)
    with pytest.raises(ValidationError):
        for event in backend.parse(ChunkedReader(b'{"name": 1}')):
            coro.send(event)
