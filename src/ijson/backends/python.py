'''
Pure-python parsing backend.
'''
from json.decoder import scanstring
import re

from ijson import common, encodings, utils


LEXEME_RE = re.compile(r'[a-z0-9eE\.\+-]+|\S')
INTEGER_RE = re.compile(r'-?[0-9]+')
UNARY_LEXEMES = set('[]{},')
EOF = -1, None


class UnexpectedSymbol(common.JSONError):
    def __init__(self, symbol, pos):
        super(UnexpectedSymbol, self).__init__(
            'Unexpected symbol %r at byte %d' % (symbol, pos),
            pos=pos,
        )


@utils.coroutine
def input_decoder(target, input_encoding=None):
    '''
    Feeds the Lexer with ``(text, offsets)`` pairs where ``offsets[i]`` is the
    byte position of ``text[i]`` in the original (pre-transcoding) stream.

    Byte streams are transcoded incrementally according to ``input_encoding``
    (or a leading BOM when it is not given, defaulting to UTF-8). Text
    streams bypass transcoding entirely: their positions are plain character
    indices, so no byte encoding is ever applied to ``str`` input.
    '''
    send = target.send
    transcoder = None
    text_mode = False
    char_pos = 0
    finished = False
    while True:
        try:
            chunk = (yield)
        except GeneratorExit:
            chunk = None
        if finished:
            return
        if chunk is not None and not chunk:
            # An empty read marks end of stream in generator mode; treat it
            # the same as the coroutine being closed.
            chunk = None
        try:
            if chunk is None:
                finished = True
                if transcoder is not None:
                    sdata, offsets = transcoder.finish()
                    if sdata:
                        send((sdata, offsets))
                # Forward the end-of-stream marker so the Lexer emits its EOF
                # event and parse_value can detect incomplete content. The
                # call propagates IncompleteJSONError directly; StopIteration
                # simply means the downstream pipeline finished cleanly.
                try:
                    send(('', []))
                except StopIteration:
                    return
                return
            if isinstance(chunk, str):
                text_mode = True
                if input_encoding is not None:
                    raise common.JSONError(
                        'input_encoding cannot be used with str input; it only '
                        'applies to byte streams'
                    )
                sdata = chunk
                offsets = list(range(char_pos, char_pos + len(chunk)))
                char_pos += len(chunk)
            else:
                if text_mode:
                    raise common.JSONError(
                        'Cannot mix str and bytes chunks in the same input stream'
                    )
                if transcoder is None:
                    transcoder = encodings.InputTranscoder(input_encoding)
                sdata, offsets = transcoder.decode(chunk)
            if sdata:
                send((sdata, offsets))
        except BaseException:
            # Make sure no downstream coroutine is left suspended: otherwise a
            # later garbage collection would surface its own error (e.g. an
            # IncompleteJSONError on EOF) as an ignored exception.
            try:
                target.close()
            except Exception:
                pass
            raise

@utils.coroutine
def Lexer(target):
    """
    Parses lexemes out of the incoming content, and sends them to parse_value.
    A special EOF result is sent when the data source has been exhausted to
    give parse_value the possibility of raising custom exceptions due to missing
    content.
    """
    try:
        data, data_offsets = (yield)
    except GeneratorExit:
        data, data_offsets = '', []
    if not data:
        # An empty first chunk means the stream is empty. Fall through with an
        # empty buffer; the main loop below delivers the EOF marker after the
        # downstream has had a chance to suspend on its first yield.
        exhausted = True
    else:
        exhausted = False
    buf = data
    buf_offsets = list(data_offsets)
    pos = 0
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
                            data, data_offsets = (yield)
                        except GeneratorExit:
                            data, data_offsets = '', []
                        if not data:
                            raise common.IncompleteJSONError('Incomplete string lexeme')
                        buf += data
                        buf_offsets.extend(data_offsets)
                send((buf_offsets[pos], buf[pos:end + 1]))
                pos = end + 1
            else:
                while lexeme not in UNARY_LEXEMES and match.end() == len(buf):
                    try:
                        data, data_offsets = (yield)
                    except GeneratorExit:
                        data, data_offsets = '', []
                    if not data:
                        exhausted = True
                        break
                    buf += data
                    buf_offsets.extend(data_offsets)
                    match = LEXEME_RE.search(buf, pos)
                    lexeme = match.group()
                send((buf_offsets[match.start()], lexeme))
                pos = match.end()
        else:
            if not exhausted:
                # No lexeme found in the remaining buffer: request more data
                # exactly once, and only treat the stream as exhausted after
                # the source returns an empty chunk.
                try:
                    data, data_offsets = (yield)
                except GeneratorExit:
                    data, data_offsets = '', []
                if not data:
                    exhausted = True
            if exhausted:
                # Normally should raise StopIteration, but can raise
                # IncompleteJSONError too, which is the point of sending EOF
                try:
                    target.send(EOF)
                except StopIteration:
                    pass
                return
            buf = data
            buf_offsets = list(data_offsets)
            pos = 0


# Parsing states
_PARSE_VALUE = 0
_PARSE_ARRAY_ELEMENT_END = 1
_PARSE_OBJECT_KEY = 2
_PARSE_OBJECT_END = 3

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
                raise common.JSONError('Additional data found', pos=pos)
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
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,), pos=pos)
                # JSON numbers can't start with a plus sign
                if symbol[0] == '+':
                    raise common.JSONError(f'Invalid JSON number: {symbol}', pos=pos)
                # Fractions need a leading digit and must be followed by a digit
                dot = symbol.find('.')
                if dot != -1 and not (symbol[dot - 1:dot].isdigit() and symbol[dot + 1:dot + 2].isdigit()):
                    raise common.JSONError('Invalid JSON number: %s' % (symbol,), pos=pos)
                try:
                    number = to_number(symbol)
                    if number == inf or number == neg_inf:
                        raise common.JSONError("float overflow: %s" % (symbol,), pos=pos)
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


def basic_parse_basecoro(target, multiple_values=False, allow_comments=False,
                         use_float=False, input_encoding=None):
    '''
    Iterator yielding unprefixed events.

    Parameters:

    - file: a readable file-like object with JSON input
    - input_encoding: explicit encoding of byte streams; one of utf-8,
      utf-16-le, utf-16-be, utf-32-le or utf-32-be. When None the encoding
      is detected from a leading BOM, defaulting to UTF-8.
    '''
    if allow_comments:
        raise ValueError("Comments are not supported by the python backend")
    return input_decoder(
        Lexer(parse_value(target, multiple_values, use_float)),
        input_encoding=input_encoding,
    )


common.enrich_backend(globals(), c_comments=False)
