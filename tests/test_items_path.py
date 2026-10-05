"""Tests for the ijson.items_path and ijson.kvitems_path methods"""

import collections

import pytest

from ijson import ARRAY_WILDCARD

from .test_parse_path import TRICKY_JSON


@pytest.mark.parametrize(
    "path, expected_items",
    (
        # exact string keys, including ones ambiguous in the prefix API
        (('a.b',), ['dotted']),
        (('', 'a.b', 0, 'item'), [1]),
        (('', 'a.b', 1, '0'), [2]),
        (('0', '1'), ['numeric']),
        # exact integer array positions
        (('item', 0), [10]),
        (('item', 1), [20]),
        (('nested', 0, 0, 0), ['deep']),
        # explicit array wildcard
        (('item', ARRAY_WILDCARD), [10, 20]),
        (('', 'a.b', ARRAY_WILDCARD), [{'item': 1}, {'0': 2}]),
        (('nested', ARRAY_WILDCARD, ARRAY_WILDCARD, ARRAY_WILDCARD), ['deep']),
        # the wildcard only matches array positions, not map keys
        ((ARRAY_WILDCARD,), []),
        (('item', ARRAY_WILDCARD, ARRAY_WILDCARD), []),
        # non-matching selectors
        (('a', 'b'), []),
        (('item', 2), []),
    )
)
def test_items_path(adaptor, path, expected_items):
    assert expected_items == adaptor.items_path(TRICKY_JSON, path)


def test_items_path_whole_document(adaptor):
    assert [ {
        "": {"a.b": [{"item": 1}, {"0": 2}]},
        "a.b": "dotted",
        "item": [10, 20],
        "0": {"1": "numeric"},
        "nested": [[["deep"]]],
    } ] == adaptor.items_path(TRICKY_JSON, ())


def test_items_path_map_type(adaptor):
    obj = adaptor.items_path(TRICKY_JSON, ('0',))[0]
    assert isinstance(obj, dict)
    obj = adaptor.items_path(
        TRICKY_JSON, ('0',), map_type=collections.OrderedDict
    )[0]
    assert isinstance(obj, collections.OrderedDict)


def test_items_path_user_events(backend):
    """items_path also accepts an iterable of parse_path events"""
    events = list(backend.parse_path(TRICKY_JSON))
    assert [10, 20] == list(
        backend.items_path(events, ('item', ARRAY_WILDCARD))
    )


@pytest.mark.parametrize(
    "path, expected_pairs",
    (
        (('0',), [('1', 'numeric')]),
        (('',), [('a.b', [{'item': 1}, {'0': 2}])]),
        (('', 'a.b', 0), [('item', 1)]),
        (('', 'a.b', ARRAY_WILDCARD), [('item', 1), ('0', 2)]),
        (('item',), []),
    )
)
def test_kvitems_path(adaptor, path, expected_pairs):
    assert expected_pairs == adaptor.kvitems_path(TRICKY_JSON, path)


def test_kvitems_path_user_events(backend):
    """kvitems_path also accepts an iterable of parse_path events"""
    events = list(backend.parse_path(TRICKY_JSON))
    assert [('1', 'numeric')] == list(backend.kvitems_path(events, ('0',)))
