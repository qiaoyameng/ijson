'''
Streaming JSON rewriter.

The document is transformed *while it is being read*: input is tokenized as
chunks arrive and rewritten text is produced immediately, without ever
building the whole document in memory. Only values explicitly selected by
path rules are touched; every other token -- most importantly the raw
lexeme of every number -- is copied verbatim, so spellings such as
``1e+09`` or ``-0`` survive untouched.

Three kinds of rules, each with its own semantics:

- ``delete``: remove an *object field* identified by a path tuple.
- ``replace``: substitute the value at a path with a Python object, encoded
  through the strict standard encoder (arbitrary strings are never injected
  as raw JSON).
- ``delete_items``: remove elements of an *array*, by original index. This is
  deliberately separate from ``delete`` -- deleting an array element is not
  the same operation as deleting an object field.

Paths are tuples of object keys (``str``) and array indices (``int``), for
example ``("users", 3, "name")``.
'''

import json
import re

from ijson.common import JSONError

__all__ = [
    'JsonRewriter',
    'rewrite_text',
    'rewrite_iterable',
    'rewrite_file',
]

_NEED = object()
_EOF = object()

_NUMBER_RE = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z')
_NUMBER_CHARS = frozenset('0123456789-+.eE')
_WS = frozenset(' \t\n\r')
_ESCAPES = frozenset('"\\/bfnrtu')
_HEX = frozenset('0123456789abcdefABCDEF')


def _normalize_paths(rules, name):
    if not isinstance(rules, dict):
        raise TypeError('%s rules must be a dict keyed by path tuples' % name)
    for path in rules:
        _check_path(path, name)
    return rules


def _check_path(path, name):
    if not isinstance(path, tuple):
        raise TypeError('%s path must be a tuple of str/int' % name)
    for segment in path:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise TypeError(
                '%s path segments must be str or int, got %r' % (name, segment))


def _is_prefix(ancestor, path):
    return len(ancestor) < len(path) and path[:len(ancestor)] == ancestor


def _strict_dumps(value, path):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            'replacement at path %r is not strict-JSON encodable: %s'
            % (path, exc))


class JsonRewriter:
    '''Incrementally rewrite a JSON document under explicit path rules.

    :param delete: ``{path: True}`` for object fields to remove.
    :param replace: ``{path: value}``; ``value`` is JSON-encoded strictly.
    :param delete_items: ``{path: [indices]}`` removing array elements by
        their index in the original array.
    '''

    def __init__(self, delete=None, replace=None, delete_items=None):
        delete = _normalize_paths(dict(delete or {}), 'delete')
        replace_raw = dict(replace or {})
        for path in replace_raw:
            if not isinstance(path, tuple):
                raise TypeError('replace paths must be tuples of str/int')
            for segment in path:
                if isinstance(segment, bool) or not isinstance(segment, (str, int)):
                    raise TypeError(
                        'replace path segments must be str or int, got %r'
                        % (segment,))
        replace = replace_raw

        for path in delete:
            if not path or not isinstance(path[-1], str):
                raise ValueError(
                    'delete path %r does not name an object field; use '
                    'delete_items to remove array elements' % (path,))
        for path in replace:
            if path in delete:
                raise ValueError(
                    'path %r is both deleted and replaced' % (path,))

        value_rules = list(delete) + list(replace)
        for i, first in enumerate(value_rules):
            for second in value_rules[i + 1:]:
                if _is_prefix(first, second) or _is_prefix(second, first):
                    raise ValueError('rules for %r and %r overlap'
                                     % (first, second))

        self._delete = frozenset(delete)
        self._replace = {path: _strict_dumps(value, path)
                         for path, value in replace.items()}

        self._delete_items = {}
        for path, indices in (delete_items or {}).items():
            # ``path`` locates the array; ``indices`` lists original indices.
            if not isinstance(path, tuple):
                raise TypeError('delete_items path must be a tuple of str/int')
            for segment in path:
                if isinstance(segment, bool) or not isinstance(segment, (str, int)):
                    raise TypeError(
                        'delete_items path segments must be str or int, got %r'
                        % (segment,))
            try:
                index_iter = iter(indices)
            except TypeError:
                raise TypeError(
                    'delete_items[%r] must be an iterable of int indices'
                    % (path,))
            index_set = set()
            for index in index_iter:
                if type(index) is not int:
                    raise TypeError(
                        'delete_items[%r] indices must be int, got %r'
                        % (path, index))
                if index < 0:
                    raise ValueError(
                        'delete_items[%r] indices must be non-negative'
                        % (path,))
                index_set.add(index)
            self._delete_items[path] = frozenset(index_set)

        for items_path in self._delete_items:
            for rule_path in value_rules:
                if items_path == rule_path \
                        or _is_prefix(items_path, rule_path) \
                        or _is_prefix(rule_path, items_path):
                    raise ValueError(
                        'delete_items rule at %r overlaps with value rule at '
                        '%r' % (items_path, rule_path))

        self._out = []
        self._frames = []
        self._skip_depth = 0
        self._skip_root = False
        self._document_done = False
        self._finished = False
        self._gen = self._tokenize()
        self._next = None

    # -- public API ---------------------------------------------------------

    def feed(self, chunk):
        '''Feed one ``str`` chunk; return rewritten text available so far.'''
        if not isinstance(chunk, str):
            raise TypeError('chunks must be str')
        if not chunk or self._document_done:
            # Anything after the completed document is checked at finish();
            # extra feed() calls are harmless and emit nothing.
            return ''
        self._drive(chunk)
        return self._drain()

    def finish(self):
        '''Signal end of input and return any remaining rewritten text.'''
        if self._finished:
            return ''
        self._drive(_EOF)
        self._finished = True
        if not self._document_done:
            raise JSONError('incomplete JSON input')
        return self._drain()

    def _drain(self):
        text = ''.join(self._out)
        self._out = []
        return text

    # -- driver -------------------------------------------------------------

    def _drive(self, data):
        gen = self._gen
        send = self._next
        self._next = None
        while True:
            try:
                if send is _NEED:
                    result = gen.send(data)
                    if data is not _EOF:
                        data = None
                else:
                    result = next(gen)
            except StopIteration:
                self._document_done = True
                return
            if result is _NEED:
                if data is None:
                    self._next = _NEED
                    return
                send = _NEED
                continue
            send = result
            self._dispatch(result)
            if self._document_done:
                self._next = _NEED
                return

    # -- tokenizer ----------------------------------------------------------

    def _tokenize(self):
        stream = self._chars()
        pushed = []

        def getc():
            if pushed:
                return pushed.pop()
            try:
                item = yield from self._one(stream)
            except StopIteration:
                raise JSONError('incomplete JSON input')
            return item

        def skip_ws():
            while True:
                char = yield from getc()
                if char is _EOF or char not in _WS:
                    return char

        def read_string():
            raw = '"'
            while True:
                char = yield from getc()
                if char is _EOF:
                    raise JSONError('unterminated string')
                if ord(char) < 0x20:
                    raise JSONError('unescaped control character in string')
                raw += char
                if char == '"':
                    return raw
                if char == '\\':
                    esc = yield from getc()
                    if esc is _EOF or esc not in _ESCAPES:
                        raise JSONError('invalid string escape')
                    raw += esc
                    if esc == 'u':
                        for _ in range(4):
                            hex_char = yield from getc()
                            if hex_char is _EOF or hex_char not in _HEX:
                                raise JSONError('invalid \\u escape')
                            raw += hex_char

        def parse_value(first):
            if first == '{':
                yield ('start_object',)
                char = yield from skip_ws()
                if char == '}':
                    yield ('end',)
                    return
                while True:
                    if char != '"':
                        raise JSONError('expected string object key')
                    yield ('key', (yield from read_string()))
                    if (yield from skip_ws()) != ':':
                        raise JSONError('expected colon after object key')
                    yield from parse_value((yield from skip_ws()))
                    char = yield from skip_ws()
                    if char == '}':
                        yield ('end',)
                        return
                    if char != ',':
                        raise JSONError('expected comma or } in object')
                    char = yield from skip_ws()

            elif first == '[':
                yield ('start_array',)
                char = yield from skip_ws()
                if char == ']':
                    yield ('end',)
                    return
                while True:
                    yield from parse_value(char)
                    char = yield from skip_ws()
                    if char == ']':
                        yield ('end',)
                        return
                    if char != ',':
                        raise JSONError('expected comma or ] in array')
                    char = yield from skip_ws()

            elif first == '"':
                yield ('string', (yield from read_string()))

            elif first == '-' or '0' <= first <= '9':
                raw = first
                while True:
                    char = yield from getc()
                    if char is _EOF:
                        break
                    if char not in _NUMBER_CHARS:
                        pushed.append(char)
                        break
                    raw += char
                if not _NUMBER_RE.match(raw):
                    raise JSONError('invalid number: %s' % raw)
                yield ('number', raw)

            else:
                for literal in ('true', 'false', 'null'):
                    if first == literal[0]:
                        raw = first
                        for expected in literal[1:]:
                            char = yield from getc()
                            if char is _EOF or char != expected:
                                raise JSONError('invalid JSON literal')
                            raw += char
                        yield ('literal', raw)
                        return
                if first is _EOF:
                    raise JSONError('incomplete JSON input')
                raise JSONError('unexpected character %r' % first)

        first = yield from skip_ws()
        if first is _EOF:
            raise JSONError('empty JSON input')
        yield from parse_value(first)
        trailing = yield from skip_ws()
        if trailing is not _EOF:
            raise JSONError('trailing data after JSON document')

    def _one(self, stream):
        try:
            item = next(stream)
            while item is _NEED:
                item = stream.send((yield _NEED))
        except StopIteration:
            raise JSONError('incomplete JSON input')
        return item

    def _chars(self):
        pending = None
        pos = 0
        while True:
            if pending is None or pos == len(pending):
                data = yield _NEED
                pos = 0
                if data is _EOF:
                    yield _EOF
                    return
                pending = data
            yield pending[pos]
            pos += 1

    # -- rewriting engine ---------------------------------------------------

    def _current_path(self):
        if not self._frames:
            return ()
        frame = self._frames[-1]
        if frame[0] == 'object':
            return tuple(frame[4]) + (frame[3],)
        return tuple(frame[4]) + (frame[2],)

    def _array_removed(self, path):
        if not path:
            return False
        array_path = tuple(path[:-1])
        indices = self._delete_items.get(array_path)
        return (indices is not None and isinstance(path[-1], int)
                and path[-1] in indices)

    def _dispatch(self, event):
        name = event[0]

        if self._skip_depth:
            if name == 'start_object' or name == 'start_array':
                self._skip_depth += 1
            elif name == 'end':
                self._skip_depth -= 1
                if self._skip_depth == 0 and self._skip_root:
                    self._skip_root = False
                    self._document_done = True
            return

        if name == 'key':
            frame = self._frames[-1]
            frame[3] = json.loads(event[1])
            frame[5] = event[1]
            return

        path = self._current_path()

        if name == 'end':
            frame = self._frames.pop()
            self._out.append(']' if frame[0] == 'array' else '}')
            if not self._frames:
                self._document_done = True
            return

        if name == 'start_object' or name == 'start_array':
            is_array = name == 'start_array'

            at_root = not self._frames
            if path in self._delete:
                self._enter_skip(at_root)
                return
            if self._frames and self._frames[-1][0] == 'object' \
                    and tuple(path[:-1]) in self._delete_items:
                raise ValueError(
                    'delete_items rule at %r found object field %r; use '
                    'delete for object fields' % (tuple(path[:-1]), path[-1]))
            if self._array_removed(path):
                self._advance_parent(path, kept=False)
                self._enter_skip(at_root)
                return
            if path in self._replace:
                self._emit_value_prefix(path)
                self._out.append(self._replace[path])
                self._advance_parent(path, kept=True)
                self._enter_skip(at_root)
                return

            self._emit_value_prefix(path)
            self._out.append('[' if is_array else '{')
            self._advance_parent(path, kept=True)
            self._frames.append(
                ['array' if is_array else 'object', False, 0, None,
                 list(path), None])
            return

        # scalar
        if path in self._delete:
            self._advance_parent(path, kept=False)
            return
        if self._frames and self._frames[-1][0] == 'object' \
                and tuple(path[:-1]) in self._delete_items:
            raise ValueError(
                'delete_items rule at %r hits object field %r; use delete '
                'for object fields' % (tuple(path[:-1]), path[-1]))
        if self._array_removed(path):
            self._advance_parent(path, kept=False)
            return
        if path in self._replace:
            self._emit_value_prefix(path)
            self._out.append(self._replace[path])
            self._advance_parent(path, kept=True)
            if not self._frames:
                self._document_done = True
            return
        self._emit_value_prefix(path)
        self._out.append(event[1])
        self._advance_parent(path, kept=True)
        if not self._frames:
            self._document_done = True

    def _enter_skip(self, at_root):
        self._skip_depth = 1
        self._skip_root = at_root

    def _emit_value_prefix(self, path):
        if not self._frames:
            return
        frame = self._frames[-1]
        if frame[1]:
            self._out.append(',')
        if frame[0] == 'object':
            self._out.append(frame[5])
            self._out.append(':')

    def _advance_parent(self, path, kept):
        if not self._frames:
            return
        frame = self._frames[-1]
        if frame[0] == 'array':
            frame[2] += 1
        if kept:
            frame[1] = True


def rewrite_text(text, delete=None, replace=None, delete_items=None):
    '''Rewrite a whole string in one pass (still processed streaming).'''
    rewriter = JsonRewriter(delete=delete, replace=replace,
                            delete_items=delete_items)
    return rewriter.feed(text) + rewriter.finish()


def rewrite_iterable(chunks, delete=None, replace=None, delete_items=None):
    '''Yield rewritten output chunks for an iterable of input chunks.'''
    rewriter = JsonRewriter(delete=delete, replace=replace,
                            delete_items=delete_items)
    for chunk in chunks:
        output = rewriter.feed(chunk)
        if output:
            yield output
    output = rewriter.finish()
    if output:
        yield output


def rewrite_file(infile, outfile, chunk_size=65536, delete=None,
                 replace=None, delete_items=None):
    '''Stream text-mode ``infile`` to text-mode ``outfile``.'''
    rewriter = JsonRewriter(delete=delete, replace=replace,
                            delete_items=delete_items)
    while True:
        chunk = infile.read(chunk_size)
        if not chunk:
            break
        outfile.write(rewriter.feed(chunk))
    outfile.write(rewriter.finish())
