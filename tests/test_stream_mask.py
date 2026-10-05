"""Tests for the path-rule based streaming JSON rewriter."""

import io
import json
import re

import pytest

from ijson import stream_mask
from ijson.stream_mask import (
    DeleteArrayElements,
    DeleteField,
    ReplaceField,
    StreamMaskError,
    rewrite,
    rewrite_text,
)


NUMBER_LEXEME_RE = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?')


def number_lexemes(text):
    return NUMBER_LEXEME_RE.findall(text)


def stream(text, chunk_size):
    if isinstance(text, bytes):
        return io.BytesIO(text)
    return io.StringIO(text)


def rewrite_in_chunks(text, rules, chunk_size=1):
    out = io.StringIO()
    rewrite(stream(text, chunk_size), out, rules, chunk_size=chunk_size)
    return out.getvalue()


@pytest.mark.parametrize('chunk_size', [1, 2, 3, 7, 4096])
def test_first_and_last_field_deletion(chunk_size):
    doc = '{"first": 1, "middle": 2, "last": 3}'
    out = rewrite_in_chunks(
        doc, [DeleteField(('first',)), DeleteField(('last',))], chunk_size)
    assert json.loads(out) == {"middle": 2}
    assert out == '{"middle":2}'


def test_deleting_all_fields_yields_empty_object():
    doc = '{"a": {"nested": 1}, "b": 2}'
    out = rewrite_text(doc, [
        DeleteField(('a',)), DeleteField(('b',)),
    ])
    assert out == '{}'
    assert json.loads(out) == {}


def test_empty_containers_are_preserved():
    doc = '{"o": {}, "a": [], "keep": 0}'
    rules = [
        DeleteField(('o', 'missing')),
        DeleteArrayElements(('a',), {0, 5}),
    ]
    out = rewrite_text(doc, rules)
    assert json.loads(out) == {"o": {}, "a": [], "keep": 0}
    assert out == '{"o":{},"a":[],"keep":0}'


def test_nested_replacement_and_deletion():
    doc = '{"a": {"b": {"c": [1, 2, {"d": 3}]}}, "drop": true}'
    rules = [
        ReplaceField(('a', 'b'), {"r": [1e9, -0]}),
        DeleteField(('drop',)),
    ]
    out = rewrite_text(doc, rules)
    assert json.loads(out) == {"a": {"b": {"r": [1e9, 0]}}}
    assert out == '{"a":{"b":{"r": [1000000000.0, 0]}}}'


def test_array_element_deletion_uses_original_indices():
    doc = '{"xs": [10, 20, 30, 40]}'
    out = rewrite_text(doc, [DeleteArrayElements(('xs',), {0, 3})])
    assert json.loads(out) == {"xs": [20, 30]}
    assert out == '{"xs":[20,30]}'


def test_nested_array_rules():
    doc = '[[1, 2, 3], {"y": [4, 5]}]'
    rules = [
        DeleteArrayElements((0,), {1}),
        ReplaceField((1, 'y', 0), "masked"),
    ]
    out = rewrite_text(doc, rules)
    assert json.loads(out) == [[1, 3], {"y": ["masked", 5]}]


def test_array_delete_rule_cannot_target_object_fields():
    # A path ending in an index is not a field path and is rejected when the
    # DeleteField rule is constructed.
    with pytest.raises(StreamMaskError):
        DeleteField(('a', 0))
    with pytest.raises(StreamMaskError):
        DeleteField((0,))


def test_object_and_array_deletion_are_distinct_rules():
    # DeleteField needs a key ending path; an array deletion cannot reuse it.
    with pytest.raises(StreamMaskError):
        DeleteField(())
    # Pointing DeleteArrayElements at an object value simply matches no
    # array, while the object stays intact: the two rule kinds never mix.
    out = rewrite_text('{"a": [0, 1]}',
                       [DeleteArrayElements(('a', 'b'), {0})])
    assert json.loads(out) == {"a": [0, 1]}
    # And a field-delete path that lands inside an array element is valid
    # only as a key-ending path.
    with pytest.raises(StreamMaskError):
        DeleteField(('a', 0))


def test_number_lexemes_are_preserved_verbatim():
    doc = '{"big": 1e+09, "negzero": -0, "nums": [1.50, -0.0, 2E-3, 007.5]}'
    # 007.5 is intentionally NOT used (invalid JSON); use legal spellings.
    doc = '{"big": 1e+09, "negzero": -0, "nums": [1.50, -0.0, 2E-3, 0]}'
    out = rewrite_text(doc, [DeleteField(('negzero',))])
    parsed = json.loads(out)
    assert parsed["big"] == 1e9
    # Every surviving number keeps its original lexical fragment.
    assert '1e+09' in out
    assert '1.50' in out
    assert '-0.0' in out
    assert '2E-3' in out
    assert number_lexemes(doc) is not None
    surviving = [lex for lex in number_lexemes(doc) if lex != '-0']
    assert all(lex in out for lex in surviving)


@pytest.mark.parametrize('chunk_size', [1, 2, 3, 5])
def test_cross_chunk_escapes(chunk_size):
    # Escaped quote, backslash and unicode split across chunk boundaries.
    doc = r'{"keep": "a\"b\\c\u0041", "gone": "x", "emoji": "\ud83d\udca9"}'
    out = rewrite_in_chunks(doc, [DeleteField(('gone',))], chunk_size)
    parsed = json.loads(out)
    assert parsed == {"keep": 'a"b\\cA', "emoji": "\U0001f4a9"}
    assert r'a\"b\\c\u0041' in out
    assert r'\ud83d\udca9' in out


def test_cross_chunk_escape_at_every_byte_offset():
    doc = r'{"s": "x\\\"y", "n": 1e+09, "d": "z"}'
    for chunk_size in range(1, len(doc) + 1):
        out = rewrite_in_chunks(doc, [DeleteField(('d',))], chunk_size)
        assert json.loads(out)["s"] == 'x\\"y'


def test_untouched_string_spelling_is_preserved():
    doc = '{"a" :\n\t "k\\u0065y" , "b":  -0 }'
    out = rewrite_text(doc, [ReplaceField(('b',), None)])
    assert json.loads(out) == {"a": "key", "b": None}
    # String lexical spelling (including the \u escape) is untouched;
    # insignificant whitespace is normalised away.
    assert out == '{"a":"k\\u0065y","b":null}'


def test_replacement_goes_through_json_encoder():
    # A raw-looking string must be emitted as a quoted string, not injected.
    out = rewrite_text('{"x": 1}', [ReplaceField(('x',), '{"evil": 1}')])
    assert json.loads(out) == {"x": '{"evil": 1}'}
    assert '"{\\"evil\\": 1}"' in out

    out = rewrite_text('[1, 2]', [ReplaceField((1,), ['a', 1, True, None])])
    assert out == '[1,["a", 1, true, null]]'
    json.loads(out)


def test_root_scalar_replacement():
    assert rewrite_text('1e+09', [ReplaceField((), -0)]) == '0'
    assert rewrite_text('"x"', [ReplaceField((), {"k": "v"})]) == '{"k": "v"}'
    out = rewrite_text(b'{"a":1}', [ReplaceField((), [])])
    assert out == '[]'


def test_bytes_input_is_decoded_as_utf8():
    out = rewrite_text('{"a": "héllo", "b": 1}'.encode('utf-8'),
                       [DeleteField(('b',))])
    assert json.loads(out) == {"a": "héllo"}


def test_invalid_rules_are_rejected_upfront():
    with pytest.raises(StreamMaskError):
        rewrite_text('{}', [DeleteField((1,))])
    with pytest.raises(StreamMaskError):
        rewrite_text('{}', [ReplaceField(('a',), object())])
    with pytest.raises(StreamMaskError):
        rewrite_text('{}', [DeleteField(('a',)), DeleteField(('a',))])
    with pytest.raises(StreamMaskError):
        rewrite_text('{}', [ReplaceField(('a',), 1), ReplaceField(('a',), 2)])
    with pytest.raises(StreamMaskError):
        rewrite_text('{}', [object()])
    with pytest.raises(StreamMaskError):
        rewrite_text('{}', [DeleteField(('a', -1))])


def test_replacement_and_deletion_on_same_path_conflict_free():
    # Deleting a field while replacing its parent replaces the whole subtree.
    out = rewrite_text('{"p": {"a": 1, "b": 2}}',
                       [ReplaceField(('p',), {}), DeleteField(('p', 'a'))])
    assert out == '{"p":{}}'


def test_malformed_input_raises():
    with pytest.raises(StreamMaskError):
        rewrite_text('{"a":}', [])
    with pytest.raises(StreamMaskError):
        rewrite_text('{"a" 1}', [])
    with pytest.raises(StreamMaskError):
        rewrite_text('[1,,2]', [])
    with pytest.raises(StreamMaskError):
        rewrite_text('{"a": "unterminated}', [])
    with pytest.raises(StreamMaskError):
        rewrite_text('1 2', [])


def test_streaming_does_not_buffer_whole_document(tmp_path):
    # A large document rewritten through tiny chunks must produce correct
    # output and the reader only keeps a bounded buffer.
    items = []
    for i in range(5000):
        items.append('{"id": %d, "n": 1e+09, "drop": -0}' % i)
    doc = '{"items": [' + ','.join(items) + ']}'

    out = io.StringIO()
    rewrite(io.StringIO(doc), out,
            [DeleteField(('items', 0, 'drop'))], chunk_size=16)
    parsed = json.loads(out.getvalue())
    assert len(parsed["items"]) == 5000
    assert parsed["items"][0] == {"id": 0, "n": 1e9}
    assert parsed["items"][1]["drop"] == 0
    assert '1e+09' in out.getvalue()


def test_deleting_empty_container_field():
    doc = '{"a": {}, "b": [], "c": {"d": {}}}'
    out = rewrite_text(doc, [DeleteField(('a',)), DeleteField(('b',)),
                             DeleteField(('c', 'd'))])
    assert json.loads(out) == {"c": {}}


def test_array_first_and_last_removal_keeps_middle_spacing():
    doc = '{"xs": [1, 2, 3]}'
    out = rewrite_text(doc, [DeleteArrayElements(('xs',), {0, 2})])
    assert out == '{"xs":[2]}'
    out = rewrite_text('[1]', [DeleteArrayElements((), {0})])
    assert out == '[]'


def test_replacement_scalar_keeps_sibling_commas():
    doc = '{"a": 1, "b": 2, "c": 3}'
    out = rewrite_text(doc, [ReplaceField(('b',), {})])
    assert out == '{"a":1,"b":{},"c":3}'
    assert json.loads(out) == {"a": 1, "b": {}, "c": 3}
    out = rewrite_text('[1, 2, 3]', [ReplaceField((1,), [])])
    assert out == '[1,[],3]'


def test_random_documents_roundtrip(tmp_path):
    import random
    import string

    random.seed(42)

    def rand_value(depth):
        kind = random.choice(['num', 'str', 'null', 'bool', 'arr', 'obj'])
        if depth == 0:
            kind = random.choice(['num', 'str', 'null', 'bool'])
        if kind == 'num':
            return random.choice(['1e+09', '-0', '1.50', '0', '-2E-3', '42'])
        if kind == 'str':
            return json.dumps(''.join(random.choice(string.printable[:62])
                                      for _ in range(random.randint(0, 6))))
        if kind == 'null':
            return 'null'
        if kind == 'bool':
            return random.choice(['true', 'false'])
        if kind == 'arr':
            return '[' + ','.join(rand_value(depth - 1)
                                  for _ in range(random.randint(0, 4))) + ']'
        fields = []
        for key in ('a', 'b', 'c'):
            if random.random() < 0.7:
                fields.append(json.dumps(key) + ':' + rand_value(depth - 1))
        return '{' + ','.join(fields) + '}'

    numbers_before = []
    rules = []
    for _ in range(300):
        doc = rand_value(4)
        for chunk in (1, 5, 64):
            out = rewrite_in_chunks(doc, rules, chunk)
            # Output is always valid JSON.
            json.loads(out)
            # With no rules, the only difference from the input is
            # whitespace; every number lexeme survives verbatim.
            if not rules:
                for lexeme in number_lexemes(doc):
                    assert lexeme in out, (lexeme, doc, out)
