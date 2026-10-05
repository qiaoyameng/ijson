'''
Event-level validation of JSON streams against a small JSON Schema subset.

The validator consumes the same ``(event, value)`` pairs produced by
``basic_parse`` and checks them against a schema *while* the document is
being read, so structural problems are reported before the whole input has
been consumed (and before any large objects are built).

Supported schema keywords:

- ``type`` (a string or a list of strings)
- ``properties`` / ``required``
- ``additionalProperties`` (boolean or a subschema)
- ``items`` (a single subschema; the tuple form is not supported)
- ``minItems`` / ``maxItems``
- ``minimum`` / ``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum``
  (numeric form)
- ``uniqueItems`` (boolean)

Any other keyword raises :class:`SchemaError` when the validator is
constructed, i.e. before any input is read.

Validation problems raise :class:`ValidationError`, which carries the
structural ``path`` of the offending value and the violated ``rule``.
``required`` is checked when an object closes; ``uniqueItems`` compares
items by JSON value semantics (``1`` equals ``1.0``, but ``true`` is not
``1``, and objects are compared independently of key order).

Tracking values for ``uniqueItems`` costs memory, so a budget
(``unique_memory_budget``, in approximate bytes) bounds what the validator
may keep. Exceeding it raises :class:`ValidationAbortedError` instead of
silently disabling the check.

Example::

    import ijson
    from ijson.validation import EventValidator

    schema = {
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {'id': {'type': 'integer', 'minimum': 0}},
            'required': ['id'],
            'additionalProperties': False,
        },
    }
    validator = EventValidator(schema)
    with open('data.json', 'rb') as f:
        for event, value in ijson.basic_parse(f):
            validator.event(event, value)
'''
import decimal

from ijson import utils


class SchemaError(Exception):
    '''
    Raised when a schema uses keywords or forms outside the supported subset.
    Raised before any input is read.
    '''
    pass


class ValidationError(Exception):
    '''
    Raised when the JSON document violates the schema.

    Attributes:

    - ``path``: structural path of the offending value, e.g. ``$.items[2].id``
    - ``rule``: the schema rule that was violated, e.g. ``'type'``
    '''
    def __init__(self, path, rule, message):
        self.path = path
        self.rule = rule
        super(ValidationError, self).__init__('%s: %s' % (path, message))


class ValidationAbortedError(ValidationError):
    '''
    Raised when validation cannot continue, e.g. because the memory budget
    for ``uniqueItems`` bookkeeping was exceeded.
    '''
    pass


_JSON_TYPES = frozenset([
    'null', 'boolean', 'object', 'array', 'number', 'integer', 'string',
])

_SUPPORTED_KEYWORDS = frozenset([
    'type',
    'properties', 'required', 'additionalProperties',
    'items', 'minItems', 'maxItems',
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'uniqueItems',
])

_DEFAULT_UNIQUE_MEMORY_BUDGET = 1024 * 1024

# Rough per-node cost used to account canonical values kept for uniqueItems
_CANONICAL_BASE_WEIGHT = 32


def _is_number(value):
    return isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool)


def _compile_schema(schema, schema_path):
    '''
    Validates a (sub)schema against the supported subset. Returns the schema
    unchanged; raises SchemaError on anything unsupported.
    '''
    if isinstance(schema, bool) or not isinstance(schema, dict):
        raise SchemaError(
            'unsupported schema at %s: expected an object, got %r' % (schema_path, schema))
    for keyword, value in schema.items():
        if keyword not in _SUPPORTED_KEYWORDS:
            raise SchemaError(
                'unsupported keyword %r at %s' % (keyword, schema_path))
        if keyword == 'type':
            types = value if isinstance(value, list) else [value]
            if not types or any(t not in _JSON_TYPES for t in types):
                raise SchemaError(
                    'unsupported type %r at %s' % (value, schema_path))
        elif keyword == 'properties':
            if not isinstance(value, dict):
                raise SchemaError(
                    "'properties' at %s must be an object" % schema_path)
            for name, subschema in value.items():
                _compile_schema(subschema, '%s.properties.%s' % (schema_path, name))
        elif keyword == 'required':
            if (not isinstance(value, list) or
                    any(not isinstance(name, str) for name in value)):
                raise SchemaError(
                    "'required' at %s must be a list of strings" % schema_path)
        elif keyword == 'additionalProperties':
            if not isinstance(value, bool):
                _compile_schema(value, '%s.additionalProperties' % schema_path)
        elif keyword == 'items':
            if isinstance(value, list):
                raise SchemaError(
                    "tuple form of 'items' is not supported (at %s)" % schema_path)
            _compile_schema(value, '%s.items' % schema_path)
        elif keyword in ('minItems', 'maxItems'):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SchemaError(
                    '%r at %s must be a non-negative integer' % (keyword, schema_path))
        elif keyword in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'):
            if not _is_number(value):
                raise SchemaError(
                    '%r at %s must be a number' % (keyword, schema_path))
        elif keyword == 'uniqueItems':
            if not isinstance(value, bool):
                raise SchemaError(
                    "'uniqueItems' at %s must be a boolean" % schema_path)
    return schema


def _format_path(components):
    '''
    Renders path components as a structural path, e.g. ``$.items[2].id``.
    '''
    parts = ['$']
    for component in components:
        if isinstance(component, int):
            parts.append('[%d]' % component)
        else:
            parts.append('.%s' % component)
    return ''.join(parts)


def _canonical_scalar(event, value):
    '''
    Canonical, hashable representation of a scalar JSON value. Numbers keep
    their numeric value (so 1 and 1.0 compare equal) but are tagged apart
    from booleans (so true is not 1).
    '''
    if event == 'null':
        return ('null', None)
    if event == 'boolean':
        return ('bool', value)
    if event == 'number':
        return ('num', value)
    return ('str', value)


def _canonical_weight(canonical):
    '''Approximate memory footprint of a canonical value.'''
    tag, value = canonical
    if tag == 'str':
        return _CANONICAL_BASE_WEIGHT + len(value)
    if tag == 'arr':
        return _CANONICAL_BASE_WEIGHT + sum(
            _canonical_weight(item) for item in value)
    if tag == 'obj':
        return _CANONICAL_BASE_WEIGHT + sum(
            _CANONICAL_BASE_WEIGHT + len(key) + _canonical_weight(item)
            for key, item in value)
    return _CANONICAL_BASE_WEIGHT


class _ObjectFrame(object):
    __slots__ = ('schema', 'seen_keys', 'pending_key', 'want_canonical', 'canonical')

    def __init__(self, schema, want_canonical):
        self.schema = schema
        self.seen_keys = set()
        self.pending_key = None
        self.want_canonical = want_canonical
        self.canonical = [] if want_canonical else None


class _ArrayFrame(object):
    __slots__ = ('schema', 'count', 'unique', 'seen', 'weight',
                 'want_canonical', 'canonical')

    def __init__(self, schema, want_canonical):
        self.schema = schema
        self.count = 0
        self.unique = bool(schema and schema.get('uniqueItems'))
        self.seen = set() if self.unique else None
        self.weight = 0
        self.want_canonical = want_canonical
        self.canonical = [] if want_canonical else None


class EventValidator(object):
    '''
    Validates a stream of ``(event, value)`` pairs (as produced by
    ``basic_parse``) against a schema from the supported subset.

    The schema is checked when the validator is constructed; anything
    outside the supported subset raises :class:`SchemaError` before any
    input is read.
    '''
    def __init__(self, schema, unique_memory_budget=_DEFAULT_UNIQUE_MEMORY_BUDGET):
        self.schema = _compile_schema(schema, '$')
        self.unique_memory_budget = unique_memory_budget
        self._unique_memory = 0
        self._frames = []
        self._path = []
        self._done = False

    def event(self, event, value):
        '''Feeds one parsing event into the validator.'''
        if self._done:
            raise ValidationError(
                _format_path(self._path), 'document',
                'unexpected event after the top-level value was completed')
        if event == 'map_key':
            self._map_key(value)
        elif event == 'start_map':
            self._start_container(event, 'object', _ObjectFrame)
        elif event == 'start_array':
            self._start_container(event, 'array', _ArrayFrame)
        elif event == 'end_map':
            self._end_object()
        elif event == 'end_array':
            self._end_array()
        else:
            self._scalar(event, value)

    # -- event handlers -------------------------------------------------

    def _map_key(self, key):
        frame = self._frames[-1]
        frame.pending_key = key
        frame.seen_keys.add(key)
        schema = frame.schema
        if schema is not None:
            properties = schema.get('properties', {})
            additional = schema.get('additionalProperties', True)
            if additional is False and key not in properties:
                raise ValidationError(
                    _format_path(self._path + [key]), 'additionalProperties',
                    "additional property %r is not allowed" % key)

    def _start_container(self, event, type_name, frame_class):
        schema = self._expect_child((type_name,))
        want_canonical = self._parent_wants_canonical()
        component = self._child_component()
        self._frames.append(frame_class(schema, want_canonical))
        if component is not None:
            self._path.append(component)

    def _end_object(self):
        frame = self._frames[-1]
        schema = frame.schema
        if schema is not None:
            required = schema.get('required')
            if required:
                missing = [name for name in required if name not in frame.seen_keys]
                if missing:
                    raise ValidationError(
                        _format_path(self._path), 'required',
                        'missing required properties: %s' % ', '.join(
                            repr(name) for name in missing))
        self._finish_container(('obj', frozenset(frame.canonical))
                               if frame.want_canonical else None)

    def _end_array(self):
        frame = self._frames[-1]
        schema = frame.schema
        if schema is not None:
            min_items = schema.get('minItems')
            if min_items is not None and frame.count < min_items:
                raise ValidationError(
                    _format_path(self._path), 'minItems',
                    'array has %d items, fewer than the minimum of %d' % (
                        frame.count, min_items))
        if frame.unique:
            self._unique_memory -= frame.weight
        self._finish_container(('arr', tuple(frame.canonical))
                               if frame.want_canonical else None)

    def _scalar(self, event, value):
        type_names = {
            'null': ('null',),
            'boolean': ('boolean',),
            'string': ('string',),
            'number': ('number', 'integer')
                      if _is_integer(value) else ('number',),
        }[event]
        schema = self._expect_child(type_names)
        if schema is not None and event == 'number':
            self._check_number_ranges(schema, value)
        self._register_child(_canonical_scalar(event, value))

    # -- structural bookkeeping -----------------------------------------

    def _child_component(self):
        if not self._frames:
            return None
        frame = self._frames[-1]
        if isinstance(frame, _ObjectFrame):
            return frame.pending_key
        return frame.count

    def _child_path(self):
        component = self._child_component()
        if component is None:
            return _format_path(self._path)
        return _format_path(self._path + [component])

    def _expect_child(self, type_names):
        '''
        Determines the schema applying to the next child value and checks
        its type. Returns the child schema (or None if unconstrained).
        '''
        schema = self._child_schema()
        if schema is not None:
            allowed = schema.get('type')
            if allowed is not None:
                if isinstance(allowed, str):
                    allowed = (allowed,)
                if not any(type_name in allowed for type_name in type_names):
                    raise ValidationError(
                        self._child_path(), 'type',
                        'expected type %s, got %s' % (
                            ' or '.join(sorted(allowed)), type_names[0]))
        return schema

    def _child_schema(self):
        if not self._frames:
            return self.schema
        frame = self._frames[-1]
        schema = frame.schema
        if schema is None:
            return None
        if isinstance(frame, _ObjectFrame):
            key = frame.pending_key
            properties = schema.get('properties', {})
            if key in properties:
                return properties[key]
            additional = schema.get('additionalProperties', True)
            if isinstance(additional, dict):
                return additional
            return None
        return schema.get('items')

    def _parent_wants_canonical(self):
        if not self._frames:
            return False
        frame = self._frames[-1]
        return (isinstance(frame, _ArrayFrame) and frame.unique) or frame.want_canonical

    def _register_child(self, canonical):
        if not self._frames:
            self._done = True
            return
        frame = self._frames[-1]
        if isinstance(frame, _ObjectFrame):
            if frame.want_canonical:
                frame.canonical.append((frame.pending_key, canonical))
            frame.pending_key = None
        else:
            frame.count += 1
            schema = frame.schema
            if schema is not None:
                max_items = schema.get('maxItems')
                if max_items is not None and frame.count > max_items:
                    raise ValidationError(
                        _format_path(self._path), 'maxItems',
                        'array has more than the maximum of %d items' % max_items)
            if frame.unique:
                if canonical in frame.seen:
                    raise ValidationError(
                        _format_path(self._path + [frame.count - 1]),
                        'uniqueItems',
                        'duplicate array item at index %d' % (frame.count - 1))
                weight = _canonical_weight(canonical)
                if self._unique_memory + weight > self.unique_memory_budget:
                    raise ValidationAbortedError(
                        _format_path(self._path + [frame.count - 1]),
                        'uniqueItems',
                        'memory budget for uniqueItems bookkeeping exceeded; '
                        'cannot continue validation')
                frame.seen.add(canonical)
                frame.weight += weight
                self._unique_memory += weight
            if frame.want_canonical:
                frame.canonical.append(canonical)

    def _finish_container(self, canonical):
        was_root = len(self._frames) == 1
        self._frames.pop()
        if not was_root:
            self._path.pop()
        self._register_child(canonical)

    # -- scalar checks ----------------------------------------------------

    def _check_number_ranges(self, schema, value):
        path = self._child_path()
        minimum = schema.get('minimum')
        if minimum is not None and value < minimum:
            raise ValidationError(
                path, 'minimum', '%r is less than the minimum of %r' % (value, minimum))
        maximum = schema.get('maximum')
        if maximum is not None and value > maximum:
            raise ValidationError(
                path, 'maximum', '%r is greater than the maximum of %r' % (value, maximum))
        exclusive_minimum = schema.get('exclusiveMinimum')
        if exclusive_minimum is not None and value <= exclusive_minimum:
            raise ValidationError(
                path, 'exclusiveMinimum',
                '%r is not greater than %r' % (value, exclusive_minimum))
        exclusive_maximum = schema.get('exclusiveMaximum')
        if exclusive_maximum is not None and value >= exclusive_maximum:
            raise ValidationError(
                path, 'exclusiveMaximum',
                '%r is not less than %r' % (value, exclusive_maximum))


def _is_integer(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, (decimal.Decimal, float)):
        return value == int(value)
    return False


@utils.coroutine
def validator_coro(target, schema, **validator_config):
    '''
    A coroutine validating ``(event, value)`` pairs against ``schema`` and
    forwarding them to ``target``. Can be chained into a coroutine pipeline,
    e.g. via ``ijson.utils.chain``.
    '''
    validator = EventValidator(schema, **validator_config)
    while True:
        event, value = (yield)
        validator.event(event, value)
        target.send((event, value))
