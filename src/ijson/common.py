'''
Backend independent higher level interfaces, common exceptions.
'''
import decimal
import inspect
import io
from collections import namedtuple
import warnings

from ijson import compat, utils, utils35


class JSONError(Exception):
    '''
    Base exception for all parsing errors.
    '''
    pass


class IncompleteJSONError(JSONError):
    '''
    Raised when the parser can't read expected data from a stream.
    '''
    pass


@utils.coroutine
def parse_basecoro(target):
    '''
    A coroutine dispatching parsing events with the information about their
    location with the JSON object tree. Events are tuples
    ``(prefix, type, value)``.

    Available types and values are:

    ('null', None)
    ('boolean', <True or False>)
    ('number', <int or Decimal>)
    ('string', <unicode>)
    ('map_key', <str>)
    ('start_map', None)
    ('end_map', None)
    ('start_array', None)
    ('end_array', None)

    Prefixes represent the path to the nested elements from the root of the JSON
    document. For example, given this document::

        {
          "array": [1, 2],
          "map": {
            "key": "value"
          }
        }

    the parser would yield events:

      ('', 'start_map', None)
      ('', 'map_key', 'array')
      ('array', 'start_array', None)
      ('array.item', 'number', 1)
      ('array.item', 'number', 2)
      ('array', 'end_array', None)
      ('', 'map_key', 'map')
      ('map', 'start_map', None)
      ('map', 'map_key', 'key')
      ('map.key', 'string', u'value')
      ('map', 'end_map', None)
      ('', 'end_map', None)

    '''
    path = []
    while True:
        event, value = yield
        if event == 'map_key':
            prefix = '.'.join(path[:-1])
            path[-1] = value
        elif event == 'start_map':
            prefix = '.'.join(path)
            path.append(None)
        elif event == 'end_map':
            path.pop()
            prefix = '.'.join(path)
        elif event == 'start_array':
            prefix = '.'.join(path)
            path.append('item')
        elif event == 'end_array':
            path.pop()
            prefix = '.'.join(path)
        else: # any scalar value
            prefix = '.'.join(path)
        target.send((prefix, event, value))


class ObjectBuilder:
    '''
    Incrementally builds an object from JSON parser events. Events are passed
    into the `event` function that accepts two parameters: event type and
    value. The object being built is available at any time from the `value`
    attribute.

    Example::

        >>> from io import BytesIO
        >>> from ijson import basic_parse
        >>> from ijson.common import ObjectBuilder

        >>> builder = ObjectBuilder()
        >>> f = BytesIO(b'{"key": "value"}')
        >>> for event, value in basic_parse(f):
        ...     builder.event(event, value)
        >>> builder.value == {'key': 'value'}
        True

    '''
    def __init__(self, map_type=None):
        self.value = None
        self.containers = []
        self.map_type = map_type or dict

    def _add(self, value):
        # Storing the in-progress containers themselves (rather than setter
        # closures, which would reference back to self and create reference
        # cycles) keeps the builder collectable by plain refcounting
        if not self.containers:
            self.value = value
        else:
            top = self.containers[-1]
            if isinstance(top, list):
                top.append(value)
            else:
                top[self.key] = value

    def event(self, event, value):
        if event == 'map_key':
            self.key = value
        elif event == 'start_map':
            mappable = self.map_type()
            self._add(mappable)
            self.containers.append(mappable)
        elif event == 'start_array':
            array = []
            self._add(array)
            self.containers.append(array)
        elif event == 'end_array' or event == 'end_map':
            self.containers.pop()
        else:
            self._add(value)


@utils.coroutine
def items_basecoro(target, prefix, map_type=None):
    '''
    An couroutine dispatching native Python objects constructed from the events
    under a given prefix.
    '''
    while True:
        current, event, value = (yield)
        if current == prefix:
            if event in ('start_map', 'start_array'):
                object_depth = 1
                builder = ObjectBuilder(map_type=map_type)
                while object_depth:
                    builder.event(event, value)
                    current, event, value = (yield)
                    if event in ('start_map', 'start_array'):
                        object_depth += 1
                    elif event in ('end_map', 'end_array'):
                        object_depth -= 1
                target.send(builder.value)
            else:
                target.send(value)


@utils.coroutine
def kvitems_basecoro(target, prefix, map_type=None):
    '''
    An coroutine dispatching (key, value) pairs constructed from the events
    under a given prefix. The prefix should point to JSON objects
    '''
    builder = None
    while True:
        path, event, value = (yield)
        while path == prefix and event == 'map_key':
            object_depth = 0
            key = value
            builder = ObjectBuilder(map_type=map_type)
            path, event, value = (yield)
            if event == 'start_map':
                object_depth += 1
            while (
                (event != 'map_key' or object_depth != 0) and
                (event != 'end_map' or object_depth != -1)):
                builder.event(event, value)
                path, event, value = (yield)
                if event == 'start_map':
                    object_depth += 1
                elif event == 'end_map':
                    object_depth -= 1
            target.send((key, builder.value))


class SubscriptionItem(namedtuple('SubscriptionItem', ('name', 'value'))):
    '''
    An object matched by one of the rules given to ``multi_items`` (or any of
    its variants). ``name`` is the subscription name the matched rule was
    registered with, ``value`` the fully constructed Python object found at
    that location.
    '''
    __slots__ = ()


def _normalize_rule(rule):
    '''
    Turns a single (name, path) rule into a (name, components) tuple. Path
    components are either strings (object keys, including keys with dots) or
    integers (array indices). The string ``item`` is the explicit wildcard for
    array elements. A path given as a string is split on dots, so keys with
    dots must be expressed as a tuple.
    '''
    try:
        name, path = rule
    except (TypeError, ValueError):
        raise ValueError(
            "subscription rules must be (name, path) pairs, got %r" % (rule,)
        )
    if not isinstance(name, str) or not name:
        raise ValueError("subscription names must be non-empty strings")
    if isinstance(path, str):
        components = tuple(path.split('.')) if path else ()
    else:
        try:
            components = tuple(path)
        except TypeError:
            raise ValueError("subscription path must be a string or tuple")
        normalized = []
        for component in components:
            if isinstance(component, int) and not isinstance(component, bool):
                normalized.append(component)
            elif isinstance(component, str):
                normalized.append(component)
            else:
                raise ValueError(
                    "path components must be strings or integers, got %r"
                    % (component,)
                )
        components = tuple(normalized)
    return name, components


def _prepare_rules(rules):
    '''
    Validates subscription rules before any reading happens: every rule needs
    a (name, path) pair, and subscription names cannot repeat.
    '''
    if not rules:
        raise ValueError("at least one subscription rule is required")
    normalized = [_normalize_rule(rule) for rule in rules]
    seen = set()
    for name, _components in normalized:
        if name in seen:
            raise ValueError("duplicated subscription name: %r" % name)
        seen.add(name)
    return normalized


def _rule_matches(components, value_stack):
    '''
    Compares rule components against the current value location. Each entry
    in ``value_stack`` is a ``(component, kind, own_index, next_index)``
    tuple where ``kind`` is one of ``'map'``, ``'array'`` (an array container)
    or ``'element'`` (an array element). Object keys and array containers are
    matched by name; array elements are matched by the explicit ``item``
    marker or by an integer component equal to their live element index.
    '''
    if len(components) != len(value_stack):
        return False
    for rule_component, (component, kind, own_index, _next_index) in zip(
            components, value_stack):
        if kind == 'element':
            if isinstance(rule_component, int):
                if rule_component != own_index:
                    return False
            elif rule_component != 'item':
                return False
        elif kind == 'array' and own_index is not None:
            # An array that is itself an element of an enclosing array: it
            # can be addressed by the item wildcard or by its element index
            if isinstance(rule_component, int):
                if rule_component != own_index:
                    return False
            elif rule_component != 'item':
                return False
        else:
            # A map value or an array container: matched by its own name
            if isinstance(rule_component, int) or rule_component != component:
                return False
    return True


@utils.coroutine
def multi_items_basecoro(target, rules, map_type=None):
    '''
    A coroutine dispatching ``SubscriptionItem`` objects built from the events
    matching any of the given ``(name, path)`` subscription rules.

    The input stream is consumed exactly once. Every matched location gets its
    own builder, so an object already delivered for a child rule is never
    mutated when an enclosing parent rule finishes its own construction, and
    different subscriptions never share their built state.

    Output ordering follows object closing order; when several rules match the
    same location they are emitted in rule declaration order. Memory is bounded
    by the objects currently being constructed.
    '''
    normalized_rules = _prepare_rules(rules)
    # One entry per open value below the document root, as a
    # (component, kind, own_index, next_index) tuple:
    #   map value       -> (key, 'map', None, None)
    #   array container -> (key or 'item', 'array', own element index or None,
    #                       next child index)
    #   array element   -> ('item', 'element', live index, None)
    # The root container pushes no entry; an empty stack is the root location
    # where empty-path rules match.
    value_stack = []
    root_container_open = False
    root_is_array = False
    root_next_index = [0]
    builders_by_depth = {}
    claimed_depths = set()
    pending_key = None

    def matching_names():
        for name, components in normalized_rules:
            if _rule_matches(components, value_stack):
                yield name

    def feed(event, value, depth):
        for open_depth, builders in builders_by_depth.items():
            if open_depth > depth:
                continue
            # Ancestor builders always build their whole value; only foreign
            # constructions inside a claimed subtree are isolated
            nearest_claim = max(
                (d for d in claimed_depths if d <= depth), default=None
            )
            if nearest_claim is not None and open_depth > nearest_claim:
                continue
            for builder, _name in builders:
                builder.event(event, value)

    def enclosing_array_index():
        # Live index of the next/ current element inside the innermost array
        if value_stack and value_stack[-1][1] == 'array':
            return value_stack[-1][3]
        if not value_stack and root_is_array:
            return root_next_index[0]
        return None

    def push_value(event, value, in_array, root_index=None):
        if in_array:
            index = (root_index if not value_stack
                     else value_stack[-1][3])
            value_stack.append(('item', 'element', index, None))
        else:
            value_stack.append((pending_key, 'map', None, None))
        if event == 'start_array':
            component, _kind, own_index, _next = value_stack[-1]
            # Promote the entry to an array container while preserving its
            # identity as an element of its enclosing array
            value_stack[-1] = (component, 'array', own_index, 0)
        return len(value_stack)

    def pop_value():
        value_stack.pop()
        # If the closed value was an element of an enclosing array, advance
        # the array's next-element index
        if value_stack and value_stack[-1][1] == 'array':
            component, kind, own_index, next_index = value_stack[-1]
            value_stack[-1] = (component, kind, own_index, next_index + 1)
        elif not value_stack and root_is_array:
            root_next_index[0] += 1

    def open_value(event, value):
        in_array = enclosing_array_index() is not None
        if in_array and not value_stack:
            return push_value(event, value, True, root_next_index[0])
        return push_value(event, value, in_array)

    try:
        while True:
            current, event, value = (yield)

            if event == 'map_key':
                pending_key = value
                feed(event, value, len(value_stack))
                continue

            if event in ('start_map', 'start_array'):
                if not root_container_open:
                    root_container_open = True
                    root_is_array = event == 'start_array'
                    depth = 0
                else:
                    depth = open_value(event, value)
                new_builders = []
                for name in matching_names():
                    new_builders.append(
                        (ObjectBuilder(map_type=map_type), name)
                    )
                if new_builders:
                    builders_by_depth.setdefault(depth, []).extend(new_builders)
                    claimed_depths.add(depth)
                feed(event, value, depth)
                pending_key = None
                continue

            if event in ('end_map', 'end_array'):
                depth = len(value_stack)
                feed(event, value, depth)
                finished = builders_by_depth.pop(depth, ())
                claimed_depths.discard(depth)
                if depth:
                    pop_value()
                else:
                    root_container_open = False
                    root_is_array = False
                    root_next_index[0] = 0
                # Independent builders per rule: the handed-out value is never
                # touched again by any enclosing construction
                for builder, name in finished:
                    target.send(SubscriptionItem(name, builder.value))
                continue

            # Scalar value
            if not root_container_open and not value_stack:
                # A scalar document root matches empty-path rules at depth 0
                depth = 0
                in_array = False
            else:
                in_array = enclosing_array_index() is not None
                depth = open_value(event, value)
            for name in matching_names():
                target.send(SubscriptionItem(name, value))
            feed(event, value, depth)
            if depth:
                pop_value()
            if not in_array:
                pending_key = None
    except GeneratorExit:
        # Release every in-progress builder so nothing under construction
        # survives a cancellation/close of the pipeline
        builders_by_depth.clear()
        claimed_depths.clear()
        value_stack.clear()
        raise


def integer_or_decimal(str_value):
    '''
    Converts string with a numeric value into an int or a Decimal.
    Used in different backends for consistent number representation.
    '''
    if not ('.' in str_value or 'e' in str_value or 'E' in str_value):
        return int(str_value)
    return decimal.Decimal(str_value)

def integer_or_float(str_value):
    '''
    Converts string with a numeric value into an int or a float.
    Used in different backends for consistent number representation.
    '''
    if not ('.' in str_value or 'e' in str_value or 'E' in str_value):
        return int(str_value)
    return float(str_value)

def number(str_value):
    warnings.warn("number() function will be removed in a later release", DeprecationWarning)
    return integer_or_decimal(str_value)

def file_source(f, buf_size=64*1024):
    '''A generator that yields data from a file-like object'''
    f = compat.bytes_reader(f)
    while True:
        data = f.read(buf_size)
        yield data
        if not data:
            break


def _basic_parse_pipeline(backend, config):
    return (
        (backend['basic_parse_basecoro'], [], config),
    )


def _parse_pipeline(backend, config):
    return (
        (backend['parse_basecoro'], [], {}),
        (backend['basic_parse_basecoro'], [], config)
    )


def _items_pipeline(backend, prefix, map_type, config):
    return (
        (backend['items_basecoro'], (prefix,), {'map_type': map_type}),
        (backend['parse_basecoro'], [], {}),
        (backend['basic_parse_basecoro'], [], config)
    )


def _kvitems_pipeline(backend, prefix, map_type, config):
    return (
        (backend['kvitems_basecoro'], (prefix,), {'map_type': map_type}),
        (backend['parse_basecoro'], [], {}),
        (backend['basic_parse_basecoro'], [], config)
    )


def _multi_items_pipeline(backend, rules, map_type, config):
    return (
        (backend['multi_items_basecoro'], (rules,), {'map_type': map_type}),
        (backend['parse_basecoro'], [], {}),
        (backend['basic_parse_basecoro'], [], config)
    )


def _make_basic_parse_coro(backend):
    def basic_parse_coro(target, **config):
        return utils.chain(
            target,
            *_basic_parse_pipeline(backend, config)
        )
    return basic_parse_coro


def _make_parse_coro(backend):
    def parse_coro(target, **config):
        return utils.chain(
            target,
            *_parse_pipeline(backend, config)
        )
    return parse_coro


def _make_items_coro(backend):
    def items_coro(target, prefix, map_type=None, **config):
        return utils.chain(
            target,
            *_items_pipeline(backend, prefix, map_type, config)
        )
    return items_coro


def _make_kvitems_coro(backend):
    def kvitems_coro(target, prefix, map_type=None, **config):
        return utils.chain(
            target,
            *_kvitems_pipeline(backend, prefix, map_type, config)
        )
    return kvitems_coro


def _make_multi_items_coro(backend):
    def multi_items_coro(target, rules, map_type=None, **config):
        return utils.chain(
            target,
            *_multi_items_pipeline(backend, rules, map_type, config)
        )
    return multi_items_coro


def is_awaitablefunction(func):
    """True if `func` is an awaitable function"""
    return (
        inspect.iscoroutinefunction(func) or (
          inspect.isgeneratorfunction(func) and
          (func.__code__.co_flags & inspect.CO_ITERABLE_COROUTINE)
        )
    )

def is_async_file(f):
    """True if `f` has an asynchronous `read` method"""
    return (
        hasattr(f, 'read') and
        is_awaitablefunction(f.read)
    )

def is_file(x):
    """True if x has a `read` method"""
    return hasattr(x, 'read')


def is_iterable(x):
    """True if x can be iterated over"""
    return hasattr(x, '__iter__')


def _get_source(source):
    if isinstance(source, bytes):
        return io.BytesIO(source)
    elif isinstance(source, str):
        return io.StringIO(source)
    return source


def _make_basic_parse_gen(backend):
    def basic_parse_gen(file_obj, buf_size=64*1024, **config):
        return utils.coros2gen(
            file_source(file_obj, buf_size=buf_size),
            *_basic_parse_pipeline(backend, config)
        )
    return basic_parse_gen


def _make_parse_gen(backend):
    def parse_gen(file_obj, buf_size=64*1024, **config):
        return utils.coros2gen(
            file_source(file_obj, buf_size=buf_size),
            *_parse_pipeline(backend, config)
        )
    return parse_gen


def _make_items_gen(backend):
    def items_gen(file_obj, prefix, map_type=None, buf_size=64*1024, **config):
        return utils.coros2gen(
            file_source(file_obj, buf_size=buf_size),
            *_items_pipeline(backend, prefix, map_type, config)
        )
    return items_gen


def _make_kvitems_gen(backend):
    def kvitems_gen(file_obj, prefix, map_type=None, buf_size=64*1024, **config):
        return utils.coros2gen(
            file_source(file_obj, buf_size=buf_size),
            *_kvitems_pipeline(backend, prefix, map_type, config)
        )
    return kvitems_gen


def _make_multi_items_gen(backend):
    def multi_items_gen(file_obj, rules, map_type=None, buf_size=64*1024, **config):
        return utils.coros2gen(
            file_source(file_obj, buf_size=buf_size),
            *_multi_items_pipeline(backend, rules, map_type, config)
        )
    return multi_items_gen


def _make_basic_parse(backend):
    def basic_parse(source, buf_size=64*1024, **config):
        source = _get_source(source)
        if is_async_file(source):
            return backend['basic_parse_async'](
                source, buf_size=buf_size, **config
            )
        elif is_file(source):
            return backend['basic_parse_gen'](
                source, buf_size=buf_size, **config
            )
        raise ValueError("Unknown source type: %r" % type(source))
    return basic_parse


def _make_parse(backend):
    def parse(source, buf_size=64*1024, **config):
        source = _get_source(source)
        if is_async_file(source):
            return backend['parse_async'](
                source, buf_size=buf_size, **config
            )
        elif is_file(source):
            return backend['parse_gen'](
                source, buf_size=buf_size, **config
            )
        elif is_iterable(source):
            return utils.coros2gen(source,
                (backend['parse_basecoro'], (), {})
            )
        raise ValueError("Unknown source type: %r" % type(source))
    return parse


def _make_items(backend):
    def items(source, prefix, map_type=None, buf_size=64*1024, **config):
        source = _get_source(source)
        if is_async_file(source):
            return backend['items_async'](
                source, prefix, map_type=map_type, buf_size=buf_size, **config
            )
        elif is_file(source):
            return backend['items_gen'](
                source, prefix, map_type=map_type, buf_size=buf_size, **config
            )
        elif is_iterable(source):
            return utils.coros2gen(source,
                (backend['items_basecoro'], (prefix,), {'map_type': map_type})
            )
        raise ValueError("Unknown source type: %r" % type(source))
    return items


def _make_kvitems(backend):
    def kvitems(source, prefix, map_type=None, buf_size=64*1024, **config):
        source = _get_source(source)
        if is_async_file(source):
            return backend['kvitems_async'](
                source, prefix, map_type=map_type, buf_size=buf_size, **config
            )
        elif is_file(source):
            return backend['kvitems_gen'](
                source, prefix, map_type=map_type, buf_size=buf_size, **config
            )
        elif is_iterable(source):
            return utils.coros2gen(source,
                (backend['kvitems_basecoro'], (prefix,), {'map_type': map_type})
            )
        raise ValueError("Unknown source type: %r" % type(source))
    return kvitems


def _make_multi_items(backend):
    def multi_items(source, rules, map_type=None, buf_size=64*1024, **config):
        # Validated eagerly, before any underlying read is attempted
        rules = _prepare_rules(rules)
        source = _get_source(source)
        if is_async_file(source):
            return backend['multi_items_async'](
                source, rules, map_type=map_type, buf_size=buf_size, **config
            )
        elif is_file(source):
            return backend['multi_items_gen'](
                source, rules, map_type=map_type, buf_size=buf_size, **config
            )
        elif is_iterable(source):
            return utils.coros2gen(source,
                (backend['multi_items_basecoro'], (rules,), {'map_type': map_type})
            )
        raise ValueError("Unknown source type: %r" % type(source))
    return multi_items


_common_functions_warn = '''
Don't use the ijson.common.* functions; instead go directly with the ijson.* ones.
See the documentation for more information.
'''

def parse(events):
    """Like ijson.parse, but takes events generated via ijson.basic_parse instead
    of a file"""
    warnings.warn(_common_functions_warn, DeprecationWarning)
    return utils.coros2gen(events,
        (parse_basecoro, (), {})
    )


def kvitems(events, prefix, map_type=None):
    """Like ijson.kvitems, but takes events generated via ijson.parse instead of
    a file"""
    warnings.warn(_common_functions_warn, DeprecationWarning)
    return utils.coros2gen(events,
        (kvitems_basecoro, (prefix,), {'map_type': map_type})
    )


def items(events, prefix, map_type=None):
    """Like ijson.items, but takes events generated via ijson.parse instead of
    a file"""
    warnings.warn(_common_functions_warn, DeprecationWarning)
    return utils.coros2gen(events,
        (items_basecoro, (prefix,), {'map_type': map_type})
    )



def multi_items(events, rules, map_type=None):
    """Like ijson.multi_items, but takes events generated via ijson.parse
    instead of a file"""
    warnings.warn(_common_functions_warn, DeprecationWarning)
    return utils.coros2gen(events,
        (multi_items_basecoro, (rules,), {'map_type': map_type})
    )

class BackendCapabilities:
    '''
    Capabilities supported by a backend.
    '''

    __slots__ = {
        'c_comments': 'C-ctyle comments (non-standard in JSON)',
        'multiple_values': 'Multiple top-level values (non-standard in JSON)',
        'invalid_leading_zeros_detection': 'Detection of leading zeros in numbers, marking them as invalid',
        'incomplete_json_tokens_detection': 'Documents with incomplete JSON tokens',
        'int64': '64 bit integers supported when running with ``use_float=True``',
    }

    def __init__(self):
        self.c_comments = True
        self.multiple_values = True
        self.invalid_leading_zeros_detection = True
        self.incomplete_json_tokens_detection = True
        self.int64 = True


def enrich_backend(backend, **capabilities_overrides):
    '''
    Provides a backend with any missing coroutines/generators/async-iterables
    it might be missing by using the generic ones written in python.
    '''
    # Backends unset some of these
    capabilities = BackendCapabilities()
    for name, value in capabilities_overrides.items():
        setattr(capabilities, name, value)
    backend['capabilities'] = capabilities
    backend['backend'] = backend['__name__'].split('.')[-1]
    backend['backend_name'] = backend['backend']
    for name in ('basic_parse', 'parse', 'items', 'kvitems', 'multi_items'):
        basecoro_name = name + '_basecoro'
        if basecoro_name not in backend:
            backend[basecoro_name] = globals()[basecoro_name]
        coro_name = name + '_coro'
        if coro_name not in backend:
            factory = globals()['_make_' + coro_name]
            backend[coro_name] = factory(backend)
        gen_name = name + '_gen'
        if gen_name not in backend:
            factory = globals()['_make_' + gen_name]
            backend[gen_name] = factory(backend)
        async_name = name + '_async'
        if async_name not in backend:
            factory = getattr(utils35, '_make_' + async_name)
            backend[async_name] = factory(backend)
        factory = globals()['_make_' + name]
        backend[name] = factory(backend)
