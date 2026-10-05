'''
Event-level JSON schema validation for streaming imports.

Validates ``(prefix, event, value)`` parse events against a small, explicit
subset of JSON Schema *before* large objects are built, so structural errors
in bulk imports are reported as early as possible.

Supported schema keywords:

- ``type`` (a string or a list of strings)
- ``properties`` / ``required``
- ``additionalProperties`` (boolean or a schema)
- ``items`` (a single schema)
- ``minItems`` / ``maxItems``
- ``minimum`` / ``maximum``
- ``uniqueItems``

Any other keyword is rejected with :class:`UnsupportedSchemaError` when the
validator is constructed, i.e. before any input is read.
'''
import decimal

from ijson import utils
from ijson.common import ObjectBuilder


SUPPORTED_KEYWORDS = frozenset([
    'type',
    'properties',
    'required',
    'additionalProperties',
    'items',
    'minItems',
    'maxItems',
    'minimum',
    'maximum',
    'uniqueItems',
])

TYPE_NAMES = frozenset([
    'null', 'boolean', 'object', 'array', 'number', 'integer', 'string',
])

#: Default budget (in estimated key-size units) for uniqueItems bookkeeping.
DEFAULT_UNIQUE_ITEMS_BUDGET = 100000

_SCALAR_EVENTS = ('null', 'boolean', 'number', 'string')
_CONTAINER_START_EVENTS = ('start_map', 'start_array')


class UnsupportedSchemaError(ValueError):
    '''
    Raised when a schema uses keywords or constructs outside the supported
    subset. Always raised before any input is read.
    '''


class ValidationError(Exception):
    '''
    Raised when the input violates the schema.

    Carries the structural ``path`` of the offending value (in ijson prefix
    notation, ``''`` for the document root) and the violated ``rule`` (the
    schema keyword that was violated).
    '''
    def __init__(self, path, rule, message):
        self.path = path
        self.rule = rule
        super(ValidationError, self).__init__(
            '%s: %s (violated rule: %s)' % (path or '<root>', message, rule)
        )


class ValidationBudgetExceeded(Exception):
    '''
    Raised when the memory budget for uniqueItems bookkeeping is exhausted.
    Validation cannot continue from this point; the check is never silently
    disabled.
    '''
    def __init__(self, path, message):
        self.path = path
        super(ValidationBudgetExceeded, self).__init__(
            '%s: %s' % (path or '<root>', message)
        )


def _check_schema(schema, where):
    '''Eagerly verifies that *schema* only uses the supported subset.'''
    if not isinstance(schema, dict):
        raise UnsupportedSchemaError(
            'schema at %s must be an object, got %r' % (where, schema)
        )
    for keyword in schema:
        if keyword not in SUPPORTED_KEYWORDS:
            raise UnsupportedSchemaError(
                'unsupported schema keyword %r at %s' % (keyword, where)
            )
    json_type = schema.get('type')
    if json_type is not None:
        types = (json_type,) if isinstance(json_type, str) else json_type
        if not isinstance(types, (list, tuple)) or not types:
            raise UnsupportedSchemaError(
                '"type" at %s must be a string or a non-empty list of strings'
                % where
            )
        for name in types:
            if name not in TYPE_NAMES:
                raise UnsupportedSchemaError(
                    'unsupported type name %r at %s' % (name, where)
                )
    properties = schema.get('properties')
    if properties is not None:
        if not isinstance(properties, dict):
            raise UnsupportedSchemaError(
                '"properties" at %s must be an object' % where
            )
        for name, subschema in properties.items():
            _check_schema(subschema, '%s.properties[%r]' % (where, name))
    required = schema.get('required')
    if required is not None:
        if (not isinstance(required, list) or
                not all(isinstance(name, str) for name in required)):
            raise UnsupportedSchemaError(
                '"required" at %s must be a list of strings' % where
            )
    additional = schema.get('additionalProperties')
    if additional is not None and not isinstance(additional, bool):
        _check_schema(additional, '%s.additionalProperties' % where)
    items = schema.get('items')
    if items is not None:
        if not isinstance(items, dict):
            raise UnsupportedSchemaError(
                'only a single schema is supported for "items" at %s' % where
            )
        _check_schema(items, '%s.items' % where)
    for keyword in ('minItems', 'maxItems'):
        value = schema.get(keyword)
        if value is not None and (
                isinstance(value, bool) or
                not isinstance(value, int) or value < 0):
            raise UnsupportedSchemaError(
                '%r at %s must be a non-negative integer' % (keyword, where)
            )
    for keyword in ('minimum', 'maximum'):
        value = schema.get(keyword)
        if value is not None and (
                isinstance(value, bool) or
                not isinstance(value, (int, float, decimal.Decimal))):
            raise UnsupportedSchemaError(
                '%r at %s must be a number' % (keyword, where)
            )
    unique_items = schema.get('uniqueItems')
    if unique_items is not None and not isinstance(unique_items, bool):
        raise UnsupportedSchemaError(
            '"uniqueItems" at %s must be a boolean' % where
        )


def _canonical_key(value):
    '''
    Returns a hashable canonical key for a JSON value, using JSON value
    semantics: ``1`` and ``1.0`` are equal, but ``true`` is not ``1`` and
    object key order does not matter.
    '''
    if value is None:
        return ('null',)
    if isinstance(value, bool):
        return ('boolean', value)
    if isinstance(value, (int, float, decimal.Decimal)):
        # Python's numeric hash/equality make 1, 1.0 and Decimal('1.0')
        # interchangeable as mapping keys.
        return ('number', value)
    if isinstance(value, str):
        return ('string', value)
    if isinstance(value, list):
        return ('array', tuple(_canonical_key(item) for item in value))
    return ('object', frozenset(
        (key, _canonical_key(item)) for key, item in value.items()
    ))


def _key_cost(key):
    '''Estimates the in-memory size of a canonical key, in abstract units.'''
    tag = key[0]
    payload = key[1] if len(key) > 1 else None
    if tag == 'string':
        return 1 + len(payload)
    if tag == 'array':
        return 1 + sum(_key_cost(item) for item in payload)
    if tag == 'object':
        return 1 + sum(
            len(name) + _key_cost(item) for name, item in payload
        )
    return 1


def _matches_type(type_name, event, value):
    if type_name == 'null':
        return event == 'null'
    if type_name == 'boolean':
        return event == 'boolean'
    if type_name == 'string':
        return event == 'string'
    if type_name == 'number':
        return event == 'number'
    if type_name == 'integer':
        if event != 'number':
            return False
        if isinstance(value, int):
            return True
        return value == int(value)
    if type_name == 'object':
        return event == 'start_map'
    return event == 'start_array'


_EVENT_TYPE_NAMES = {
    'null': 'null',
    'boolean': 'boolean',
    'number': 'number',
    'string': 'string',
    'start_map': 'object',
    'start_array': 'array',
}


class _ObjectContext(object):
    __slots__ = ('schema', 'path', 'key', 'seen')

    def __init__(self, schema, path):
        self.schema = schema
        self.path = path
        self.key = None
        self.seen = set()


class _ArrayContext(object):
    __slots__ = ('schema', 'path', 'count', 'unique', 'unique_cost',
                 'builder', 'builder_depth')

    def __init__(self, schema, path):
        self.schema = schema
        self.path = path
        self.count = 0
        self.unique = set() if schema and schema.get('uniqueItems') else None
        self.unique_cost = 0
        self.builder = None
        self.builder_depth = 0


class EventValidator(object):
    '''
    Validates a stream of ``(prefix, event, value)`` parse events against a
    schema from the supported subset.

    The schema is checked for unsupported keywords at construction time,
    before any input is read. Events are then fed one by one via
    :meth:`event`; the first violation raises :class:`ValidationError` with
    the structural path and the violated rule.

    ``unique_items_budget`` bounds the memory used to remember values seen in
    ``uniqueItems`` arrays (in estimated key-size units, live at any one
    time). If the budget is exceeded, :class:`ValidationBudgetExceeded` is
    raised because validation cannot continue; the check is never silently
    turned off.
    '''
    def __init__(self, schema,
                 unique_items_budget=DEFAULT_UNIQUE_ITEMS_BUDGET):
        _check_schema(schema, '$')
        self.schema = schema
        self.unique_items_budget = unique_items_budget
        self._unique_cost = 0
        self._stack = []

    def event(self, prefix, event, value):
        '''Feeds one parse event into the validator.'''
        self._feed_builders(event, value)
        if event == 'map_key':
            self._on_map_key(prefix, value)
        elif event in _SCALAR_EVENTS:
            self._on_scalar(prefix, event, value)
        elif event == 'start_map':
            self._on_container_start(prefix, event, _ObjectContext)
        elif event == 'start_array':
            self._on_container_start(prefix, event, _ArrayContext)
        elif event == 'end_map':
            self._on_map_end()
        else:  # end_array
            self._on_array_end()

    def _on_map_key(self, prefix, key):
        context = self._stack[-1]
        context.key = key
        context.seen.add(key)
        schema = context.schema
        if not schema:
            return
        properties = schema.get('properties', {})
        if (key not in properties and
                schema.get('additionalProperties', True) is False):
            path = '%s.%s' % (prefix, key) if prefix else key
            raise ValidationError(
                path, 'additionalProperties',
                'unexpected property %r' % (key,)
            )

    def _on_scalar(self, prefix, event, value):
        schema = self._expected_schema()
        if schema:
            self._check_type(prefix, schema, event, value)
            if event == 'number':
                self._check_range(prefix, schema, value)
        self._register_item(prefix, event, value)

    def _on_container_start(self, prefix, event, context_class):
        schema = self._expected_schema()
        if schema:
            self._check_type(prefix, schema, event, None)
        self._register_item(prefix, event, None)
        self._stack.append(context_class(schema, prefix))

    def _on_map_end(self):
        context = self._stack.pop()
        schema = context.schema
        if schema:
            missing = [name for name in schema.get('required', ())
                       if name not in context.seen]
            if missing:
                raise ValidationError(
                    context.path, 'required',
                    'missing required properties: %s'
                    % ', '.join(sorted(missing))
                )

    def _on_array_end(self):
        context = self._stack.pop()
        schema = context.schema
        if schema:
            min_items = schema.get('minItems')
            if min_items is not None and context.count < min_items:
                raise ValidationError(
                    context.path, 'minItems',
                    'array has %d items, fewer than the minimum of %d'
                    % (context.count, min_items)
                )
            max_items = schema.get('maxItems')
            if max_items is not None and context.count > max_items:
                raise ValidationError(
                    context.path, 'maxItems',
                    'array has %d items, more than the maximum of %d'
                    % (context.count, max_items)
                )
        self._unique_cost -= context.unique_cost

    def _expected_schema(self):
        if not self._stack:
            return self.schema
        context = self._stack[-1]
        schema = context.schema
        if not schema:
            return None
        if isinstance(context, _ObjectContext):
            properties = schema.get('properties', {})
            if context.key in properties:
                return properties[context.key]
            additional = schema.get('additionalProperties')
            return additional if isinstance(additional, dict) else None
        return schema.get('items')

    def _check_type(self, path, schema, event, value):
        json_type = schema.get('type')
        if json_type is None:
            return
        types = ((json_type,) if isinstance(json_type, str)
                 else tuple(json_type))
        if any(_matches_type(name, event, value) for name in types):
            return
        expected = ' or '.join(types)
        raise ValidationError(
            path, 'type',
            'expected type %s, got %s'
            % (expected, _EVENT_TYPE_NAMES[event])
        )

    def _check_range(self, path, schema, value):
        minimum = schema.get('minimum')
        if minimum is not None and value < minimum:
            raise ValidationError(
                path, 'minimum',
                'value %r is less than the minimum of %r' % (value, minimum)
            )
        maximum = schema.get('maximum')
        if maximum is not None and value > maximum:
            raise ValidationError(
                path, 'maximum',
                'value %r is greater than the maximum of %r' % (value, maximum)
            )

    def _register_item(self, prefix, event, value):
        if not self._stack:
            return
        context = self._stack[-1]
        if not isinstance(context, _ArrayContext):
            return
        context.count += 1
        if context.unique is None:
            return
        if event in _CONTAINER_START_EVENTS:
            context.builder = ObjectBuilder()
            context.builder.event(event, value)
            context.builder_depth = 1
        else:
            self._record_unique(context, value)

    def _feed_builders(self, event, value):
        for context in self._stack:
            builder = getattr(context, 'builder', None)
            if builder is None:
                continue
            builder.event(event, value)
            if event in _CONTAINER_START_EVENTS:
                context.builder_depth += 1
            elif event in ('end_map', 'end_array'):
                context.builder_depth -= 1
                if context.builder_depth == 0:
                    context.builder = None
                    self._record_unique(context, builder.value)

    def _record_unique(self, context, value):
        key = _canonical_key(value)
        if key in context.unique:
            raise ValidationError(
                context.path, 'uniqueItems',
                'duplicate array item %r' % (value,)
            )
        cost = _key_cost(key)
        if self._unique_cost + cost > self.unique_items_budget:
            raise ValidationBudgetExceeded(
                context.path,
                'uniqueItems bookkeeping exceeded its memory budget '
                '(%d units); validation cannot continue'
                % self.unique_items_budget
            )
        context.unique.add(key)
        context.unique_cost += cost
        self._unique_cost += cost


@utils.coroutine
def validator_basecoro(target, schema,
                       unique_items_budget=DEFAULT_UNIQUE_ITEMS_BUDGET):
    '''
    A coroutine validating ``(prefix, event, value)`` events against *schema*
    and forwarding them unchanged to *target*, so it can be chained into
    coroutine pipelines like the other ``ijson.common`` coroutines.
    '''
    validator = EventValidator(schema, unique_items_budget)
    while True:
        event = (yield)
        validator.event(*event)
        target.send(event)


def validate_events(events, schema,
                    unique_items_budget=DEFAULT_UNIQUE_ITEMS_BUDGET):
    '''
    Returns a generator yielding the ``(prefix, event, value)`` events from
    the *events* iterable after validating them against *schema*.

    The schema is checked for unsupported keywords immediately, before any
    event is consumed from *events*.
    '''
    validator = EventValidator(schema, unique_items_budget)
    return _validated(validator, events)


def _validated(validator, events):
    for prefix, event, value in events:
        validator.event(prefix, event, value)
        yield prefix, event, value
