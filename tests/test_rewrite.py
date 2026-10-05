'''Tests for the streaming JSON rewriter (ijson.rewrite).'''

import io
import json

import pytest

from ijson.common import JSONError
from ijson.rewrite import (
    JsonRewriter,
    rewrite_text,
    rewrite_iterable,
    rewrite_file,
)


# --------------------------------------------------------------------------
# Independent path-aware number lexeme scanner. It walks raw JSON text without
# going through Python float parsing, so it preserves lexemes such as 1e+09.
# --------------------------------------------------------------------------

_NUMBER_CHARS = set('0123456789-+.eE')


def collect_number_lexemes(text):
    '''Return {path_tuple: number_lexeme} for every number in ``text``.'''
    pos = 0
    length = len(text)

    def skip_ws():
        nonlocal pos
        while pos < length and text[pos] in ' \t\n\r':
            pos += 1

    def read_string():
        nonlocal pos
        assert text[pos] == '"'
        pos += 1
        chars = []
        while True:
            char = text[pos]
            pos += 1
            if char == '"':
                return json.loads('"' + ''.join(chars) + '"')
            if char == '\\':
                chars.append(char)
                chars.append(text[pos])
                pos += 1
            else:
                chars.append(char)

    numbers = {}

    def parse_value(path):
        nonlocal pos
        skip_ws()
        char = text[pos]
        if char == '{':
            pos += 1
            skip_ws()
            if text[pos] == '}':
                pos += 1
                return
            while True:
                skip_ws()
                key = read_string()
                skip_ws()
                assert text[pos] == ':'
                pos += 1
                parse_value(path + (key,))
                skip_ws()
                if text[pos] == '}':
                    pos += 1
                    return
                assert text[pos] == ','
                pos += 1
        if char == '[':
            pos += 1
            skip_ws()
            if text[pos] == ']':
                pos += 1
                return
            index = 0
            while True:
                parse_value(path + (index,))
                index += 1
                skip_ws()
                if text[pos] == ']':
                    pos += 1
                    return
                assert text[pos] == ','
                pos += 1
                skip_ws()
        if char == '"':
            read_string()
            return
        if char in 'tfn':
            for literal in ('true', 'false', 'null'):
                if text.startswith(literal, pos):
                    pos += len(literal)
                    return
        start = pos
        while pos < length and text[pos] in _NUMBER_CHARS:
            pos += 1
        numbers[path] = text[start:pos]

    parse_value(())
    skip_ws()
    assert pos == length
    return numbers


# --------------------------------------------------------------------------
# Basic rewriting semantics
# --------------------------------------------------------------------------

def test_delete_first_field():
    out = rewrite_text('{"first": 1, "middle": 2, "last": 3}',
                       delete={('first',): True})
    assert json.loads(out) == {"middle": 2, "last": 3}
    assert out == '{"middle":2,"last":3}'


def test_delete_last_field():
    out = rewrite_text('{"first": 1, "middle": 2, "last": 3}',
                       delete={('last',): True})
    assert json.loads(out) == {"first": 1, "middle": 2}
    assert out == '{"first":1,"middle":2}'


def test_delete_only_field_leaves_empty_object():
    assert rewrite_text('{"only": 1}', delete={('only',): True}) == '{}'


def test_delete_nested_compound_field():
    out = rewrite_text('{"a": 1, "b": {"x": [1, 2], "y": 2}, "c": 3}',
                       delete={('b',): True})
    assert json.loads(out) == {"a": 1, "c": 3}


def test_empty_containers_preserved():
    assert rewrite_text('{}') == '{}'
    assert rewrite_text('[]') == '[]'
    out = rewrite_text('{"a": {}, "b": [], "c": [{}, []]}')
    assert json.loads(out) == {"a": {}, "b": [], "c": [{}, []]}


def test_nested_replacement_via_encoder():
    out = rewrite_text(
        '{"a": {"b": {"c": [1, 2]}}, "kept": 7}',
        replace={('a', 'b'): {"x": "v", "y": [True, None, -0.0]}})
    assert json.loads(out) == {
        "a": {"b": {"x": "v", "y": [True, None, -0.0]}},
        "kept": 7,
    }
    assert out.startswith('{"a":{"b":{"x":"v","y":[true,null,-0.0]}}')


def test_replace_string_with_special_characters_is_encoded():
    out = rewrite_text('{"a": 1}', replace={('a',): 'x",:}\\u0000'})
    assert json.loads(out) == {"a": 'x",:}\\u0000'}
    # Plain string rules never inject raw text.
    assert '\\u0000' in out


def test_replace_scalar_types():
    out = rewrite_text('{"n": 1, "s": "a", "b": false}',
                       replace={('n',): 42, ('s',): None, ('b',): 1.5})
    assert json.loads(out) == {"n": 42, "s": None, "b": 1.5}


def test_replace_root_value():
    assert json.loads(rewrite_text('[1, 2]', replace={(): {"k": 1}})) == {"k": 1}
    assert json.loads(rewrite_text('5', replace={(): -0.0})) == 0.0


def test_array_delete_items_is_separate_from_field_delete():
    out = rewrite_text('[10, 20, 30, 40]',
                       delete_items={(): [0, 2, 3]})
    assert json.loads(out) == [20]


def test_array_delete_items_nested():
    out = rewrite_text('{"xs": [1, 2, 3, 4], "ys": ["a", "b"]}',
                       delete_items={('xs',): [1], ('ys',): [0]})
    assert json.loads(out) == {"xs": [1, 3, 4], "ys": ["b"]}


def test_delete_all_array_items_leaves_empty_array():
    assert rewrite_text('[1, 2, 3]', delete_items={(): [0, 1, 2]}) == '[]'


def test_original_indices_are_used_for_array_deletion():
    out = rewrite_text('[0, 1, 2, 3, 4]', delete_items={(): [1, 3]})
    assert json.loads(out) == [0, 2, 4]


def test_field_delete_rule_on_array_item_is_rejected():
    with pytest.raises(ValueError):
        rewrite_text('[1]', delete={(0,): True})


def test_array_delete_rule_naming_field_is_rejected():
    # An items rule applied where an object (not an array) stands is rejected,
    # whether the offending value is an object or a scalar.
    with pytest.raises(ValueError):
        rewrite_text('{"0": 1}', delete_items={(): [0]})
    with pytest.raises(ValueError):
        rewrite_text('{"a": {"0": 1}}', delete_items={('a',): [0]})
    with pytest.raises(ValueError):
        rewrite_text('{"obj": 1}', delete_items={(): [0]})


# --------------------------------------------------------------------------
# Literal preservation: untouched numbers keep their exact lexeme
# --------------------------------------------------------------------------

DOC_WITH_LEXEMES = (
    '{"big": 1e+09, "negzero": -0, "kept2": 1.50, "exp": 0E-3, '
    '"nested": {"x": -12.340E+5, "arr": [1, -0.0, 2.00]}, '
    '"drop": 999, "repl": 7}'
)
RULES = {'delete': {('drop',): True}, 'replace': {('repl',): 1}}


def test_untouched_number_lexemes_are_verbatim():
    out = rewrite_text(DOC_WITH_LEXEMES, **RULES)
    json.loads(out)
    source = collect_number_lexemes(DOC_WITH_LEXEMES)
    result = collect_number_lexemes(out)
    changed = {('drop',), ('repl',)}
    for path in source.keys() & result.keys():
        if path in changed:
            continue
        assert result[path] == source[path], (path, source[path], result[path])
    assert ('drop',) not in result
    assert result[('repl',)] == '1'
    # the replaced field carries an encoder-produced number, not the input
    assert ('nested', 'x') in source
    assert result[('repl',)] == '1'


def test_specific_lexeme_examples():
    out = rewrite_text('{"a": 1e+09, "b": -0}')
    assert '1e+09' in out and '-0' in out
    assert json.loads(out)['a'] == 1e9
    assert json.loads(out)['b'] == 0


def test_unselected_strings_keep_raw_spelling():
    text = '{"a": "\\u4e2d\\n\\t\\"\\\\\\/end", "b": 2}'
    out = rewrite_text(text, delete={('b',): True})
    assert json.loads(out) == {"a": "中\n\t\"\\/end"}
    assert '\\u4e2d' in out  # original escape spelling retained


# --------------------------------------------------------------------------
# Whitespace normalization
# --------------------------------------------------------------------------

def test_whitespace_is_normalized_but_values_are_not():
    out = rewrite_text('  { "a" :\n\t 1 ,  "b" : [ 1 ,  2 ] }  ')
    assert out == '{"a":1,"b":[1,2]}'


def test_numbers_survive_whitespace_normalization():
    out = rewrite_text('{ "a" : 1e+09 , "b" :\n-0 }')
    assert '1e+09' in out and '-0' in out


# --------------------------------------------------------------------------
# Streaming / chunked input
# --------------------------------------------------------------------------

def split_text(text, sizes):
    start = 0
    for size in sizes:
        while start < len(text):
            end = min(start + size, len(text))
            yield text[start:end]
            start = end
            break
    while start < len(text):
        end = min(start + sizes[-1], len(text))
        yield text[start:end]
        start = end
    assert start == len(text)


def test_rewrite_iterable_matches_single_pass():
    expected = rewrite_text(DOC_WITH_LEXEMES, **RULES)
    for sizes in ([1], [2], [3], [1, 3, 2, 7], [1000]):
        chunks = list(split_text(DOC_WITH_LEXEMES, sizes))
        out = ''.join(rewrite_iterable(iter(chunks), **RULES))
        assert out == expected


def test_cross_chunk_escapes_for_every_split_point():
    text = ('{"s": "a\\u4e2d\\n\\\\\\"x\\ty", "n": 1e+09, '
            '"drop": "gone", "more": "-0 inside"}')
    rules = {'delete': {('drop',): True}, 'replace': {('n',): -0.0}}
    expected = ''.join(rewrite_iterable([text], **rules))
    json.loads(expected)
    for cut in range(1, len(text)):
        out = ''.join(rewrite_iterable(iter([text[:cut], text[cut:]]), **rules))
        assert out == expected, cut


def test_cross_chunk_per_character():
    text = '{"a": "é\\u00e9", "b": 1e+09, "drop": 0}'
    out = ''.join(rewrite_iterable(iter(list(text)),
                                   delete={('drop',): True}))
    assert json.loads(out) == {"a": "éé", "b": 1e9}
    assert '1e+09' in out


def test_empty_feeds_between_chunks():
    rewriter = JsonRewriter(delete={('b',): True})
    assert rewriter.feed('') == ''
    out = rewriter.feed('{"a": 1,') + rewriter.feed('')
    out += rewriter.feed(' "b": 2}') + rewriter.finish()
    assert json.loads(out) == {"a": 1}


def test_output_is_emitted_incrementally():
    rewriter = JsonRewriter(delete={('last',): True})
    head = rewriter.feed('{"first": [1, 2, 3], "x": 1,')
    assert head.startswith('{"first":[1,2,3],"x":1')
    rest = rewriter.feed(' "last": 9}') + rewriter.finish()
    assert json.loads(head + rest) == {"first": [1, 2, 3], "x": 1}


def test_rewrite_file_roundtrip(tmp_path):
    source = tmp_path / 'in.json'
    target = tmp_path / 'out.json'
    source.write_text('{"a": 1e+09, "b": -0, "c": [1, 2]}', encoding='utf-8')
    with source.open(encoding='utf-8') as src, \
            target.open('w', encoding='utf-8') as dst:
        rewrite_file(src, dst, chunk_size=3,
                     delete={('b',): True},
                     delete_items={('c',): [0]})
    out = target.read_text(encoding='utf-8')
    assert json.loads(out) == {"a": 1e9, "c": [2]}
    assert '1e+09' in out


# --------------------------------------------------------------------------
# Encoder discipline and rule validation
# --------------------------------------------------------------------------

def test_non_encodable_replacement_is_rejected():
    with pytest.raises(ValueError):
        JsonRewriter(replace={('a',): object()})
    with pytest.raises(ValueError):
        JsonRewriter(replace={('a',): float('nan')})


def test_replace_and_delete_same_path_rejected():
    with pytest.raises(ValueError):
        JsonRewriter(delete={('a',): True}, replace={('a',): 1})


def test_overlapping_rules_rejected():
    with pytest.raises(ValueError):
        JsonRewriter(replace={('a',): 1}, delete={('a', 'b'): True})
    with pytest.raises(ValueError):
        JsonRewriter(delete={('a', 'b'): True, },
                     replace={('a',): None})


def test_bad_path_types_rejected():
    with pytest.raises(TypeError):
        JsonRewriter(delete={'a': True})
    with pytest.raises(TypeError):
        JsonRewriter(delete={('a', 1.5): True})
    with pytest.raises(TypeError):
        JsonRewriter(delete_items={('a', 1.5): [0]})
    with pytest.raises(TypeError):
        JsonRewriter(delete_items={('a',): 'not-an-iterable'})
    with pytest.raises(ValueError):
        JsonRewriter(delete_items={('a',): [-1]})
    with pytest.raises(ValueError):
        JsonRewriter(delete={('a', 0): True})
    with pytest.raises(TypeError):
        JsonRewriter(delete_items={'not-a-tuple': [0]})


def test_cannot_delete_root():
    with pytest.raises(ValueError):
        JsonRewriter(delete={(): True})


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------

def test_invalid_json_raises():
    with pytest.raises(JSONError):
        rewrite_text('{"a": }')
    with pytest.raises(JSONError):
        rewrite_text('[1,]')
    with pytest.raises(JSONError):
        rewrite_text('{"a" 1}')
    with pytest.raises(JSONError):
        rewrite_text('01')
    with pytest.raises(JSONError):
        rewrite_text('tru')
    with pytest.raises(JSONError):
        rewrite_text('"unterminated')


def test_incomplete_input_raises_on_finish():
    rewriter = JsonRewriter()
    rewriter.feed('{"a": 1')
    with pytest.raises(JSONError):
        rewriter.finish()


def test_empty_input_raises():
    with pytest.raises(JSONError):
        rewrite_text('')
    with pytest.raises(JSONError):
        rewrite_text('   ')


def test_trailing_data_raises():
    with pytest.raises(JSONError):
        rewrite_text('{} {}')


def test_chunks_must_be_str():
    rewriter = JsonRewriter()
    with pytest.raises(TypeError):
        rewriter.feed(b'{"a": 1}')


def test_delete_field_whose_value_is_array():
    out = rewrite_text('{"keep": 1, "arr": [1, 2, 3], "tail": 4}',
                       delete={('arr',): True})
    assert json.loads(out) == {"keep": 1, "tail": 4}


def test_delete_field_whose_value_is_nested_array_or_object():
    out = rewrite_text('{"arr": [1, [2, [3]]], "obj": {"k": {"j": [0]}}}',
                       delete={('arr',): True, ('obj',): True})
    assert json.loads(out) == {}


def test_delete_array_element_that_is_object_or_array():
    out = rewrite_text('[{"a": 1}, [1, 2], 3]', delete_items={(): [0, 1]})
    assert json.loads(out) == [3]


def test_replace_field_with_array_value():
    out = rewrite_text('{"arr": [1, 2, 3]}', replace={('arr',): {"x": 1}})
    assert json.loads(out) == {"arr": {"x": 1}}


def test_delete_items_overlapping_value_rule_rejected():
    with pytest.raises(ValueError):
        JsonRewriter(delete={('a',): True}, delete_items={('a',): [0]})
    with pytest.raises(ValueError):
        JsonRewriter(replace={('a', 0): 1}, delete_items={('a',): [0]})


def test_finish_idempotent_and_accepts_late_feeds():
    rewriter = JsonRewriter()
    out = rewriter.feed('{"a": 1}')
    assert rewriter.feed(' ') == ''
    assert rewriter.finish() == ''
    assert rewriter.finish() == ''
    assert out == '{"a":1}'


def test_replaced_string_raw_spelling_comes_from_encoder():
    # Even a value that looks like raw JSON fragments must be encoded safely.
    out = rewrite_text('{"a": 1}', replace={('a',): '1}, "injected": true, "'})
    assert json.loads(out) == {"a": '1}, "injected": true, "'}
