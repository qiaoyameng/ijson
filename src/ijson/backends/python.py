'''
Pure-python parsing backend.
'''
from json.decoder import scanstring
import re

from ijson import common, utils, utils35
import codecs


LEXEME_RE = re.compile(r'[a-z0-9eE\.\+-]+|\S')
INTEGER_RE = re.compile(r'-?[0-9]+')
UNARY_LEXEMES = set('[]{},')
EOF = -1, None


class UnexpectedSymbol(common.JSONError):
    def __init__(self, symbol, pos):
        super(UnexpectedSymbol, self).__init__(
            'Unexpected symbol %r at %d' % (symbol, pos)
        )


@utils.coroutine
def utf8_encoder(target):
    decoder = codecs.getincrementaldecoder('utf-8')()
    decode = decoder.decode
    send = target.send
    while True:
        try:
            final = False
            bdata = (yield)
        except GeneratorExit:
            final = True
            bdata = b''
        try:
            sdata = decode(bdata, final)
        except UnicodeDecodeError as e:
            try:
                target.close()
            except:
                pass
            raise common.IncompleteJSONError(e)
        except:
            try:
                target.close()
            except:
                pass
            raise
        if sdata:
            send(sdata)
        elif not bdata:
            target.close()
            break

@utils.coroutine
def Lexer(target):
    """
    Parses lexemes out of the incoming content, and sends them to parse_value.
    A special EOF result is sent when the data source has been exhausted to
    give parse_value the possibility of raising custom exceptions due to missing
    content.
    """
    try:
        data = (yield)
    except GeneratorExit:
        data = ''
    buf = data
    pos = 0
    discarded = 0
    send = target.send
    while True:
        match = LEXEME_RE.search(buf, pos)
        if match:
            lexeme = match.group()
            if lexeme == '"':
                pos = match.start()
                start = pos + 1
                while True:
                    try:
                        end = buf.index('"', start)
                        escpos = end - 1
                        while buf[escpos] == '\\':
                            escpos -= 1
                        if (end - escpos) % 2 == 0:
                            start = end + 1
                        else:
                            break
                    except ValueError:
                        try:
                            data = (yield)
                        except GeneratorExit:
                            data = ''
                        if not data:
                            raise common.IncompleteJSONError('Incomplete string lexeme')
                        buf += data
                send((discarded + pos, buf[pos:end + 1]))
                pos = end + 1
            else:
                while lexeme not in UNARY_LEXEMES and match.end() == len(buf):
                    try:
                        data = (yield)
                    except GeneratorExit:
                        data = ''
                    if not data:
                        break
                    buf += data
                    match = LEXEME_RE.search(buf, pos)
                    lexeme = match.group()
                send((discarded + match.start(), lexeme))
                pos = match.end()
        else:
            # Don't ask data from an already exhausted source
            if data:
                try:
                    data = (yield)
                except GeneratorExit:
                    data = ''
            if not data:
                # Normally should raise StopIteration, but can raise
                # IncompleteJSONError too, which is the point of sending EOF
                try:
                    target.send(EOF)
                except StopIteration:
                    pass
                break
            discarded += len(buf)
            buf = data
            pos = 0


# Parsing states
_PARSE_VALUE = 0
_PARSE_ARRAY_ELEMENT_END = 1
_PARSE_OBJECT_KEY = 2
_PARSE_OBJECT_END = 3

# Token kinds sent by the SegmentedLexer to parse_value_segmented
_LEXEME = 'lexeme'
_STRING_START = 'string_start'
_STRING_DATA = 'string_data'
_STRING_END = 'string_end'
SEG_EOF = -1, 'eof', None

# Default maximum number of characters per 'string_chunk' event
DEFAULT_STRING_CHUNK_SIZE = 64 * 1024

_STRING_ESCAPES = {
    '"': '"',
    '\\': '\\',
    '/': '/',
    'b': '\b',
    'f': '\f',
    'n': '\n',
    'r': '\r',
    't': '\t',
}
_HEX_DIGITS = frozenset('0123456789abcdefABCDEF')


def _is_hex4(digits):
    return len(digits) == 4 and all(c in _HEX_DIGITS for c in digits)


def _decode_string(target, buf, pos, discarded):
    """
    Sub-coroutine of SegmentedLexer decoding the contents of a string from
    `buf`, starting at `pos` (just past the opening quote). Decoded content
    is sent to `target` as (pos, 'string_data', str) tokens, followed by a
    single (pos, 'string_end', None) token. More input is requested via
    (yield) when needed. Returns (buf, pos, discarded) with `pos` just past
    the closing quote.
    """
    send = target.send
    text_start = pos
    while True:
        if pos == len(buf):
            # Out of input: flush pending raw text, discard everything
            # already consumed (so memory stays bounded) and ask for more
            if text_start < pos:
                send((discarded + text_start, _STRING_DATA, buf[text_start:pos]))
            discarded += pos
            buf = buf[pos:]
            pos = text_start = 0
            try:
                data = (yield)
            except GeneratorExit:
                data = ''
            if not data:
                raise common.IncompleteJSONError('Incomplete string lexeme')
            buf += data
            continue
        char = buf[pos]
        if char == '"':
            if text_start < pos:
                send((discarded + text_start, _STRING_DATA, buf[text_start:pos]))
            send((discarded + pos, _STRING_END, None))
            return buf, pos + 1, discarded
        if char == '\\':
            if text_start < pos:
                send((discarded + text_start, _STRING_DATA, buf[text_start:pos]))
            # Make sure the whole escape sequence is buffered; a \uXXXX
            # escape is 6 characters long
            while pos + 2 > len(buf) or (buf[pos + 1] == 'u' and pos + 6 > len(buf)):
                discarded += pos
                buf = buf[pos:]
                pos = 0
                try:
                    data = (yield)
                except GeneratorExit:
                    data = ''
                if not data:
                    raise common.IncompleteJSONError('Incomplete string lexeme')
                buf += data
            esc = buf[pos + 1]
            if esc in _STRING_ESCAPES:
                send((discarded + pos, _STRING_DATA, _STRING_ESCAPES[esc]))
                pos += 2
            elif esc == 'u':
                digits = buf[pos + 2:pos + 6]
                if not _is_hex4(digits):
                    raise common.JSONError('Invalid \\uXXXX escape sequence')
                code = int(digits, 16)
                pos += 6
                if 0xD800 <= code <= 0xDBFF:
                    # A high surrogate: check whether a matching low
                    # surrogate escape follows, which might still be on
                    # its way in a later input chunk
                    while (buf[pos:pos + 2] == '\\u'[:len(buf) - pos]
                           and pos + 6 > len(buf)):
                        discarded += pos
                        buf = buf[pos:]
                        pos = 0
                        try:
                            data = (yield)
                        except GeneratorExit:
                            data = ''
                        if not data:
                            raise common.IncompleteJSONError('Incomplete string lexeme')
                        buf += data
                    if buf[pos:pos + 2] == '\\u':
                        digits = buf[pos + 2:pos + 6]
                        if not _is_hex4(digits):
                            raise common.JSONError('Invalid \\uXXXX escape sequence')
                        code2 = int(digits, 16)
                        if 0xDC00 <= code2 <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (code2 - 0xDC00)
                            pos += 6
                        # Otherwise the high surrogate is emitted on its
                        # own and the second escape is reprocessed later,
                        # matching json.decoder.scanstring
                send((discarded + pos, _STRING_DATA, chr(code)))
            else:
                raise common.JSONError('Invalid \\escape: %r' % (esc,))
            text_start = pos
        elif ord(char) < 0x20:
            raise common.JSONError('Invalid control character %r in string' % (char,))
        else:
            pos += 1


@utils.coroutine
def SegmentedLexer(target):
    """
    Like Lexer, but strings are decoded on the fly and reported in pieces:
    a (pos, 'string_start', None) token, zero or more (pos, 'string_data',
    str) tokens with non-empty decoded content, and a (pos, 'string_end',
    None) token. Everything else is reported as (pos, 'lexeme', lexeme)
    tokens. A special SEG_EOF result is sent when the data source has been
    exhausted.
    """
    try:
        data = (yield)
    except GeneratorExit:
        data = ''
    buf = data
    pos = 0
    discarded = 0
    send = target.send
    while True:
        match = LEXEME_RE.search(buf, pos)
        if match:
            lexeme = match.group()
            if lexeme == '"':
                pos = match.start()
                send((discarded + pos, _STRING_START, None))
                buf, pos, discarded = yield from _decode_string(
                    target, buf, pos + 1, discarded
                )
            else:
                while lexeme not in UNARY_LEXEMES and match.end() == len(buf):
                    try:
                        data = (yield)
                    except GeneratorExit:
                        data = ''
                    if not data:
                        break
                    buf += data
                    match = LEXEME_RE.search(buf, pos)
                    lexeme = match.group()
                send((discarded + match.start(), _LEXEME, lexeme))
                pos = match.end()
        else:
            # Don't ask data from an already exhausted source
            if data:
                try:
                    data = (yield)
                except GeneratorExit:
                    data = ''
            if not data:
                # Normally should raise StopIteration, but can raise
                # IncompleteJSONError too, which is the point of sending EOF
                try:
                    target.send(SEG_EOF)
                except StopIteration:
                    pass
                break
            discarded += len(buf)
            buf = data
            pos = 0

# infinity singletons for overflow checks
inf = float("inf")
neg_inf = float("-inf")

@utils.coroutine
def parse_value(target, multivalue, use_float):
    """
    Parses results coming out of the Lexer into ijson events, which are sent to
    `target`. A stack keeps track of the type of object being parsed at the time
    (a value, and object or array -- the last two being values themselves).

    A special EOF result coming from the Lexer indicates that no more content is
    expected. This is used to check for incomplete content and raise the
    appropriate exception, which wouldn't be possible if the Lexer simply closed
    this co-routine (either explicitly via .close(), or implicitly by itself
    finishing and decreasing the only reference to the co-routine) since that
    causes a GeneratorExit exception that cannot be replaced with a custom one.
    """

    state_stack = [_PARSE_VALUE]
    pop = state_stack.pop
    push = state_stack.append
    send = target.send
    prev_pos, prev_symbol = None, None
    to_number = common.integer_or_float if use_float else common.integer_or_decimal
    while True:

        if prev_pos is None:
            pos, symbol = (yield)
            if (pos, symbol) == EOF:
                if state_stack:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                break
        else:
            pos, symbol = prev_pos, prev_symbol
            prev_pos, prev_symbol = None, None
        try:
            state = state_stack[-1]
        except IndexError:
            if multivalue:
                state = _PARSE_VALUE
                push(state)
            else:
                raise common.JSONError('Additional data found')
        assert state_stack

        if state == _PARSE_VALUE:
            # Simple, common cases
            if symbol == 'null':
                send(('null', None))
                pop()
            elif symbol == 'true':
                send(('boolean', True))
                pop()
            elif symbol == 'false':
                send(('boolean', False))
                pop()
            elif symbol[0] == '"':
                send(('string', parse_string(symbol)))
                pop()
            # Array start
            elif symbol == '[':
                send(('start_array', None))
                pos, symbol = (yield)
                if (pos, symbol) == EOF:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                if symbol == ']':
                    send(('end_array', None))
                    pop()
                else:
                    prev_pos, prev_symbol = pos, symbol
                    push(_PARSE_ARRAY_ELEMENT_END)
                    push(_PARSE_VALUE)
            # Object start
            elif symbol == '{':
                send(('start_map', None))
                pos, symbol = (yield)
                if (pos, symbol) == EOF:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                if symbol == '}':
                    send(('end_map', None))
                    pop()
                else:
                    prev_pos, prev_symbol = pos, symbol
                    push(_PARSE_OBJECT_KEY)
            # A number
            else:
                # JSON numbers can't contain leading zeros
                if ((len(symbol) > 1 and symbol[0] == '0' and symbol[1] not in ('e', 'E', '.')) or
                    (len(symbol) > 2 and symbol[0:2] == '-0' and symbol[2] not in ('e', 'E', '.'))):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,))
                # JSON numbers can't start with a plus sign
                if symbol[0] == '+':
                    raise common.JSONError(f'Invalid JSON number: {symbol}')
                # Fractions need a leading digit and must be followed by a digit
                dot = symbol.find('.')
                if dot != -1 and not (symbol[dot - 1:dot].isdigit() and symbol[dot + 1:dot + 2].isdigit()):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,))
                try:
                    number = to_number(symbol)
                    if number == inf or number == neg_inf:
                        raise common.JSONError("float overflow: %s" % (symbol,))
                except:
                    if 'true'.startswith(symbol) or 'false'.startswith(symbol) or 'null'.startswith(symbol):
                        raise common.IncompleteJSONError('Incomplete JSON content')
                    # a well-formed integer that int() still refused is one
                    # longer than sys.get_int_max_str_digits(); let that
                    # error through as the other backends do
                    if INTEGER_RE.fullmatch(symbol):
                        raise
                    raise UnexpectedSymbol(symbol, pos)
                else:
                    send(('number', number))
                    pop()

        elif state == _PARSE_OBJECT_KEY:
            if symbol[0] != '"':
                raise UnexpectedSymbol(symbol, pos)
            send(('map_key', parse_string(symbol)))
            pos, symbol = (yield)
            if (pos, symbol) == EOF:
                raise common.IncompleteJSONError('Incomplete JSON content')
            if symbol != ':':
                raise UnexpectedSymbol(symbol, pos)
            state_stack[-1] = _PARSE_OBJECT_END
            push(_PARSE_VALUE)

        elif state == _PARSE_OBJECT_END:
            if symbol == ',':
                state_stack[-1] = _PARSE_OBJECT_KEY
            elif symbol != '}':
                raise UnexpectedSymbol(symbol, pos)
            else:
                send(('end_map', None))
                pop()
                pop()

        elif state == _PARSE_ARRAY_ELEMENT_END:
            if symbol == ',':
                state_stack[-1] = _PARSE_ARRAY_ELEMENT_END
                push(_PARSE_VALUE)
            elif symbol != ']':
                raise UnexpectedSymbol(symbol, pos)
            else:
                send(('end_array', None))
                pop()
                pop()


def parse_string(symbol):
    return scanstring(symbol, 1)[0]


def _string_events(send, string_chunk_size):
    """
    Sub-coroutine of parse_value_segmented turning the tokens of a string
    value into ('string_chunk', str) events of at most string_chunk_size
    characters each, followed by a final ('end_string', None) event. The
    intermediate buffer never holds more than string_chunk_size characters
    once a token has been processed.
    """
    chunk = []
    chunk_len = 0
    while True:
        pos, kind, symbol = (yield)
        if kind == _STRING_DATA:
            chunk.append(symbol)
            chunk_len += len(symbol)
            while chunk_len >= string_chunk_size:
                text = ''.join(chunk)
                send(('string_chunk', text[:string_chunk_size]))
                text = text[string_chunk_size:]
                chunk = [text]
                chunk_len = len(text)
        elif kind == _STRING_END:
            if chunk_len:
                send(('string_chunk', ''.join(chunk)))
            send(('end_string', None))
            return
        else:
            raise common.IncompleteJSONError('Incomplete JSON content')


def _map_key(send, map_key_max_length):
    """
    Sub-coroutine of parse_value_segmented accumulating the tokens of an
    object key into a single ('map_key', str) event, whose length is
    limited to map_key_max_length characters (if given).
    """
    parts = []
    key_len = 0
    while True:
        pos, kind, symbol = (yield)
        if kind == _STRING_DATA:
            parts.append(symbol)
            key_len += len(symbol)
            if map_key_max_length is not None and key_len > map_key_max_length:
                raise common.JSONError(
                    'Map key exceeds the maximum length of %d characters'
                    % (map_key_max_length,)
                )
        elif kind == _STRING_END:
            send(('map_key', ''.join(parts)))
            return
        else:
            raise common.IncompleteJSONError('Incomplete JSON content')


@utils.coroutine
def parse_value_segmented(target, multivalue=False, use_float=False,
                          string_chunk_size=DEFAULT_STRING_CHUNK_SIZE,
                          map_key_max_length=None):
    """
    Like parse_value, but string values are reported as a sequence of
    events: ('start_string', None), zero or more non-empty
    ('string_chunk', str) events of at most string_chunk_size characters
    each, and a final ('end_string', None) event. Map keys are still
    reported as whole ('map_key', str) events, but their length is limited
    to map_key_max_length characters (no limit if None).
    """
    state_stack = [_PARSE_VALUE]
    pop = state_stack.pop
    push = state_stack.append
    send = target.send
    prev = None
    to_number = common.integer_or_float if use_float else common.integer_or_decimal
    while True:

        if prev is None:
            pos, kind, symbol = (yield)
            if (pos, kind, symbol) == SEG_EOF:
                if state_stack:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                break
        else:
            pos, kind, symbol = prev
            prev = None
        try:
            state = state_stack[-1]
        except IndexError:
            if multivalue:
                state = _PARSE_VALUE
                push(state)
            else:
                raise common.JSONError('Additional data found')
        assert state_stack

        if state == _PARSE_VALUE:
            # A string, reported in chunks
            if kind == _STRING_START:
                send(('start_string', None))
                yield from _string_events(send, string_chunk_size)
                pop()
            # Simple, common cases
            elif symbol == 'null':
                send(('null', None))
                pop()
            elif symbol == 'true':
                send(('boolean', True))
                pop()
            elif symbol == 'false':
                send(('boolean', False))
                pop()
            # Array start
            elif symbol == '[':
                send(('start_array', None))
                pos, kind, symbol = (yield)
                if (pos, kind, symbol) == SEG_EOF:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                if kind == _LEXEME and symbol == ']':
                    send(('end_array', None))
                    pop()
                else:
                    prev = (pos, kind, symbol)
                    push(_PARSE_ARRAY_ELEMENT_END)
                    push(_PARSE_VALUE)
            # Object start
            elif symbol == '{':
                send(('start_map', None))
                pos, kind, symbol = (yield)
                if (pos, kind, symbol) == SEG_EOF:
                    raise common.IncompleteJSONError('Incomplete JSON content')
                if kind == _LEXEME and symbol == '}':
                    send(('end_map', None))
                    pop()
                else:
                    prev = (pos, kind, symbol)
                    push(_PARSE_OBJECT_KEY)
            # A number
            else:
                # JSON numbers can't contain leading zeros
                if ((len(symbol) > 1 and symbol[0] == '0' and symbol[1] not in ('e', 'E', '.')) or
                    (len(symbol) > 2 and symbol[0:2] == '-0' and symbol[2] not in ('e', 'E', '.'))):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,))
                # JSON numbers can't start with a plus sign
                if symbol[0] == '+':
                    raise common.JSONError(f'Invalid JSON number: {symbol}')
                # Fractions need a leading digit and must be followed by a digit
                dot = symbol.find('.')
                if dot != -1 and not (symbol[dot - 1:dot].isdigit() and symbol[dot + 1:dot + 2].isdigit()):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,))
                try:
                    number = to_number(symbol)
                    if number == inf or number == neg_inf:
                        raise common.JSONError("float overflow: %s" % (symbol,))
                except:
                    if 'true'.startswith(symbol) or 'false'.startswith(symbol) or 'null'.startswith(symbol):
                        raise common.IncompleteJSONError('Incomplete JSON content')
                    # a well-formed integer that int() still refused is one
                    # longer than sys.get_int_max_str_digits(); let that
                    # error through as the other backends do
                    if INTEGER_RE.fullmatch(symbol):
                        raise
                    raise UnexpectedSymbol(symbol, pos)
                else:
                    send(('number', number))
                    pop()

        elif state == _PARSE_OBJECT_KEY:
            if kind != _STRING_START:
                raise UnexpectedSymbol(symbol, pos)
            yield from _map_key(send, map_key_max_length)
            pos, kind, symbol = (yield)
            if (pos, kind, symbol) == SEG_EOF:
                raise common.IncompleteJSONError('Incomplete JSON content')
            if kind != _LEXEME or symbol != ':':
                raise UnexpectedSymbol(symbol, pos)
            state_stack[-1] = _PARSE_OBJECT_END
            push(_PARSE_VALUE)

        elif state == _PARSE_OBJECT_END:
            if symbol == ',':
                state_stack[-1] = _PARSE_OBJECT_KEY
            elif symbol != '}':
                raise UnexpectedSymbol(symbol, pos)
            else:
                send(('end_map', None))
                pop()
                pop()

        elif state == _PARSE_ARRAY_ELEMENT_END:
            if symbol == ',':
                state_stack[-1] = _PARSE_ARRAY_ELEMENT_END
                push(_PARSE_VALUE)
            elif symbol != ']':
                raise UnexpectedSymbol(symbol, pos)
            else:
                send(('end_array', None))
                pop()
                pop()


def basic_parse_basecoro(target, multiple_values=False, allow_comments=False,
                         use_float=False):
    '''
    Iterator yielding unprefixed events.

    Parameters:

    - file: a readable file-like object with JSON input
    '''
    if allow_comments:
        raise ValueError("Comments are not supported by the python backend")
    return utf8_encoder(Lexer(parse_value(target, multiple_values, use_float)))


def basic_parse_segmented_basecoro(target, multiple_values=False,
                                   allow_comments=False, use_float=False,
                                   string_chunk_size=DEFAULT_STRING_CHUNK_SIZE,
                                   map_key_max_length=None):
    '''
    Like basic_parse_basecoro, but string values are dispatched as
    ('start_string', None) / ('string_chunk', str) / ('end_string', None)
    event sequences instead of a single ('string', str) event. Each
    'string_chunk' event carries at most string_chunk_size characters.
    Map keys are still dispatched as whole ('map_key', str) events, limited
    to map_key_max_length characters (no limit if None).
    '''
    if allow_comments:
        raise ValueError("Comments are not supported by the python backend")
    if string_chunk_size < 1:
        raise ValueError("string_chunk_size must be a positive integer")
    return utf8_encoder(SegmentedLexer(parse_value_segmented(
        target, multiple_values, use_float,
        string_chunk_size, map_key_max_length
    )))


def _segmented_pipeline(config):
    return (
        (basic_parse_segmented_basecoro, [], config),
    )


def basic_parse_segmented_coro(target, **config):
    '''A coroutine dispatching segmented parsing events to `target`'''
    return utils.chain(target, *_segmented_pipeline(config))


def basic_parse_segmented_gen(file_obj, buf_size=64*1024, **config):
    '''Iterator yielding segmented unprefixed events from a file-like object'''
    return utils.coros2gen(
        common.file_source(file_obj, buf_size=buf_size),
        *_segmented_pipeline(config)
    )


def basic_parse_segmented_async(f, buf_size=64*1024, **config):
    '''Async iterable yielding segmented unprefixed events from an async file'''
    return utils35.async_iterable(f, buf_size, *_segmented_pipeline(config))


def basic_parse_segmented(source, buf_size=64*1024, **config):
    '''
    Iterator (or async iterable, depending on `source`) yielding segmented
    unprefixed events: string values are reported as
    ('start_string', None) / ('string_chunk', str) / ('end_string', None)
    event sequences instead of a single ('string', str) event.
    '''
    source = common._get_source(source)
    if common.is_async_file(source):
        return basic_parse_segmented_async(source, buf_size=buf_size, **config)
    elif common.is_file(source):
        return basic_parse_segmented_gen(source, buf_size=buf_size, **config)
    raise ValueError("Unknown source type: %r" % type(source))


common.enrich_backend(globals(), c_comments=False)
