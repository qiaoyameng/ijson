"""Tests for the multi_items multi-subscription interface"""

import io

import pytest

from ijson import SubscriptionItem


# A response containing dotted object keys, an empty array and nested
# parent/child matches.
NESTED_JSON = b'''{
  "records": [
    {"id": 1, "attachments": [{"name": "a"}, {"name": "b"}]},
    {"id": 2, "attachments": []}
  ],
  "a.b": {"x.y": 7}
}'''

NESTED_RULES = [
    ("records", "records.item"),
    ("attachments", "records.item.attachments.item"),
    ("dotted", ("a.b", "x.y")),
]

NESTED_EXPECTED = [
    SubscriptionItem("attachments", {"name": "a"}),
    SubscriptionItem("attachments", {"name": "b"}),
    SubscriptionItem(
        "records",
        {"id": 1, "attachments": [{"name": "a"}, {"name": "b"}]},
    ),
    SubscriptionItem("records", {"id": 2, "attachments": []}),
    SubscriptionItem("dotted", 7),
]


def test_multi_items_output_order(adaptor):
    assert NESTED_EXPECTED == adaptor.multi_items(NESTED_JSON, NESTED_RULES)


def test_multi_items_dotted_key_via_string_path(adaptor):
    # String paths split on dots and therefore cannot express dotted keys;
    # tuple paths preserve them verbatim
    result = adaptor.multi_items(
        b'{"a.b": 5}', [("dotted", ("a.b",))]
    )
    assert [SubscriptionItem("dotted", 5)] == result


def test_multi_items_string_path_with_item_wildcard(adaptor):
    result = adaptor.multi_items(
        b'{"docs": [1, 2, 3]}', [("doc", "docs.item")]
    )
    assert [
        SubscriptionItem("doc", 1),
        SubscriptionItem("doc", 2),
        SubscriptionItem("doc", 3),
    ] == result


def test_multi_items_integer_index(adaptor):
    result = adaptor.multi_items(
        NESTED_JSON,
        [
            ("first", ("records", 0, "id")),
            ("second", ("records", 1, "id")),
        ],
    )
    assert [
        SubscriptionItem("first", 1),
        SubscriptionItem("second", 2),
    ] == result


def test_multi_items_nested_array_index(adaptor):
    result = adaptor.multi_items(
        b'{"m": [[1, 2], [3, 4]]}',
        [("x", ("m", 1, 0)), ("y", ("m", 0, 1))],
    )
    assert [
        SubscriptionItem("y", 2),
        SubscriptionItem("x", 3),
    ] == result


def test_multi_items_same_position_declaration_order(adaptor):
    result = adaptor.multi_items(
        b'{"a": {"b": 1}}',
        [("first", "a"), ("second", "a"), ("third", "a")],
    )
    assert [
        SubscriptionItem("first", {"b": 1}),
        SubscriptionItem("second", {"b": 1}),
        SubscriptionItem("third", {"b": 1}),
    ] == result
    # Every rule gets its own independently constructed object
    assert result[0].value is not result[1].value


def test_multi_items_parent_and_child_independent(adaptor):
    result = adaptor.multi_items(
        b'{"parent": {"child": {"n": [1]}, "keep": 9}}',
        [("child", "parent.child"), ("parent", "parent")],
    )
    # Child closes before the parent, so it is emitted first
    assert ["child", "parent"] == [item.name for item in result]
    child, parent = result
    child.value["n"].append(99)
    child.value["extra"] = True
    assert parent.value == {"child": {"n": [1]}, "keep": 9}
    assert child.value is not parent.value["child"]


def test_multi_items_empty_containers(adaptor):
    result = adaptor.multi_items(
        b'{"empty_array": [], "empty_object": {}}',
        [
            ("array", "empty_array"),
            ("object", "empty_object"),
            ("element", "empty_array.item"),
        ],
    )
    assert [
        SubscriptionItem("array", []),
        SubscriptionItem("object", {}),
    ] == result


def test_multi_items_root_array(adaptor):
    result = adaptor.multi_items(
        b'[10, 20, 30]',
        [("zero", (0,)), ("one", (1,)), ("item", "item"), ("all", "")],
    )
    assert [
        SubscriptionItem("zero", 10),
        SubscriptionItem("item", 10),
        SubscriptionItem("one", 20),
        SubscriptionItem("item", 20),
        SubscriptionItem("item", 30),
        SubscriptionItem("all", [10, 20, 30]),
    ] == result


def test_multi_items_root_scalar(adaptor):
    result = adaptor.multi_items(b'42', [("root", "")])
    assert [SubscriptionItem("root", 42)] == result


def test_multi_items_empty_root_array(adaptor):
    result = adaptor.multi_items(
        b'[]', [("item", "item"), ("all", "")]
    )
    assert [SubscriptionItem("all", [])] == result


def test_multi_items_nested_empty_arrays(adaptor):
    result = adaptor.multi_items(
        b'{"v": [[], [1]]}',
        [("v", "v.item"), ("n", "v.item.item")],
    )
    assert [
        SubscriptionItem("v", []),
        SubscriptionItem("n", 1),
        SubscriptionItem("v", [1]),
    ] == result


def test_multi_items_no_match_yields_nothing(adaptor):
    assert [] == adaptor.multi_items(b'{"a": 1}', [("x", "b")])


@pytest.mark.parametrize(
    "rules",
    (
        [],
        [("only_name",)],
        [("", "a")],
        [("ok", 123)],
        [("ok", [1.5])],
    ),
)
def test_multi_items_invalid_rules(adaptor, rules):
    with pytest.raises(ValueError):
        adaptor.multi_items(b'{}', rules)


def test_multi_items_duplicated_name_rejected_before_reading(backend):
    class ExplodingReader:
        def read(self, size):
            raise AssertionError("source must not be read")

    with pytest.raises(ValueError, match="duplicated subscription name"):
        list(
            backend.multi_items(
                ExplodingReader(), [("x", "a"), ("x", "b")]
            )
        )


def test_multi_items_single_underlying_read(backend):
    class CountingReader:
        def __init__(self, data):
            self._buffer = io.BytesIO(data)
            self.reads = 0

        def read(self, size):
            self.reads += 1
            return self._buffer.read(size)

    data = NESTED_JSON
    multi_reader = CountingReader(data)
    list(
        backend.multi_items(
            multi_reader,
            [
                ("records", "records.item"),
                ("attachments", "records.item.attachments.item"),
            ],
            buf_size=16,
        )
    )

    separate_reads = 0
    for prefix in ("records.item", "records.item.attachments.item"):
        reader = CountingReader(data)
        list(backend.items(reader, prefix, buf_size=16))
        separate_reads += reader.reads

    assert multi_reader.reads == separate_reads // 2


def test_multi_items_map_type(adaptor):
    import collections

    result = adaptor.multi_items(
        b'{"a": {"b": 1}}', [("a", "a")], map_type=collections.OrderedDict
    )
    assert isinstance(result[0].value, collections.OrderedDict)


def test_multi_items_coro_closes_without_leftover_builders(backend):
    import gc
    import weakref

    from ijson import common

    created = []
    original_init = common.ObjectBuilder.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(weakref.ref(self))

    common.ObjectBuilder.__init__ = tracking_init
    try:
        generator = backend.multi_items(
            io.BytesIO(
                b'{"big": ['
                + b",".join(b'{"k": %d}' % i for i in range(500))
                + b"]}"
            ),
            [("item", "big.item")],
            buf_size=4,
        )
        for index, _item in enumerate(generator):
            if index == 2:
                generator.close()
                break
    finally:
        common.ObjectBuilder.__init__ = original_init

    gc.collect()
    assert all(ref() is None for ref in created)
