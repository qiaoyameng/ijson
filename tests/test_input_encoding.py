"""
Tests for the ``input_encoding`` option and source-byte-accurate error
locations in the pure-python backend.
"""
import codecs
import itertools

import pytest

import ijson
from ijson import common
from ijson.encodings import InputTranscoder, SUPPORTED_ENCODINGS

from .test_base import JSON, JSON_PARSE_EVENTS


ENCODINGS = ('utf-8', 'utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be')

# A short document containing a non-BMP character (4 UTF-8 bytes, one UTF-16
# surrogate pair, one UTF-32 code unit), BMP multibyte characters and ASCII.
NON_BMP_DOC = '["a💩bñ", {"k": "é"}]'


def _all_routines():
    backend = ijson.get_backend('python')
    return (
        ('basic_parse', backend.basic_parse),
        ('parse', backend.parse),
        ('items', lambda f, **kw: backend.items(f, '', **kw)),
        ('kvitems', lambda f, **kw: backend.kvitems(f, '', **kw)),
    )


@pytest.mark.parametrize('encoding', sorted(SUPPORTED_ENCODINGS))
def test_explicit_encoding_roundtrip(encoding):
    data = NON_BMP_DOC.encode(encoding)
    events = list(ijson.parse(ijson.common._get_source(data),
                              input_encoding=encoding))
    assert events == list(ijson.parse(NON_BMP_DOC.encode('utf-8')))


@pytest.mark.parametrize('encoding', ENCODINGS)
def test_every_byte_boundary_matches_one_shot_decode(encoding):
    '''
    Splitting the encoded document at every single byte boundary must yield
    exactly the same events as decoding the whole document at once, including
    across code units and UTF-16 surrogate pairs.
    '''
    data = NON_BMP_DOC.encode(encoding)
    expected = list(ijson.parse(ijson.common._get_source(data),
                                input_encoding=encoding))
    for cut in range(1, len(data) + 1):
        chunks = [data[:cut], data[cut:]]
        reader = ijson.from_iter(iter(chunks))
        events = list(ijson.parse(reader, input_encoding=encoding))
        assert events == expected, (encoding, cut)


@pytest.mark.parametrize('encoding', ENCODINGS)
def test_three_way_splits(encoding):
    data = NON_BMP_DOC.encode(encoding)
    expected = list(ijson.parse(ijson.common._get_source(data),
                                input_encoding=encoding))
    for cuts in itertools.combinations(range(1, len(data)), 2):
        chunks = [data[:cuts[0]], data[cuts[0]:cuts[1]], data[cuts[1]:]]
        reader = ijson.from_iter(iter(chunks))
        assert list(ijson.parse(reader, input_encoding=encoding)) == expected


@pytest.mark.parametrize('encoding', ('utf-16-le', 'utf-16-be',
                                      'utf-32-le', 'utf-32-be'))
def test_bom_autodetection(encoding):
    # No input_encoding given: the BOM alone must select the charset.
    bom = {
        'utf-16-le': codecs.BOM_UTF16_LE,
        'utf-16-be': codecs.BOM_UTF16_BE,
        'utf-32-le': codecs.BOM_UTF32_LE,
        'utf-32-be': codecs.BOM_UTF32_BE,
    }[encoding]
    data = bom + NON_BMP_DOC.encode(encoding)
    expected = list(ijson.parse(NON_BMP_DOC.encode('utf-8')))
    assert list(ijson.parse(ijson.common._get_source(data))) == expected


def test_utf8_bom_is_consumed():
    data = codecs.BOM_UTF8 + JSON
    assert list(ijson.parse(ijson.common._get_source(data))) == JSON_PARSE_EVENTS


def test_no_bom_defaults_to_utf8():
    # A BOM-less stream must never be heuristically detected as UTF-16/32.
    assert list(ijson.parse(ijson.common._get_source(JSON))) == JSON_PARSE_EVENTS


@pytest.mark.parametrize('encoding', ('utf-16-le', 'utf-16-be',
                                      'utf-32-le', 'utf-32-be'))
def test_bom_split_across_chunks(encoding):
    bom = {
        'utf-16-le': codecs.BOM_UTF16_LE,
        'utf-16-be': codecs.BOM_UTF16_BE,
        'utf-32-le': codecs.BOM_UTF32_LE,
        'utf-32-be': codecs.BOM_UTF32_BE,
    }[encoding]
    payload = NON_BMP_DOC.encode(encoding)
    for cut in range(1, len(bom)):
        reader = ijson.from_iter(iter([bom[:cut], bom[cut:] + payload]))
        events = list(ijson.parse(reader))
        assert events == list(ijson.parse(NON_BMP_DOC.encode('utf-8')))


@pytest.mark.parametrize('encoding,bom', (
    ('utf-16-le', codecs.BOM_UTF16_BE),
    ('utf-16-be', codecs.BOM_UTF16_LE),
    ('utf-32-le', codecs.BOM_UTF32_BE),
    ('utf-32-be', codecs.BOM_UTF32_LE),
))
def test_declaration_bom_conflict_raises(encoding, bom):
    data = bom + b'\x00' * 4
    with pytest.raises(common.JSONError, match='conflicts with BOM'):
        list(ijson.parse(ijson.common._get_source(data),
                         input_encoding=encoding))


@pytest.mark.parametrize('bad_name', ('utf-16', 'utf-32', 'latin-1', 'ascii'))
def test_unsupported_encoding_name_raises(bad_name):
    with pytest.raises(common.JSONError, match='Unsupported input_encoding'):
        list(ijson.basic_parse(
            ijson.common._get_source(b'{}'), input_encoding=bad_name))


@pytest.mark.parametrize('encoding,chunks', (
    ('utf-16-le', [b'\x3d\xd8\x00\x00']),       # lone high surrogate
    ('utf-16-le', [b'\xa9', b'\xdc\x00\x00']),  # lone low across chunks
    ('utf-16-le', [b'[']),                       # truncated code unit
    ('utf-32-le', [b'\x00\x00']),               # truncated code unit
    ('utf-32-le', [b'\x00\xd8\x00\x00']),       # surrogate code point
))
def test_malformed_multibyte_input_raises(encoding, chunks):
    reader = ijson.from_iter(iter(chunks))
    with pytest.raises(common.JSONError):
        list(ijson.parse(reader, input_encoding=encoding))


def test_truncated_utf8_raises():
    with pytest.raises(common.JSONError):
        list(ijson.parse(ijson.common._get_source(b'["\xd1')))


def test_str_input_rejects_input_encoding():
    import io
    with pytest.raises(common.JSONError, match='str input'):
        list(ijson.parse(io.StringIO('{}'), input_encoding='utf-16-le'))


@pytest.mark.parametrize('routine_name,routine', _all_routines())
def test_error_position_is_source_byte_offset(routine_name, routine):
    '''
    A bad JSON token placed after multibyte characters must report its actual
    byte offset in the original encoded file, not an offset into the decoded
    UTF-8/text representation.
    '''
    prefix = '["ññ", '
    bad = 'invalid]'
    for encoding in ENCODINGS:
        raw_prefix = prefix.encode(encoding)
        data = raw_prefix + bad.encode(encoding)
        expected_pos = len(raw_prefix)
        with pytest.raises(common.JSONError) as excinfo:
            list(routine(ijson.common._get_source(data),
                         input_encoding=encoding))
        assert excinfo.value.pos == expected_pos, (
            routine_name, encoding, excinfo.value.pos, expected_pos)


@pytest.mark.parametrize('encoding', ENCODINGS)
def test_error_position_after_non_bmp_character(encoding):
    prefix = '["💩", '
    bad = 'invalid]'
    raw_prefix = prefix.encode(encoding)
    data = raw_prefix + bad.encode(encoding)
    with pytest.raises(common.JSONError) as excinfo:
        list(ijson.parse(ijson.common._get_source(data),
                         input_encoding=encoding))
    assert excinfo.value.pos == len(raw_prefix)


def test_parse_error_carries_event_path():
    doc = b'{"docs": [{"ok": 1}, {"bad": invalid}]}'
    with pytest.raises(common.JSONError) as excinfo:
        list(ijson.parse(ijson.common._get_source(doc)))
    err = excinfo.value
    assert err.pos == doc.index(b'invalid')
    assert err.prefix == 'docs.item.bad'


def test_basic_parse_error_has_position_but_no_path():
    doc = b'{"a": invalid}'
    with pytest.raises(common.JSONError) as excinfo:
        list(ijson.basic_parse(ijson.common._get_source(doc)))
    assert excinfo.value.pos == doc.index(b'invalid')
    assert excinfo.value.prefix is None


def test_utf16_error_position_and_path():
    doc = '{"a": invalid}'.encode('utf-16-le')
    with pytest.raises(common.JSONError) as excinfo:
        list(ijson.parse(ijson.common._get_source(doc),
                         input_encoding='utf-16-le'))
    err = excinfo.value
    assert err.pos == len('{"a": '.encode('utf-16-le'))
    assert err.prefix == 'a'


def test_other_backends_reject_input_encoding():
    for name in ijson.ALL_BACKENDS:
        if name == 'python':
            continue
        try:
            backend = ijson.get_backend(name)
        except ImportError:
            continue
        with pytest.raises(ValueError, match='input_encoding'):
            list(backend.basic_parse(
                ijson.common._get_source(b'{}'),
                input_encoding='utf-16-le'))


@pytest.mark.parametrize('backend_name',
                         ('yajl', 'yajl2', 'yajl2_c', 'yajl2_cffi'))
def test_reject_helper_for_non_python_backends(backend_name):
    # None must always be accepted (default behavior), anything else rejected.
    common.reject_input_encoding(backend_name, None)
    with pytest.raises(ValueError, match='input_encoding'):
        common.reject_input_encoding(backend_name, 'utf-16-le')


def test_transcoder_offsets_match_manual_reference():
    doc = '["a💩bñ"]'
    for encoding in ENCODINGS:
        data = doc.encode(encoding)
        transcoder = InputTranscoder(encoding)
        text, offsets = transcoder.decode(data)
        text2, offsets2 = transcoder.finish()
        text += text2
        offsets += offsets2
        assert text == doc
        # Independently reconstruct offsets from the raw encoded bytes.
        ref, pos = [], 0
        for char in doc:
            ref.append(pos)
            if encoding == 'utf-8':
                pos += len(char.encode('utf-8'))
            else:
                width = 2 if encoding.startswith('utf-16') else 4
                units = 2 if width == 2 and ord(char) > 0xffff else 1
                pos += width * units
        assert offsets == ref
