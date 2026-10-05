"""Path-rule based streaming JSON rewriter for data masking.

JSON data is read incrementally from a file-like object and rewritten to an
output stream *without* ever building the whole document in memory. The
lexical form of untouched values is preserved verbatim -- this notably keeps
numbers such as ``1e+09`` and ``-0`` exactly as written -- while insignificant
whitespace is normalised away.

Rules address explicit document paths. A path is a tuple of object keys
(``str``) and array indices (``int``); the empty tuple addresses the document
root. For instance, in ``{"a": [{"b": 1}]}`` the number ``1`` lives at
``("a", 0, "b")``.

Object-field deletion and array-element deletion are separate rules; deleting
an object field by key cannot be expressed with the array rule and vice
versa:

* :class:`DeleteField` removes an object field addressed by a path ending in
  a ``str`` key.
* :class:`ReplaceField` substitutes the value at a path with a Python object
  encoded through the standard :mod:`json` module. Raw JSON text is never
  injected, so a replacement given as a string is always emitted as a quoted
  JSON string.
* :class:`DeleteArrayElements` removes elements by index from the array at
  the given path.
"""

import codecs
import json
import re

__all__ = [
    'StreamMaskError',
    'DeleteField',
    'ReplaceField',
    'DeleteArrayElements',
    'rewrite',
    'rewrite_text',
]

_NUMBER_RE = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?')


class StreamMaskError(ValueError):
    """Raised on invalid rules or malformed JSON input."""


def _valid_path_part(part):
    return isinstance(part, str) or (
        isinstance(part, int) and not isinstance(part, bool) and part >= 0)


def _check_path(path):
    path = tuple(path)
    if not all(_valid_path_part(part) for part in path):
        raise StreamMaskError(
            'path parts must be str keys or non-negative int indices: %r'
            % (path,))
    return path


class _Rule:
    def __init__(self, path):
        self.path = _check_path(path)


class DeleteField(_Rule):
    """Delete the object field at *path* (must end in a ``str`` key)."""

    def __init__(self, path):
        super().__init__(path)
        if not self.path or not isinstance(self.path[-1], str):
            raise StreamMaskError(
                'DeleteField path must end in an object key: %r'
                % (self.path,))


class ReplaceField(_Rule):
    """Replace the value at *path* with the JSON encoding of *value*."""

    def __init__(self, path, value):
        super().__init__(path)
        self.value = value


class DeleteArrayElements(_Rule):
    """Delete elements at the given *indices* from the array at *path*."""

    def __init__(self, path, indices):
        super().__init__(path)
        self.indices = frozenset(indices)


# ---------------------------------------------------------------------------
# Streaming lexer
# ---------------------------------------------------------------------------

class _Lexer:
    """Incremental JSON lexer.

    Yields ``(kind, raw_text)`` token tuples where *raw_text* is the exact
    source spelling (preserving number formats such as ``1e+09`` and ``-0``).
    Only a small, bounded tail of the input is buffered at any time.
    """

    def __init__(self, source, chunk_size=65536):
        self._read = source.read
        self._chunk_size = chunk_size
        self._buf = ''
        self._pos = 0
        self._decoder = None

    def _refill(self):
        chunk = self._read(self._chunk_size)
        if not chunk:
            if self._decoder is not None:
                tail = self._decoder.decode(b'', final=True)
                if tail:
                    self._buf += tail
                    return True
            return False
        if isinstance(chunk, bytes):
            if self._decoder is None:
                self._decoder = codecs.getincrementaldecoder('utf-8')()
            chunk = self._decoder.decode(chunk)
        self._buf += chunk
        return True

    def _peek(self):
        while self._pos >= len(self._buf):
            if not self._refill():
                return ''
        return self._buf[self._pos]

    def _advance(self):
        self._pos += 1

    def _compact(self):
        if self._pos:
            self._buf = self._buf[self._pos:]
            self._pos = 0

    def tokens(self):
        while True:
            ch = self._peek()
            if ch == '':
                return
            if ch in ' \t\n\r':
                self._advance()
                self._compact()
            elif ch in '{}[]:,':
                self._advance()
                self._compact()
                yield ch, ch
            elif ch == '"':
                yield 'string', self._read_string()
            elif ch == '-' or '0' <= ch <= '9':
                yield 'number', self._read_number()
            elif ch in 'tfn':
                yield 'literal', self._read_literal()
            else:
                raise StreamMaskError('unexpected character %r' % ch)

    def _read_string(self):
        start = self._pos
        self._advance()  # opening quote
        escaped = False
        while True:
            ch = self._peek()
            if ch == '':
                raise StreamMaskError('unterminated string')
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                self._advance()
                text = self._buf[start:self._pos]
                self._compact()
                return text
            elif ord(ch) < 0x20:
                raise StreamMaskError('unescaped control character in string')
            self._advance()

    def _read_number(self):
        start = self._pos
        while True:
            ch = self._peek()
            if ch == '' or not (ch.isdigit() or ch in '-.+eE'):
                text = self._buf[start:self._pos]
                if _NUMBER_RE.fullmatch(text) is None:
                    raise StreamMaskError('invalid number %r' % text)
                self._compact()
                return text
            self._advance()

    def _read_literal(self):
        first = self._peek()
        target = {'t': 'true', 'f': 'false', 'n': 'null'}[first]
        for expected in target:
            ch = self._peek()
            if ch != expected:
                raise StreamMaskError('invalid literal')
            self._advance()
        self._compact()
        return target


# ---------------------------------------------------------------------------
# Rule compilation
# ---------------------------------------------------------------------------

def _compile(rules):
    delete_fields = {}
    replacements = {}
    delete_indices = {}

    for rule in rules:
        if not isinstance(rule, _Rule):
            raise StreamMaskError('expected a rule instance, got %r' % (rule,))
        if isinstance(rule, DeleteField):
            if rule.path in delete_fields:
                raise StreamMaskError('duplicate DeleteField rule: %r'
                                      % (rule.path,))
            delete_fields[rule.path] = None
        elif isinstance(rule, ReplaceField):
            if rule.path in replacements:
                raise StreamMaskError('duplicate ReplaceField rule: %r'
                                      % (rule.path,))
            # Encode up front through the standards-compliant encoder. A
            # string value is therefore emitted as a quoted JSON string,
            # never injected as raw JSON text.
            try:
                encoded = json.dumps(rule.value, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise StreamMaskError(
                    'replacement value at %r is not JSON encodable: %s'
                    % (rule.path, exc)) from exc
            replacements[rule.path] = encoded
        elif isinstance(rule, DeleteArrayElements):
            if rule.path in delete_indices:
                raise StreamMaskError('duplicate DeleteArrayElements rule: %r'
                                      % (rule.path,))
            delete_indices[rule.path] = frozenset(rule.indices)
        else:  # pragma: no cover - defensive
            raise StreamMaskError('unsupported rule type %r' % type(rule))

    return delete_fields, replacements, delete_indices


# ---------------------------------------------------------------------------
# Rewriting engine
# ---------------------------------------------------------------------------

class _Frame:
    """State for one open object or array."""

    def __init__(self, is_object, location=None):
        self.is_object = is_object
        self.location = location     # parent key (str) or array index (int)
        self.kept_count = 0
        self.index = 0               # original array index of next element
        self.key_raw = None          # raw (quoted) key spelling, objects only
        self.key_decoded = None      # decoded key for path matching
        self.await_value = False     # colon seen, waiting for value
        self.seen_value = False     # source value consumed; comma/close next


class _Rewriter:

    def __init__(self, token_stream, compiled):
        self._it = iter(token_stream)
        self._delete_fields, self._replacements, self._delete_indices = compiled
        self._stack = []
        self._skip_depth = 0
        self._started = False
        self._finished = False

    def run(self, write):
        self._write = write
        for kind, text in self._it:
            if self._skip_depth:
                self._consume(kind)
                continue
            frame = self._stack[-1] if self._stack else None
            if frame is None:
                self._root_value(kind, text)
            elif frame.is_object:
                self._object_token(kind, text)
            else:
                self._array_value(kind, text)
        if not self._finished:
            raise StreamMaskError('unexpected end of input')
        return self

    # -- path helpers -----------------------------------------------------

    def _parent_path(self):
        # Location of the container represented by the top frame, i.e. the
        # path of the value that is currently starting minus its final key
        # or index. Every frame records where it sits inside its parent.
        return tuple(frame.location for frame in self._stack
                     if frame.location is not None)

    # -- root -------------------------------------------------------------

    def _root_value(self, kind, text):
        if self._started:
            raise StreamMaskError('extra data after top-level value')
        self._started = True
        if () in self._replacements:
            self._write(self._replacements[()])
            if kind in ('{', '['):
                # Consume the complete original root value silently.
                depth = 1
                for skip_kind, _ in self._it:
                    if skip_kind in ('{', '['):
                        depth += 1
                    elif skip_kind in ('}', ']'):
                        depth -= 1
                        if depth == 0:
                            break
                if depth:
                    raise StreamMaskError('unexpected end of input')
            for _ in self._it:
                raise StreamMaskError('extra data after top-level value')
            self._finished = True
            return
        if kind in ('{', '['):
            self._write(kind)
            self._stack.append(_Frame(is_object=(kind == '{'), location=None))
        elif kind in ('number', 'string', 'literal'):
            self._write(text)
            self._finished = True
        else:
            raise StreamMaskError('unexpected token %r at root' % kind)

    # -- objects ----------------------------------------------------------

    def _object_token(self, kind, text):
        frame = self._stack[-1]

        if kind == '}':
            if frame.await_value or frame.key_raw is not None:
                raise StreamMaskError('object closed before its value')
            self._write('}')
            self._pop_frame()
            return

        if frame.await_value:
            # The colon has just been consumed; this token starts the value.
            self._start_member_value(kind, text)
            return

        if kind == ',':
            if frame.key_raw is not None:
                raise StreamMaskError('missing value before next field')
            if not frame.seen_value:
                raise StreamMaskError('unexpected "," in object')
            frame.seen_value = False
            return
        if kind == ':':
            if frame.key_raw is None:
                raise StreamMaskError('unexpected ":"')
            frame.await_value = True
            return
        if frame.seen_value:
            raise StreamMaskError('missing "," between object fields')
        if kind != 'string':
            raise StreamMaskError('expected object key, got %r' % kind)
        frame.key_raw = text
        frame.key_decoded = json.loads(text)
        return

    # -- arrays -----------------------------------------------------------

    def _array_value(self, kind, text):
        frame = self._stack[-1]
        if kind == ']':
            self._write(']')
            self._pop_frame()
            return
        if kind == ',':
            if not frame.seen_value:
                raise StreamMaskError('unexpected "," in array')
            frame.seen_value = False
            return
        if frame.seen_value:
            raise StreamMaskError('missing "," between array elements')
        self._start_member_value(kind, text)

    # -- member (field or element) value dispatch -------------------------

    def _start_member_value(self, kind, text):
        frame = self._stack[-1]
        parent_path = self._parent_path()
        frame.seen_value = True  # member exists in the source, kept or not

        if frame.is_object:
            key = frame.key_decoded
            key_raw = frame.key_raw
            value_path = parent_path + (key,)
            frame.key_raw = None
            frame.key_decoded = None
            frame.await_value = False
            if value_path in self._delete_fields:
                self._skip_value(kind)
                return
            self._pending_location = key
            if value_path in self._replacements:
                self._emit_member(frame, key_raw)
                self._write(self._replacements[value_path])
                self._skip_value(kind)
                return
            self._emit_member(frame, key_raw)
            self._keep_value(kind, text)
            return

        index = frame.index
        frame.index += 1
        value_path = parent_path + (index,)
        if index in self._delete_indices.get(parent_path, frozenset()):
            self._skip_value(kind)
            return
        self._pending_location = index
        if value_path in self._replacements:
            self._emit_member(frame, None)
            self._write(self._replacements[value_path])
            self._skip_value(kind)
            return
        self._emit_member(frame, None)
        self._keep_value(kind, text)

    def _emit_member(self, frame, key_raw):
        if frame.kept_count:
            self._write(',')
        if frame.is_object:
            self._write(key_raw)
            self._write(':')
        frame.kept_count += 1

    def _keep_value(self, kind, text):
        if kind in ('{', '['):
            location = self._pending_location
            self._write(text)
            self._stack.append(_Frame(is_object=(kind == '{'),
                                      location=location))
        elif kind not in ('number', 'string', 'literal'):
            raise StreamMaskError('expected value, got %r' % kind)
        else:
            self._write(text)

    def _pop_frame(self):
        self._stack.pop()
        if not self._stack:
            self._finished = True

    # -- skipping removed values ------------------------------------------

    def _skip_value(self, kind):
        """Consume a removed/replaced value starting with token *kind*.

        Replacement text has already been emitted by the caller. A scalar is
        one token; containers are consumed by :meth:`_consume` through the
        skip depth counter.
        """
        if kind in ('{', '['):
            self._skip_depth = 1
        elif kind not in ('number', 'string', 'literal'):
            raise StreamMaskError('expected value, got %r' % kind)

    def _consume(self, kind):
        if kind in ('{', '['):
            self._skip_depth += 1
        elif kind in ('}', ']'):
            self._skip_depth -= 1
            if self._skip_depth:
                return
            if not self._stack:
                self._finished = True

    # -- frame management -------------------------------------------------

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rewrite(source, target, rules=(), chunk_size=65536):
    """Stream *source* JSON to *target* applying *rules*.

    *source* is a text or binary file-like object opened for reading; binary
    streams must use UTF-8. *target* is a text file-like object opened for
    writing. Output whitespace is normalised (all insignificant whitespace
    removed); untouched strings keep their original escaping and untouched
    numbers keep their exact lexical spelling.
    """
    lexer = _Lexer(source, chunk_size=chunk_size)
    compiled = _compile(rules)
    _Rewriter(lexer.tokens(), compiled).run(target.write)
    return target


def rewrite_text(text, rules=(), chunk_size=65536):
    """Convenience wrapper rewriting a string (or bytes) and returning text."""
    import io
    if isinstance(text, bytes):
        source = io.BytesIO(text)
    else:
        source = io.StringIO(text)
    output = io.StringIO()
    return rewrite(source, output, rules, chunk_size=chunk_size).getvalue()
