"""Tests for the typed-path API: ijson.parse_typed and ijson.items_at_path"""

import json

import pytest

from ijson.common import ANY_INDEX


TYPED_JSON = b'''
{
  "a.b": {"": [10, 20]},
  "item": ["x", ["y"]],
  "0": {"0": "zero"},
  "nested": [[1, 2], [3]]
}
'''
TYPED_JSON_VALUE = json.loads(TYPED_JSON.decode('utf-8'))
TYPED_JSON_EVENTS = [
    ((), 'start_map', None),
    (('a.b',), 'map_key', 'a.b'),
    (('a.b',), 'start_map', None),
    (('a.b', ''), 'map_key', ''),
    (('a.b', ''), 'start_array', None),
    (('a.b', '', 0), 'number', 10),
    (('a.b', '', 1), 'number', 20),
    (('a.b', ''), 'end_array', None),
    (('a.b',), 'end_map', None),
    (('item',), 'map_key', 'item'),
    (('item',), 'start_array', None),
    (('item', 0), 'string', 'x'),
    (('item', 1), 'start_array', None),
    (('item', 1, 0), 'string', 'y'),
    (('item', 1), 'end_array', None),
    (('item',), 'end_array', None),
    (('0',), 'map_key', '0'),
    (('0',), 'start_map', None),
    (('0', '0'), 'map_key', '0'),
    (('0', '0'), 'string', 'zero'),
    (('0',), 'end_map', None),
    (('nested',), 'map_key', 'nested'),
    (('nested',), 'start_array', None),
    (('nested', 0), 'start_array', None),
    (('nested', 0, 0), 'number', 1),
    (('nested', 0, 1), 'number', 2),
    (('nested', 0), 'end_array', None),
    (('nested', 1), 'start_array', None),
    (('nested', 1, 0), 'number', 3),
    (('nested', 1), 'end_array', None),
    (('nested',), 'end_array', None),
    ((), 'end_map', None),
]


def _resolve(value, path):
    for segment in path:
        value = value[segment]
    return value


def test_parse_typed(adaptor):
    assert TYPED_JSON_EVENTS == adaptor.parse_typed(TYPED_JSON)


def test_parse_typed_paths_are_immutable(adaptor):
    events = adaptor.parse_typed(TYPED_JSON)
    assert all(type(path) is tuple for path, _, _ in events)
    # Re-parsing yields equal events, so nothing was mutated in place
    assert events == adaptor.parse_typed(TYPED_JSON)


def test_parse_typed_paths_resolve_to_expected_objects(adaptor):
    for path, event, value in adaptor.parse_typed(TYPED_JSON):
        target = _resolve(TYPED_JSON_VALUE, path)
        if event in ('start_map', 'end_map'):
            assert isinstance(target, dict)
        elif event in ('start_array', 'end_array'):
            assert isinstance(target, list)
        elif event == 'map_key':
            assert path[-1] == value
        else:
            assert target == value


def test_parse_typed_user_events(backend):
    basic_parse_events = [
        ('start_array', None),
        ('number', 0),
        ('end_array', None),
    ]
    expected_typed_events = [
        ((), 'start_array', None),
        ((0,), 'number', 0),
        ((), 'end_array', None),
    ]
    assert expected_typed_events == list(backend.parse_typed(basic_parse_events))


def test_items_at_path(adaptor):
    assert [[10, 20]] == adaptor.items_at_path(TYPED_JSON, ('a.b', ''))
    assert [10, 20] == adaptor.items_at_path(TYPED_JSON, ('a.b', '', ANY_INDEX))
    assert ['x', ['y']] == adaptor.items_at_path(TYPED_JSON, ('item', ANY_INDEX))
    assert ['zero'] == adaptor.items_at_path(TYPED_JSON, ('0', '0'))
    assert [1, 2, 3] == adaptor.items_at_path(TYPED_JSON, ('nested', ANY_INDEX, ANY_INDEX))
    assert [3] == adaptor.items_at_path(TYPED_JSON, ('nested', 1, 0))
    assert [TYPED_JSON_VALUE] == adaptor.items_at_path(TYPED_JSON, ())


def test_items_at_path_wildcard_matches_only_array_positions(adaptor):
    # The top-level value is an object, so no array position matches
    assert [] == adaptor.items_at_path(TYPED_JSON, (ANY_INDEX,))
    # '0' is an object key, not an array index
    assert [] == adaptor.items_at_path(TYPED_JSON, (ANY_INDEX, '0'))
    # ... and an integer segment does not match object keys either
    assert [] == adaptor.items_at_path(TYPED_JSON, (0,))


def test_items_at_path_on_typed_events(backend):
    typed_events = list(backend.parse_typed(TYPED_JSON))
    assert [[1, 2], [3]] == list(backend.items_at_path(typed_events, ('nested', ANY_INDEX)))


def test_items_at_path_rejects_string_path(adaptor):
    with pytest.raises(TypeError):
        adaptor.items_at_path(TYPED_JSON, 'a.b.')
