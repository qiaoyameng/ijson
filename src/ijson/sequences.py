'''
RFC 7464 JSON Text Sequences support.

A JSON text sequence is a stream of records, each one starting with the ASCII
record separator byte (RS, ``\\x1e``) and ending with a line feed (LF,
``\\n``); the trailing LF is optional for the final record. Records are
independent: an invalid record does not prevent the following records from
being parsed.
'''
from collections import namedtuple
import io

from ijson import adapters, common
from ijson.compat import bytes_reader


RS = 0x1e
'''ASCII record separator byte (``\\x1e``), the start of every record.'''

LF = 0x0a
'''ASCII line feed byte (``\\n``), the nominal end of a record.'''


SKIP = object()
'''Value for ``on_bad_record``: silently skip bad records.'''


class InvalidRecordError(common.JSONError):
    '''
    Raised in strict mode when a record does not contain a single, valid JSON
    text.
    '''

    def __init__(self, record_number, start, end, reason):
        super(InvalidRecordError, self).__init__(
            'Invalid JSON text sequence record #%d at bytes %d-%d: %s' % (
                record_number, start, end, reason
            )
        )
        self.record_number = record_number
        self.start = start
        self.end = end
        self.reason = reason


# A single record as framed out of the byte stream. ``data`` is None when the
# record exceeded the size limit or was not terminated; in that case ``reason``
# explains why.
_Frame = namedtuple('_Frame', ('number', 'start', 'end', 'data', 'reason'))

# A bad record reported to the user: its 1-based number, its [start, end)
# source byte range, and the reason why it was rejected.
BadRecord = namedtuple('BadRecord', ('number', 'start', 'end', 'reason'))


def _byte_chunks(source, buf_size):
    '''Yields byte chunks coming from a file, bytes or a bytes iterable.'''
    if isinstance(source, bytes):
        yield source
        return
    if isinstance(source, str):
        yield source.encode('utf-8')
        return
    if hasattr(source, 'read'):
        if common.is_async_file(source):
            raise ValueError("Asynchronous sources are not supported")
        f = bytes_reader(source)
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            yield chunk
        return
    if hasattr(source, '__iter__'):
        reader = adapters.from_iter(source)
        while True:
            chunk = reader.read(buf_size)
            if not chunk:
                break
            yield chunk
        return
    raise ValueError("Unknown source type: %r" % type(source))


def _oversize_reason(max_record_size):
    return 'record exceeds maximum size of %d bytes' % max_record_size


def _frames(source, buf_size, max_record_size):
    # Splits a byte stream into independent RFC 7464 records on raw bytes.
    #
    # Only RS bytes delimit records, so newlines inside a record (in pretty
    # printed documents or inside strings) never produce record boundaries,
    # and multi-byte UTF-8 characters spanning chunk boundaries are kept
    # intact; they are decoded later by the regular JSON parser.
    #
    # Records larger than max_record_size bytes are rejected. The check is
    # applied before any more data is read in for the open record: as soon as
    # the buffered payload cannot fit, it is dropped and only the position of
    # the next RS byte is tracked, so an oversized record is never buffered.
    chunks = _byte_chunks(source, buf_size)
    # Absolute offset of the RS opening the record being assembled; None
    # while skipping anything before the first RS.
    record_start = None
    record_number = 0
    # Payload bytes of the open record, kept only while within the limit.
    buf = b''
    too_large = False
    # Absolute source offset at which the following chunk begins.
    stream_pos = 0

    while True:
        # Enforce the size limit before more bytes are read in.
        if (
            record_start is not None and not too_large
            and max_record_size is not None
            and len(buf) > max_record_size
        ):
            too_large = True
            buf = b''

        chunk = next(chunks, None)
        if not chunk:
            break

        base = stream_pos
        pos = 0

        if record_start is None:
            # Anything before the first RS belongs to no record (RFC 7464).
            rs_pos = chunk.find(RS)
            if rs_pos == -1:
                stream_pos = base + len(chunk)
                continue
            record_number = 1
            record_start = base + rs_pos
            too_large = False
            buf = b''
            pos = rs_pos + 1

        stream_pos = base + len(chunk)

        while pos < len(chunk):
            rs_pos = chunk.find(RS, pos)
            if rs_pos == -1:
                if not too_large:
                    buf += chunk[pos:]
                    if (
                        max_record_size is not None
                        and len(buf) > max_record_size
                    ):
                        # Already over the limit within this chunk; drop the
                        # payload and resume scanning for the next RS byte.
                        too_large = True
                        buf = b''
                pos = len(chunk)
            else:
                end = base + rs_pos
                if not too_large:
                    buf += chunk[pos:rs_pos]
                    if (
                        max_record_size is not None
                        and len(buf) > max_record_size
                    ):
                        too_large = True
                if too_large:
                    yield _Frame(
                        record_number, record_start, end, None,
                        _oversize_reason(max_record_size)
                    )
                else:
                    yield _Frame(record_number, record_start, end, buf, None)
                record_start = end
                record_number += 1
                buf = b''
                too_large = False
                pos = rs_pos + 1

    if record_start is None:
        # No record ever started.
        return

    end = record_start + 1 + len(buf)
    if too_large:
        yield _Frame(
            record_number, record_start, end, None,
            _oversize_reason(max_record_size)
        )
    elif not _is_blank(buf):
        if buf and buf[-1] == LF:
            # A final record needs no RS after it: the RS that opened it plus
            # its terminating LF delimit it completely.
            yield _Frame(record_number, record_start, end, buf, None)
        else:
            yield _Frame(
                record_number, record_start, end, buf,
                'record is not terminated'
            )


def _payload_without_lf(payload):
    '''Drops the single trailing LF that belongs to the record framing.'''
    if payload and payload[-1] == LF:
        return payload[:-1]
    return payload


def _is_blank(payload):
    '''True if the payload carries no JSON text.

    At most one trailing LF is record framing per RFC 7464. Anything else is
    blank if it only holds JSON whitespace.
    '''
    payload = _payload_without_lf(payload)
    return not payload or not payload.strip()


def _parse_record(data, config):
    '''
    Parses a single record into a Python value using the regular ijson JSON
    parsing machinery. A fresh parser per record guarantees the record holds
    exactly one JSON text (anything trailing yields an error) and lets a
    failure in one record never leak into the next one.
    '''
    from ijson import basic_parse
    builder = common.ObjectBuilder()
    saw_event = False
    for event, value in basic_parse(io.BytesIO(data), **config):
        builder.event(event, value)
        saw_event = True
    if not saw_event:
        return None, 'record is empty'
    return builder.value, None


def items(source, on_bad_record='raise', max_record_size=None,
          buf_size=64 * 1024, **config):
    '''
    Iterates over the Python values of the records of an `RFC 7464`_ JSON text
    sequence read from ``source`` (a binary file, ``bytes`` or an iterable of
    byte chunks).

    Records are delimited by the RS byte (``\\x1e``), so newlines appearing
    legitimately inside a record (inside strings or in pretty printed
    documents) never cause record boundaries, and multi-byte UTF-8 characters
    spanning chunk boundaries are decoded correctly. Every record is parsed in
    isolation, which also enforces the proper termination of top level scalar
    values such as numbers.

    A single broken record does not invalidate the rest of the stream. The
    ``on_bad_record`` parameter selects what happens with records that do not
    contain exactly one valid JSON text, as well as with records whose payload
    is larger than ``max_record_size`` bytes (the limit is enforced before any
    further bytes of the record are buffered):

    - ``'raise'`` (the default): an :class:`InvalidRecordError` is raised.
    - ``ijson.sequences.SKIP``: the record is silently skipped.
    - a callable: it is invoked with a :class:`BadRecord` containing the
      1-based record number, its ``[start, end)`` byte range within the source
      and the failure reason. Skipping resumes parsing at the next RS byte, so
      no part of the broken record is mistaken for the next JSON text.

    Any extra keyword arguments are passed through to the JSON parser (e.g.
    ``use_float=True``).

    .. _RFC 7464: https://www.rfc-editor.org/rfc/rfc7464
    '''
    if (
        on_bad_record != 'raise'
        and on_bad_record is not SKIP
        and not callable(on_bad_record)
    ):
        raise ValueError(
            "on_bad_record must be 'raise', ijson.sequences.SKIP or a callable"
        )

    def reject(frame, reason):
        bad = BadRecord(frame.number, frame.start, frame.end, reason)
        if on_bad_record == 'raise':
            raise InvalidRecordError(
                bad.number, bad.start, bad.end, bad.reason
            )
        if callable(on_bad_record):
            on_bad_record(bad)

    for frame in _frames(source, buf_size, max_record_size):
        if frame.reason is not None:
            reject(frame, frame.reason)
            continue
        if _is_blank(frame.data):
            # Empty records are valid sequence gaps and produce no value.
            continue
        try:
            value, reason = _parse_record(
                _payload_without_lf(frame.data), config
            )
        except common.JSONError as e:
            reason = str(e) or type(e).__name__
        except ValueError as e:
            # String/UTF-8 decoding failures (e.g. bad escapes, invalid
            # UTF-8) surface as ValueError subclasses in the python backend.
            reason = str(e) or type(e).__name__
        if reason is not None:
            reject(frame, reason)
            continue
        yield value
