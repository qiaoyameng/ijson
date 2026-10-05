'''
Streaming, per-object filtering of JSON array items.

This module implements ``filtered_items``, a streaming entry point that
builds only the objects of a JSON array that satisfy a set of declarative
field conditions.  Conditions are restricted to scalar comparisons against
configured fields (see ``FieldCondition``); no arbitrary expression code is
ever evaluated.

While the conditions of an object cannot be decided yet, the parsing events
needed to reconstruct the object are kept in a bounded in-memory buffer that
spills to a temporary file (in a caller-provided directory) once a
configurable threshold is exceeded.  Objects that fail the filter are never
fully materialized in memory.
'''
import decimal
import operator
import pickle
import tempfile

from ijson import common


_SCALAR_TYPES = (str, int, float, bool, type(None), decimal.Decimal)

_CONTAINS = lambda value, options: value in options


class FieldCondition:
    '''
    A declarative filter condition comparing the scalar value of a single
    field of a JSON object against a fixed reference value.

    ``field`` is the dot-separated path of the field relative to the filtered
    object (e.g. ``'status'`` or ``'meta.state'``).  ``op`` is one of
    ``'eq'``, ``'ne'``, ``'lt'``, ``'le'``, ``'gt'``, ``'ge'`` or ``'in'``.
    ``value`` must be a scalar (``str``, ``int``, ``float``, ``bool``,
    ``None`` or ``Decimal``), or -- for the ``'in'`` operator only -- a
    collection of scalars.

    Conditions are purely declarative: no callables, expressions or arbitrary
    code are accepted or executed.
    '''

    _OPERATORS = {
        'eq': operator.eq,
        'ne': operator.ne,
        'lt': operator.lt,
        'le': operator.le,
        'gt': operator.gt,
        'ge': operator.ge,
        'in': _CONTAINS,
    }

    def __init__(self, field, op, value):
        if not isinstance(field, str) or not field:
            raise ValueError('Condition field must be a non-empty string')
        if op not in self._OPERATORS:
            raise ValueError(
                'Unsupported operator %r; expected one of %s'
                % (op, ', '.join(sorted(self._OPERATORS)))
            )
        if op == 'in':
            if (not isinstance(value, (tuple, list, set, frozenset)) or
                    not all(isinstance(option, _SCALAR_TYPES) for option in value)):
                raise ValueError(
                    "The 'in' operator requires a collection of scalar values"
                )
            value = frozenset(value)
        elif not isinstance(value, _SCALAR_TYPES):
            raise ValueError(
                'Condition value must be a scalar, got %r' % type(value)
            )
        self.field = field
        self.op = op
        self.value = value

    def evaluate(self, value):
        '''True if the scalar ``value`` satisfies this condition.'''
        try:
            return bool(self._OPERATORS[self.op](value, self.value))
        except TypeError:
            # Incomparable types (e.g. ``'a' < 1``) never match
            return False

    def __repr__(self):
        return 'FieldCondition(%r, %r, %r)' % (self.field, self.op, self.value)


def _normalize_conditions(conditions):
    if isinstance(conditions, (FieldCondition, tuple)):
        conditions = [conditions]
    normalized = []
    for condition in conditions:
        if isinstance(condition, tuple):
            condition = FieldCondition(*condition)
        if not isinstance(condition, FieldCondition):
            raise TypeError(
                'Conditions must be FieldCondition instances, got %r'
                % type(condition)
            )
        normalized.append(condition)
    if not normalized:
        raise ValueError('At least one condition is required')
    return normalized


class _SpillBuffer:
    '''
    A bounded buffer for parsing events.

    Up to ``max_in_memory`` events are kept in a list; once that threshold is
    exceeded the buffer is spilled to a temporary file created under
    ``temp_dir`` (the system default if ``None``).  The temporary file is
    visible in the directory while in use and is deleted as soon as
    ``close()`` is called.  ``close()`` is idempotent.
    '''

    def __init__(self, max_in_memory, temp_dir=None):
        if max_in_memory < 0:
            raise ValueError('max_in_memory must be >= 0')
        self._max_in_memory = max_in_memory
        self._temp_dir = temp_dir
        self._events = []
        self._file = None
        self._spilled_count = 0

    @property
    def spilled(self):
        '''True if the buffer has been spilled to a temporary file.'''
        return self._file is not None

    def append(self, event):
        if self._file is not None:
            pickle.dump(event, self._file, pickle.HIGHEST_PROTOCOL)
            self._spilled_count += 1
        elif len(self._events) >= self._max_in_memory:
            self._file = tempfile.NamedTemporaryFile(
                mode='w+b', prefix='ijson-filter-', dir=self._temp_dir
            )
            for buffered in self._events:
                pickle.dump(buffered, self._file, pickle.HIGHEST_PROTOCOL)
            self._spilled_count = len(self._events)
            self._events = []
            pickle.dump(event, self._file, pickle.HIGHEST_PROTOCOL)
            self._spilled_count += 1
        else:
            self._events.append(event)

    def __iter__(self):
        if self._file is None:
            yield from self._events
            return
        self._file.flush()
        self._file.seek(0)
        for _ in range(self._spilled_count):
            yield pickle.load(self._file)

    def close(self):
        self._events = []
        if self._file is not None:
            # NamedTemporaryFile deletes the file on close
            self._file.close()
            self._file = None
            self._spilled_count = 0


_REJECTED = object()

_DEPTH_DELTAS = {
    'start_map': 1,
    'start_array': 1,
    'end_map': -1,
    'end_array': -1,
}

_SCALAR_EVENTS = ('null', 'boolean', 'number', 'string')


def _build_passthrough_value(events, event, value, map_type):
    '''Builds a non-object value (scalar or array) found at the item prefix.'''
    if event != 'start_array':
        return value
    builder = common.ObjectBuilder(map_type=map_type)
    builder.event(event, value)
    depth = 1
    for _prefix, event, value in events:
        builder.event(event, value)
        depth += _DEPTH_DELTAS.get(event, 0)
        if depth == 0:
            return builder.value
    raise common.IncompleteJSONError('Incomplete JSON content')


def _capture_map(events, conditions, on_duplicate, max_buffered_events,
                 temp_dir, map_type):
    '''
    Consumes the events of a single object (whose ``start_map`` event has
    already been seen) and returns the built object if it passes the filter,
    or ``_REJECTED`` otherwise.

    While undecided, events are accumulated in a bounded, spilling buffer.
    With ``on_duplicate='first'`` the first occurrence of each condition
    field is decisive, so a failing object is rejected as soon as any
    condition fails and its remaining events are skipped without buffering.
    With ``on_duplicate='last'`` the last occurrence wins and the decision
    is only final once the object ends.
    '''
    buffer = _SpillBuffer(max_buffered_events, temp_dir)
    buffer.append(('start_map', None))
    field_to_conditions = {}
    for index, condition in enumerate(conditions):
        field_to_conditions.setdefault(condition.field, []).append(index)
    condition_count = len(conditions)
    seen = [False] * condition_count
    values = [None] * condition_count
    path = [None]
    depth = 1
    decided = None
    builder = None
    try:
        for _prefix, event, value in events:
            depth += _DEPTH_DELTAS.get(event, 0)
            if decided is False:
                # Rejected: skip the rest of the object without buffering
                if depth == 0:
                    return _REJECTED
                continue
            if decided is True:
                # Accepted: build directly, no more buffering
                builder.event(event, value)
                if depth == 0:
                    return builder.value
                continue
            buffer.append((event, value))
            if event == 'map_key':
                path[-1] = value
            elif event == 'start_map':
                path.append(None)
            elif event == 'start_array':
                path.append('item')
            elif event in ('end_map', 'end_array'):
                path.pop()
            elif event in _SCALAR_EVENTS:
                field = '.'.join(path)
                for index in field_to_conditions.get(field, ()):
                    if on_duplicate == 'last' or not seen[index]:
                        seen[index] = True
                        values[index] = value
                if on_duplicate == 'first':
                    if any(
                        seen[index]
                        and not conditions[index].evaluate(values[index])
                        for index in range(condition_count)
                    ):
                        decided = False
                        buffer.close()
                    elif all(seen):
                        decided = True
                        builder = common.ObjectBuilder(map_type=map_type)
                        for buffered_event, buffered_value in buffer:
                            builder.event(buffered_event, buffered_value)
                        buffer.close()
            if depth == 0:
                if all(
                    seen[index] and conditions[index].evaluate(values[index])
                    for index in range(condition_count)
                ):
                    builder = common.ObjectBuilder(map_type=map_type)
                    for buffered_event, buffered_value in buffer:
                        builder.event(buffered_event, buffered_value)
                    return builder.value
                return _REJECTED
        raise common.IncompleteJSONError('Incomplete JSON content')
    finally:
        buffer.close()


def _filtered_items_events_gen(events, prefix, conditions, on_duplicate,
                               max_buffered_events, temp_dir, map_type):
    events = iter(events)
    try:
        for current, event, value in events:
            if current != prefix:
                continue
            if event == 'start_map':
                result = _capture_map(
                    events, conditions, on_duplicate,
                    max_buffered_events, temp_dir, map_type
                )
                if result is not _REJECTED:
                    yield result
            else:
                # Non-object items cannot be filtered by field conditions
                # and are passed through.
                yield _build_passthrough_value(events, event, value, map_type)
    finally:
        close = getattr(events, 'close', None)
        if close is not None:
            close()


def filtered_items_events(events, prefix, conditions, on_duplicate='last',
                          max_buffered_events=10000, temp_dir=None,
                          map_type=None):
    '''
    Filters items produced by a ``(prefix, event, value)`` event stream (as
    generated by ``ijson.parse``), yielding only the objects found under
    ``prefix`` that satisfy all the given conditions.

    ``conditions`` is a ``FieldCondition`` (or an iterable of them, with
    ``(field, op, value)`` tuples accepted as shorthand); all conditions must
    hold for an object to be yielded.  Objects missing a condition field do
    not match.

    ``on_duplicate`` selects how repeated condition fields within a single
    object are handled: ``'last'`` (the default) lets the last occurrence
    decide, so an early ``accepted`` value is correctly overridden by a later
    ``rejected`` one; ``'first'`` fixes each field's value at its first
    occurrence, which allows failing objects to be discarded early.

    While the filter cannot be decided, the events needed to reconstruct the
    current object are kept in a buffer holding at most
    ``max_buffered_events`` events in memory; beyond that threshold the
    buffer spills to a temporary file under ``temp_dir`` (the system default
    temporary directory if ``None``).  Rejected objects are never fully
    materialized in memory.  Temporary files are closed and removed when the
    object is finished, when the returned generator is closed early, or when
    the event source fails.
    '''
    conditions = _normalize_conditions(conditions)
    if on_duplicate not in ('first', 'last'):
        raise ValueError(
            "on_duplicate must be 'first' or 'last', got %r" % (on_duplicate,)
        )
    if max_buffered_events < 0:
        raise ValueError('max_buffered_events must be >= 0')
    return _filtered_items_events_gen(
        events, prefix, conditions, on_duplicate,
        max_buffered_events, temp_dir, map_type
    )


def _make_filtered_items(backend):
    def filtered_items(source, prefix, conditions, on_duplicate='last',
                       max_buffered_events=10000, temp_dir=None, map_type=None,
                       buf_size=64*1024, **config):
        '''
        Like ``items``, but yields only the objects under ``prefix`` that
        satisfy all the given field conditions.  See
        ``ijson.filtering.filtered_items_events`` for the full documentation.
        Asynchronous sources are not supported.
        '''
        source = common._get_source(source)
        if common.is_async_file(source):
            raise ValueError(
                'filtered_items does not support asynchronous sources'
            )
        events = backend['parse'](source, buf_size=buf_size, **config)
        return filtered_items_events(
            events, prefix, conditions,
            on_duplicate=on_duplicate,
            max_buffered_events=max_buffered_events,
            temp_dir=temp_dir,
            map_type=map_type,
        )
    return filtered_items
