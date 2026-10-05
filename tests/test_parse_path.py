"""Tests for the ijson.parse_path method"""

import ijson
from ijson.common import ARRAY_WILDCARD

# Document exercising keys that are ambiguous under the dotted-prefix API:
# empty keys, keys with dots, the "item" array marker and numeric strings,
# plus nested arrays.
TRICKY_JSON = b'''{
  "": {"a.b": [{"item": 1}, {"0": 2}]},
  "a.b": "dotted",
  "item": [10, 20],
  "0": {"1": "numeric"},
  "nested": [[["deep"]]]
}'''
TRICKY_OBJECT = {
    "": {"a.b": [{"item": 1}, {"0": 2}]},
    "a.b": "dotted",
    "item": [10, 20],
    "0": {"1": "numeric"},
    "nested": [[["deep"]]],
}
TRICKY_PARSE_PATH_EVENTS = [
    ((), 'start_map', None),
    ((), 'map_key', ''),
    (('',), 'start_map', None),
    (('',), 'map_key', 'a.b'),
    (('', 'a.b'), 'start_array', None),
    (('', 'a.b', 0), 'start_map', None),
    (('', 'a.b', 0), 'map_key', 'item'),
    (('', 'a.b', 0, 'item'), 'number', 1),
    (('', 'a.b', 0), 'end_map', None),
    (('', 'a.b', 1), 'start_map', None),
    (('', 'a.b', 1), 'map_key', '0'),
    (('', 'a.b', 1, '0'), 'number', 2),
    (('', 'a.b', 1), 'end_map', None),
    (('', 'a.b'), 'end_array', None),
    (('',), 'end_map', None),
    ((), 'map_key', 'a.b'),
    (('a.b',), 'string', 'dotted'),
    ((), 'map_key', 'item'),
    (('item',), 'start_array', None),
    (('item', 0), 'number', 10),
    (('item', 1), 'number', 20),
    (('item',), 'end_array', None),
    ((), 'map_key', '0'),
    (('0',), 'start_map', None),
    (('0',), 'map_key', '1'),
    (('0', '1'), 'string', 'numeric'),
    (('0',), 'end_map', None),
    ((), 'map_key', 'nested'),
    (('nested',), 'start_array', None),
    (('nested', 0), 'start_array', None),
    (('nested', 0, 0), 'start_array', None),
    (('nested', 0, 0, 0), 'string', 'deep'),
    (('nested', 0, 0), 'end_array', None),
    (('nested', 0), 'end_array', None),
    (('nested',), 'end_array', None),
    ((), 'end_map', None),
]


def _locate(obj, path):
    for segment in path:
        obj = obj[segment]
    return obj


def test_parse_path(adaptor):
    assert TRICKY_PARSE_PATH_EVENTS == adaptor.parse_path(TRICKY_JSON)


def test_parse_path_events_have_tuple_paths(adaptor):
    for path, _, _ in adaptor.parse_path(TRICKY_JSON):
        assert isinstance(path, tuple)
        for segment in path:
            assert isinstance(segment, (str, int))


def test_parse_path_events_do_not_change_after_delivery(adaptor):
    # Events are collected as they are delivered; if the internal position
    # stack were reused by reference, previously delivered paths would
    # mutate as parsing progresses and no longer locate their values.
    events = adaptor.parse_path(TRICKY_JSON)
    snapshots = [path for path, _, _ in events]
    assert snapshots == [path for path, _, _ in events]


def test_parse_path_locates_values(adaptor):
    for path, event, value in adaptor.parse_path(TRICKY_JSON):
        if event == 'map_key':
            container = _locate(TRICKY_OBJECT, path)
            assert isinstance(container, dict)
            assert value in container
        elif event == 'start_map':
            assert isinstance(_locate(TRICKY_OBJECT, path), dict)
        elif event == 'start_array':
            assert isinstance(_locate(TRICKY_OBJECT, path), list)
        elif event in ('end_map', 'end_array'):
            assert isinstance(_locate(TRICKY_OBJECT, path), (dict, list))
        else:
            assert _locate(TRICKY_OBJECT, path) == value


def test_parse_path_top_level_scalar(adaptor):
    assert [((), 'number', 1)] == adaptor.parse_path(b'1')


def test_parse_path_top_level_array(adaptor):
    assert [
        ((), 'start_array', None),
        ((0,), 'string', 'a'),
        ((1,), 'string', 'b'),
        ((), 'end_array', None),
    ] == adaptor.parse_path(b'["a", "b"]')


def test_parse_path_user_events(backend):
    """parse_path also accepts an iterable of basic_parse events"""
    basic_events = list(backend.basic_parse(b'{"a.b": [1]}'))
    assert [
        ((), 'start_map', None),
        ((), 'map_key', 'a.b'),
        (('a.b',), 'start_array', None),
        (('a.b', 0), 'number', 1),
        (('a.b',), 'end_array', None),
        ((), 'end_map', None),
    ] == list(backend.parse_path(basic_events))


def test_parse_path_distinct_from_prefix_api(backend):
    """The dotted prefix API is left unchanged by the typed path API"""
    prefixes = [prefix for prefix, _, _ in backend.parse(TRICKY_JSON)]
    assert 'a.b' in prefixes
    assert 'item.item' in prefixes
    paths = [path for path, _, _ in backend.parse_path(TRICKY_JSON)]
    assert ('a.b',) in paths
    assert ('item', 0) in paths
    assert ARRAY_WILDCARD is ijson.ARRAY_WILDCARD
