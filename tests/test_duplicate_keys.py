"""Tests for the duplicate_keys option"""

import io

import pytest

import ijson
from ijson import DuplicateKeyError, JSONError


DUPLICATE_JSON = b'{"status": 1, "status": 2}'


@pytest.fixture
def dup_adaptor(adaptor):
    if not adaptor.backend.capabilities.duplicate_keys:
        pytest.skip("backend doesn't support the duplicate_keys option")
    return adaptor


def test_allow_is_the_default(dup_adaptor):
    assert [{'status': 2}] == dup_adaptor.items(DUPLICATE_JSON, '')
    assert [{'status': 2}] == dup_adaptor.items(
        DUPLICATE_JSON, '', duplicate_keys='allow'
    )


def test_allow_parse_events_are_untouched(dup_adaptor):
    events = dup_adaptor.parse(DUPLICATE_JSON)
    assert [
        ('', 'start_map', None),
        ('', 'map_key', 'status'),
        ('status', 'number', 1),
        ('', 'map_key', 'status'),
        ('status', 'number', 2),
        ('', 'end_map', None),
    ] == events


def test_first_keeps_first_value(dup_adaptor):
    json = b'{"status": "a", "status": "b", "nested": {"k": 1, "k": 2}}'
    assert [{'status': 'a', 'nested': {'k': 1}}] == dup_adaptor.items(
        json, '', duplicate_keys='first'
    )


def test_last_keeps_last_value(dup_adaptor):
    json = b'{"status": "a", "status": "b", "nested": {"k": 1, "k": 2}}'
    assert [{'status': 'b', 'nested': {'k': 2}}] == dup_adaptor.items(
        json, '', duplicate_keys='last'
    )


def test_first_drops_whole_duplicate_subtree(dup_adaptor):
    json = b'{"a": {"x": 1}, "a": {"y": 2}}'
    assert [{'a': {'x': 1}}] == dup_adaptor.items(
        json, '', duplicate_keys='first'
    )


def test_kvitems_first(dup_adaptor):
    json = b'{"a": {"x": 1, "x": 2}, "b": 3}'
    assert [('a', {'x': 1}), ('b', 3)] == dup_adaptor.kvitems(
        json, '', duplicate_keys='first'
    )


def test_error_raises_at_second_key(dup_adaptor):
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.items(DUPLICATE_JSON, '', duplicate_keys='error')


def test_error_is_a_json_error(dup_adaptor):
    with pytest.raises(JSONError):
        dup_adaptor.items(DUPLICATE_JSON, '', duplicate_keys='error')


def test_error_message_contains_key_and_position(dup_adaptor):
    json = b'{"abc": 1, "abc": 2}'
    with pytest.raises(DuplicateKeyError) as exc_info:
        dup_adaptor.items(json, '', duplicate_keys='error')
    message = str(exc_info.value)
    assert "'abc'" in message
    assert str(json.index(b'"abc"', 2)) in message


def test_error_in_parse_event_stream(dup_adaptor):
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.parse(DUPLICATE_JSON, duplicate_keys='error')
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.basic_parse(DUPLICATE_JSON, duplicate_keys='error')


def test_error_on_empty_key(dup_adaptor):
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.items(b'{"": 1, "": 2}', '', duplicate_keys='error')


def test_error_on_differently_escaped_equal_keys(dup_adaptor):
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.items(b'{"\\u0061": 1, "a": 2}', '', duplicate_keys='error')


def test_error_counts_nested_objects_separately(dup_adaptor):
    json = b'{"a": {"a": 1}, "b": {"a": 2}}'
    assert [{'a': {'a': 1}, 'b': {'a': 2}}] == dup_adaptor.items(
        json, '', duplicate_keys='error'
    )


def test_error_on_nested_duplicate(dup_adaptor):
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.items(b'{"a": {"x": 1, "x": 2}}', '', duplicate_keys='error')


def test_error_counts_top_level_objects_separately(dup_adaptor):
    if not dup_adaptor.backend.capabilities.multiple_values:
        pytest.skip("backend doesn't support multiple values")
    json = b'{"a": 1} {"a": 2}'
    assert [{'a': 1}, {'a': 2}] == dup_adaptor.items(
        json, '', multiple_values=True, duplicate_keys='error'
    )


def test_error_in_second_top_level_object(dup_adaptor):
    if not dup_adaptor.backend.capabilities.multiple_values:
        pytest.skip("backend doesn't support multiple values")
    json = b'{"a": 1} {"a": 2, "a": 3}'
    with pytest.raises(DuplicateKeyError):
        dup_adaptor.items(
            json, '', multiple_values=True, duplicate_keys='error'
        )


def test_budget_exceeded_stops_with_error(dup_adaptor):
    json = b'{"a": 1, "b": 2, "c": 3}'
    with pytest.raises(JSONError) as exc_info:
        dup_adaptor.items(
            json, '', duplicate_keys='error', duplicate_keys_budget=2
        )
    assert not isinstance(exc_info.value, DuplicateKeyError)
    assert 'budget' in str(exc_info.value)


def test_parse_and_basic_parse_reject_object_policies(adaptor):
    for policy in ('first', 'last'):
        with pytest.raises(ValueError):
            adaptor.parse(DUPLICATE_JSON, duplicate_keys=policy)
        with pytest.raises(ValueError):
            adaptor.basic_parse(DUPLICATE_JSON, duplicate_keys=policy)


def test_invalid_policy_rejected(adaptor):
    with pytest.raises(ValueError):
        adaptor.items(DUPLICATE_JSON, '', duplicate_keys='bogus')
    with pytest.raises(ValueError):
        adaptor.parse(DUPLICATE_JSON, duplicate_keys='bogus')


def test_unsupported_backend_raises(adaptor):
    if adaptor.backend.capabilities.duplicate_keys:
        pytest.skip("backend supports the duplicate_keys option")
    with pytest.raises(ValueError):
        adaptor.items(DUPLICATE_JSON, '', duplicate_keys='error')


def test_object_policies_rejected_before_reading():
    class ExplodingReader:
        def read(self, n=-1):
            raise AssertionError("source should not be read")

    for routine in (ijson.parse, ijson.basic_parse):
        for policy in ('first', 'last'):
            with pytest.raises(ValueError):
                routine(ExplodingReader(), duplicate_keys=policy)


def test_error_across_read_chunks():
    backend = ijson.get_backend(ijson.backend_name)
    if not backend.capabilities.duplicate_keys:
        pytest.skip("backend doesn't support the duplicate_keys option")
    json = b'{"status": "' + b'x' * 100 + b'", "status": 2}'
    with pytest.raises(DuplicateKeyError):
        list(backend.items(io.BytesIO(json), '', buf_size=7,
                           duplicate_keys='error'))


def test_error_sends_no_object_to_target():
    events = ijson.sendable_list()
    coro = ijson.items_coro(events, '', duplicate_keys='error')
    with pytest.raises(DuplicateKeyError):
        coro.send(DUPLICATE_JSON)
    assert [] == events


def test_error_sends_no_object_to_target_across_sends():
    events = ijson.sendable_list()
    coro = ijson.items_coro(events, '', duplicate_keys='error')
    coro.send(b'{"status": 1, "sta')
    with pytest.raises(DuplicateKeyError):
        coro.send(b'tus": 2}')
    assert [] == events
