"""Tests for the ijson.filtered_items streaming filter"""

import io
import os

import pytest

import ijson
from ijson import JSONError
from ijson.filtering import FieldCondition, filtered_items_events


JSON_OBJECTS = (
    b'[{"id": 1, "status": "accepted", "payload": "aaa"},'
    b' {"id": 2, "status": "rejected", "payload": "bbb"},'
    b' {"id": 3, "status": "accepted"},'
    b' {"id": 4}]'
)


def test_filtered_items_basic(backend):
    result = list(backend.filtered_items(
        JSON_OBJECTS, 'item', FieldCondition('status', 'eq', 'accepted')
    ))
    assert result == [
        {'id': 1, 'status': 'accepted', 'payload': 'aaa'},
        {'id': 3, 'status': 'accepted'},
    ]


def test_filtered_items_tuple_shorthand(backend):
    result = list(backend.filtered_items(
        JSON_OBJECTS, 'item', ('status', 'ne', 'rejected')
    ))
    assert [item['id'] for item in result] == [1, 3]


def test_filtered_items_nested_prefix(backend):
    json = b'{"results": [{"status": "yes"}, {"status": "no"}]}'
    result = list(backend.filtered_items(
        json, 'results.item', ('status', 'eq', 'yes')
    ))
    assert result == [{'status': 'yes'}]


def test_filtered_items_multiple_conditions(backend):
    json = (
        b'[{"status": "ok", "n": 5},'
        b' {"status": "ok", "n": 1},'
        b' {"status": "ko", "n": 9}]'
    )
    result = list(backend.filtered_items(
        json, 'item',
        [FieldCondition('status', 'eq', 'ok'), FieldCondition('n', 'gt', 2)]
    ))
    assert result == [{'status': 'ok', 'n': 5}]


def test_filtered_items_nested_field_path(backend):
    json = (
        b'[{"meta": {"state": "ok"}, "v": 1},'
        b' {"meta": {"state": "bad"}, "v": 2}]'
    )
    result = list(backend.filtered_items(
        json, 'item', ('meta.state', 'eq', 'ok')
    ))
    assert result == [{'meta': {'state': 'ok'}, 'v': 1}]


def test_filtered_items_in_operator(backend):
    result = list(backend.filtered_items(
        JSON_OBJECTS, 'item', ('status', 'in', ['accepted', 'pending'])
    ))
    assert [item['id'] for item in result] == [1, 3]


def test_filtered_items_missing_field_rejected(backend):
    result = list(backend.filtered_items(
        JSON_OBJECTS, 'item', ('status', 'ne', 'rejected')
    ))
    assert 4 not in [item['id'] for item in result]


def test_filtered_items_duplicate_last_wins_by_default(backend):
    json = (
        b'[{"status": "accepted", "status": "rejected", "id": 1},'
        b' {"status": "rejected", "status": "accepted", "id": 2}]'
    )
    result = list(backend.filtered_items(
        json, 'item', ('status', 'eq', 'accepted')
    ))
    assert [item['id'] for item in result] == [2]


def test_filtered_items_duplicate_first_policy(backend):
    json = (
        b'[{"status": "accepted", "status": "rejected", "id": 1},'
        b' {"status": "rejected", "status": "accepted", "id": 2}]'
    )
    result = list(backend.filtered_items(
        json, 'item', ('status', 'eq', 'accepted'), on_duplicate='first'
    ))
    assert [item['id'] for item in result] == [1]


def test_filtered_items_passthrough_non_objects(backend):
    json = b'[[1, 2], {"status": "yes"}, 42, {"status": "no"}]'
    result = list(backend.filtered_items(
        json, 'item', ('status', 'eq', 'yes')
    ))
    assert result == [[1, 2], {'status': 'yes'}, 42]


def test_filtered_items_map_type(backend):
    from collections import OrderedDict
    result = list(backend.filtered_items(
        JSON_OBJECTS, 'item', ('status', 'eq', 'accepted'),
        map_type=OrderedDict
    ))
    assert all(isinstance(item, OrderedDict) for item in result)


def test_condition_rejects_non_scalar_value():
    with pytest.raises(ValueError):
        FieldCondition('status', 'eq', object())
    with pytest.raises(ValueError):
        FieldCondition('status', 'eq', lambda value: True)
    with pytest.raises(ValueError):
        FieldCondition('status', 'in', 'accepted')
    with pytest.raises(ValueError):
        FieldCondition('status', 'in', [object()])


def test_condition_rejects_unknown_operator():
    with pytest.raises(ValueError):
        FieldCondition('status', 'eval', 'accepted')


def test_condition_rejects_bad_field():
    with pytest.raises(ValueError):
        FieldCondition('', 'eq', 'accepted')
    with pytest.raises(ValueError):
        FieldCondition(None, 'eq', 'accepted')


def test_filtered_items_rejects_bad_arguments(backend):
    with pytest.raises(ValueError):
        backend.filtered_items(JSON_OBJECTS, 'item', [])
    with pytest.raises(ValueError):
        backend.filtered_items(
            JSON_OBJECTS, 'item', ('status', 'eq', 'accepted'),
            on_duplicate='ignore'
        )
    with pytest.raises(ValueError):
        backend.filtered_items(
            JSON_OBJECTS, 'item', ('status', 'eq', 'accepted'),
            max_buffered_events=-1
        )
    with pytest.raises(TypeError):
        backend.filtered_items(JSON_OBJECTS, 'item', [object()])


def test_filtered_items_events_validation():
    with pytest.raises(ValueError):
        filtered_items_events(iter([]), 'item', [])


class CountingReader:
    """A file-like object counting read() calls and snapshotting a directory."""

    def __init__(self, data, observed_dir):
        self._file = io.BytesIO(data)
        self._observed_dir = observed_dir
        self.reads = 0
        self.dir_snapshots = []

    def read(self, size=-1):
        self.reads += 1
        self.dir_snapshots.append(os.listdir(self._observed_dir))
        return self._file.read(size)


def _big_rejected_json(payload_size):
    return (
        b'[{"id": 1, "status": "accepted"},'
        b' {"id": 2, "payload": [' + b'0,' * payload_size + b'0], "status": "rejected"},'
        b' {"id": 3, "status": "accepted"}]'
    )


def test_rejected_object_spills_to_temp_dir(backend, tmp_path):
    reader = CountingReader(_big_rejected_json(20000), tmp_path)
    result = list(backend.filtered_items(
        reader, 'item', ('status', 'eq', 'accepted'),
        max_buffered_events=8, temp_dir=str(tmp_path), buf_size=128
    ))
    assert [item['id'] for item in result] == [1, 3]
    # The source was read incrementally, many times
    assert reader.reads > 10
    # While reading, the bounded buffer spilled to a temp file...
    assert any(
        any(name.startswith('ijson-filter-') for name in snapshot)
        for snapshot in reader.dir_snapshots
    )
    # ...and every temp file was removed once done
    assert os.listdir(tmp_path) == []


def test_rejected_object_not_cached_with_first_policy(backend, tmp_path):
    json = (
        b'[{"status": "rejected", "payload": [' + b'0,' * 20000 + b'0]},'
        b' {"status": "accepted"}]'
    )
    reader = CountingReader(json, tmp_path)
    result = list(backend.filtered_items(
        reader, 'item', ('status', 'eq', 'accepted'),
        on_duplicate='first', max_buffered_events=8,
        temp_dir=str(tmp_path), buf_size=128
    ))
    assert result == [{'status': 'accepted'}]
    # The source was still fully consumed...
    assert reader.reads > 10
    # ...but after the early rejection nothing was buffered or spilled
    assert not any(reader.dir_snapshots)
    assert os.listdir(tmp_path) == []


def test_early_close_cleans_up(backend, tmp_path):
    reader = CountingReader(_big_rejected_json(20000), tmp_path)
    generator = backend.filtered_items(
        reader, 'item', ('status', 'eq', 'accepted'),
        max_buffered_events=8, temp_dir=str(tmp_path), buf_size=128
    )
    first = next(generator)
    assert first['id'] == 1
    generator.close()
    assert os.listdir(tmp_path) == []


def test_corrupt_source_cleans_up(backend, tmp_path):
    json = (
        b'[{"id": 1, "status": "accepted"},'
        b' {"id": 2, "payload": [' + b'0,' * 20000
    )
    reader = CountingReader(json, tmp_path)
    generator = backend.filtered_items(
        reader, 'item', ('status', 'eq', 'accepted'),
        max_buffered_events=8, temp_dir=str(tmp_path), buf_size=128
    )
    assert next(generator)['id'] == 1
    with pytest.raises(JSONError):
        next(generator)
    assert os.listdir(tmp_path) == []


def test_accepted_object_replayed_from_spill(backend, tmp_path):
    json = (
        b'[{"payload": "' + b'x' * 20000 + b'", "status": "accepted",'
        b' "nested": {"list": [1, 2, {"deep": true}]}}]'
    )
    result = list(backend.filtered_items(
        io.BytesIO(json), 'item', ('status', 'eq', 'accepted'),
        max_buffered_events=4, temp_dir=str(tmp_path)
    ))
    assert result == [{
        'payload': 'x' * 20000,
        'status': 'accepted',
        'nested': {'list': [1, 2, {'deep': True}]},
    }]
    assert os.listdir(tmp_path) == []
