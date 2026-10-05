'''
RFC 7464 JSON Text Sequences.
'''
from ijson import common, compat, utils, utils35


RS = b'\x1e'
_SKIPPED_RECORD = object()


class JSONSequenceError(common.JSONError):
    '''Base exception for JSON Text Sequence parsing errors.'''


class InvalidRecordError(JSONSequenceError):
    '''Raised when a JSON Text Sequence record is not valid.'''

    def __init__(self, record_number, start, end, reason):
        self.record_number = record_number
        self.start = start
        self.end = end
        self.reason = reason
        super().__init__(
            'Invalid record %d at bytes %d:%d: %s'
            % (record_number, start, end, reason)
        )


class RecordTooLargeError(InvalidRecordError):
    '''Raised when a record exceeds the configured size limit.'''

    def __init__(self, record_number, start, end, max_record_size):
        self.max_record_size = max_record_size
        super().__init__(
            record_number, start, end,
            'record exceeds maximum size of %d bytes' % max_record_size
        )


class _Record:
    def __init__(self, backend, number, start, config):
        self.number = number
        self.start = start
        self.end = start
        self.events = utils.sendable_list()
        self.coro = backend['items_coro'](self.events, '', **config)
        self.values = []
        self.error = None
        self.error_reason = None
        self.limit_reported = False
        self.range_fixed = False
        self.payload_size = 0

    def feed(self, data):
        self.end += len(data)
        self.payload_size += len(data)
        try:
            self.coro.send(data)
        except Exception as exc:
            self.fail(exc)
            return
        self._collect()

    def finish(self):
        try:
            self.coro.close()
        except Exception as exc:
            self.fail(exc)
        else:
            self._collect()

    def payload_range_end(self):
        return self.start + self.payload_size

    def skip(self, data):
        self.end += len(data)
        self.payload_size += len(data)

    def close(self):
        try:
            self.coro.close()
        except Exception:
            pass

    def fail(self, exc):
        self.close()
        if self.error is not None:
            return
        reason = str(exc)
        error = InvalidRecordError(
            self.number, self.start, self.end, reason
        )
        error.__cause__ = exc
        self.error = error
        self.error_reason = reason

    def value(self):
        if self.error is not None:
            raise self.error
        if len(self.values) != 1:
            raise InvalidRecordError(
                self.number, self.start, self.end,
                'record does not contain exactly one JSON value'
            )
        return self.values[0]

    def fail_with_range(self, end):
        if self.error is None:
            return
        if self.range_fixed:
            return
        self.error.start = self.start
        self.error.end = end
        self.error.record_number = self.number
        self.error.reason = self.error_reason
        self.error.args = (
            'Invalid record %d at bytes %d:%d: %s'
            % (self.error.record_number, self.start, end, self.error_reason),
        )

    def _collect(self):
        self.values.extend(self.events)
        del self.events[:]


class _SequenceReader:
    def __init__(self, buf_size, max_record_size, on_bad_record, config):
        self.backend = config.pop('_backend', None)
        if self.backend is None:
            import ijson
            self.backend = ijson.get_backend(ijson.backend_name).__dict__
        self.buf_size = buf_size
        self.max_record_size = max_record_size
        self.on_bad_record = on_bad_record
        self.config = config
        self.position = 0
        self.next_number = 1
        self.next_start = 0
        self.seen_first_rs = False
        self.inside_record = False
        self.record = None
        self.preamble = 0

    def process(self, data, final=False):
        chunk_start = self.position
        self.position += len(data)
        offset = 0

        while offset < len(data):
            rs = data.find(RS, offset)
            if rs >= 0:
                segment_end = rs
                frame_end = chunk_start + rs
            else:
                segment_end = len(data)
                frame_end = self.position

            segment = data[offset:segment_end]
            if not self.seen_first_rs:
                if segment:
                    self._feed_preamble(segment)
            else:
                self._feed_record(segment)

            if rs < 0:
                break

            if not self.seen_first_rs:
                self._end_preamble(frame_end)
            elif self.inside_record:
                result = self._end_record(frame_end)
                self.record = None
                yield result
            else:
                yield self._empty_record(frame_end)

            self.next_number += 1
            self.next_start = frame_end + 1
            self.inside_record = False
            self.record = None
            offset = rs + 1

        if final and not self.seen_first_rs and self.preamble:
            self._report_preamble(self.position)
        elif final and self.inside_record:
            result = self._end_record(self.position)
            self.record = None
            yield result
        elif final and self.seen_first_rs:
            yield self._empty_record(self.position - 1)

    def read_size(self):
        if not self.seen_first_rs and not self.inside_record:
            return self._limited_size(0)
        if self.record is None:
            size = 0
        else:
            size = self.record.end - self.record.start
        return self._limited_size(size)

    def _limited_size(self, size):
        remaining = self.max_record_size - size
        if remaining <= 0:
            return 1
        return min(self.buf_size, remaining + 1)

    def _start_record(self):
        self.record = _Record(
            self.backend, self.next_number, self.next_start,
            self.config
        )
        self.inside_record = True

    def _feed_record(self, segment):
        if not segment:
            return
        if self.record is None:
            self._start_record()
        record = self.record

        if record.error is not None:
            record.skip(segment)
            return

        size = record.end - record.start
        allowed = self.max_record_size - size
        if len(segment) <= allowed:
            record.feed(segment)
            return

        if allowed:
            record.feed(segment[:allowed])
        record.close()
        record.end = record.start + self.max_record_size + 1
        error = RecordTooLargeError(
            record.number, record.start, record.end, self.max_record_size
        )
        record.error = error
        record.limit_reported = True
        record.range_fixed = True
        raise error

    def _end_record(self, frame_end):
        if self.record is None:
            self._start_record()
        record = self.record
        if record.error is None:
            record.finish()
        if not record.payload_size:
            record.end = frame_end
            record.fail_with_range(frame_end)
        else:
            record.fail_with_range(record.payload_range_end())
        if record.error is not None:
            if not record.limit_reported:
                self._report(record.error)
            return _SKIPPED_RECORD
        return record.value()

    def _empty_record(self, end):
        error = InvalidRecordError(
            self.next_number, end + 1, end + 1, 'empty record'
        )
        self._report(error)
        return _SKIPPED_RECORD

    def _feed_preamble(self, segment):
        self.preamble += len(segment)
        if self.preamble > self.max_record_size:
            error = RecordTooLargeError(
                1, 0, self.max_record_size + 1, self.max_record_size
            )
            raise error

    def _end_preamble(self, rs_position):
        if self.preamble:
            self._report_preamble(rs_position)
        self.preamble = 0
        self.seen_first_rs = True
        self.next_number = 0
        self.next_start = rs_position + 1

    def _report_preamble(self, end):
        error = InvalidRecordError(
            1, 0, end,
            'JSON Text Sequence must start with an RS byte'
        )
        self._report(error)

    def _report(self, error):
        if self.on_bad_record is None:
            raise error
        self.on_bad_record(error)

    def close(self):
        record = self.record
        self.record = None
        if record is not None and record.error is None:
            record.close()


def _valid_values(values):
    for value in values:
        if value is not _SKIPPED_RECORD:
            yield value


def _bad_record_handler(on_bad_record, skip_bad_records):
    if on_bad_record is not None and skip_bad_records is False:
        raise ValueError(
            'on_bad_record skips invalid records and cannot be combined '
            'with skip_bad_records=False'
        )
    if skip_bad_records is False:
        return None
    if skip_bad_records is True:
        return lambda error: None
    return on_bad_record


def _make_json_sequence(backend):
    def json_sequence(source, buf_size=64 * 1024,
                      max_record_size=64 * 1024 * 1024,
                      on_bad_record=None, skip_bad_records=None, **config):
        backend_name = config.pop('backend', None)
        if backend_name is not None:
            import ijson
            config['_backend'] = ijson.get_backend(backend_name).__dict__
        else:
            config['_backend'] = backend
        handler = _bad_record_handler(on_bad_record, skip_bad_records)
        return _json_sequence(
            source, buf_size=buf_size, max_record_size=max_record_size,
            on_bad_record=handler, **config
        )
    return json_sequence


def _make_json_sequence_async(backend):
    def json_sequence_async(source, buf_size=64 * 1024,
                            max_record_size=64 * 1024 * 1024,
                            on_bad_record=None, skip_bad_records=None, **config):
        backend_name = config.pop('backend', None)
        if backend_name is not None:
            import ijson
            config['_backend'] = ijson.get_backend(backend_name).__dict__
        else:
            config['_backend'] = backend
        handler = _bad_record_handler(on_bad_record, skip_bad_records)
        return _json_sequence_async(
            source, buf_size=buf_size, max_record_size=max_record_size,
            on_bad_record=handler, **config
        )
    return json_sequence_async


def _enrich_backend(backend):
    backend['json_sequence'] = _make_json_sequence(backend)
    backend['json_sequence_async'] = _make_json_sequence_async(backend)


common._sequence_backend_enricher = _enrich_backend


def _json_sequence(source, buf_size=64 * 1024,
                  max_record_size=64 * 1024 * 1024,
                  on_bad_record=None, **config):
    '''Synchronously iterate top-level values from an RFC 7464 stream.'''
    if buf_size <= 0:
        raise ValueError('buf_size must be greater than zero')
    if max_record_size <= 0:
        raise ValueError('max_record_size must be greater than zero')

    source = common._get_source(source)
    reader = _SequenceReader(
        buf_size, max_record_size, on_bad_record, config
    )

    try:
        if common.is_file(source):
            source = compat.bytes_reader(source)
            while True:
                data = source.read(reader.read_size())
                if not data:
                    yield from _valid_values(reader.process(b'', final=True))
                    return
                yield from _valid_values(reader.process(data))
        elif common.is_iterable(source):
            for data in source:
                if isinstance(data, str):
                    data = data.encode('utf-8')
                elif not isinstance(data, bytes):
                    raise TypeError('sequence source must yield bytes or str')
                yield from _valid_values(reader.process(data))
            yield from _valid_values(reader.process(b'', final=True))
        else:
            raise ValueError('Unknown source type: %r' % type(source))
    finally:
        reader.close()


async def _json_sequence_async(source, buf_size=64 * 1024,
                              max_record_size=64 * 1024 * 1024,
                              on_bad_record=None, **config):
    '''Asynchronously iterate top-level values from an RFC 7464 stream.'''
    if buf_size <= 0:
        raise ValueError('buf_size must be greater than zero')
    if max_record_size <= 0:
        raise ValueError('max_record_size must be greater than zero')

    source = common._get_source(source)
    reader = _SequenceReader(
        buf_size, max_record_size, on_bad_record, config
    )

    try:
        if common.is_async_file(source):
            read = await utils35._get_read(source)
            while True:
                data = await read(reader.read_size())
                if not data:
                    async for value in _async_valid_values(
                        reader.process(b'', final=True)
                    ):
                        yield value
                    return
                async for value in _async_valid_values(reader.process(data)):
                    yield value
        elif hasattr(source, '__aiter__'):
            async for data in source:
                if isinstance(data, str):
                    data = data.encode('utf-8')
                elif not isinstance(data, bytes):
                    raise TypeError('sequence source must yield bytes or str')
                async for value in _async_valid_values(reader.process(data)):
                    yield value
            async for value in _async_valid_values(
                reader.process(b'', final=True)
            ):
                yield value
        else:
            for value in _json_sequence(
                source, buf_size=buf_size, max_record_size=max_record_size,
                on_bad_record=on_bad_record, **config
            ):
                yield value
    finally:
        reader.close()


async def _async_valid_values(values):
    for value in values:
        if value is not _SKIPPED_RECORD:
            yield value


json_sequence = _json_sequence
json_sequence_async = _json_sequence_async
sequence_items = _json_sequence
json_text_sequence = _json_sequence
