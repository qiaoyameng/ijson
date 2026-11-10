'''
Incremental decoding of byte streams into the unicode strings the pure
python lexer works on.

Only the encodings explicitly listed in :data:`SUPPORTED_ENCODINGS` are
accepted, and BOM-based detection is purely deterministic: no heuristic or
probabilistic charset detection is ever performed.
'''
import codecs

from ijson import common


SUPPORTED_ENCODINGS = frozenset((
    'utf-8',
    'utf-16-le',
    'utf-16-be',
    'utf-32-le',
    'utf-32-be',
))

# Encoding name aliases users might reasonably pass. Everything is normalized
# to one of the canonical names in SUPPORTED_ENCODINGS.
_ENCODING_ALIASES = {
    'utf8': 'utf-8',
    'u8': 'utf-8',
    'utf_8': 'utf-8',
    'utf16le': 'utf-16-le',
    'utf_16_le': 'utf-16-le',
    'u16le': 'utf-16-le',
    'utf16be': 'utf-16-be',
    'utf_16_be': 'utf-16-be',
    'u16be': 'utf-16-be',
    'utf16': 'utf-16',
    'utf_16': 'utf-16',
    'u16': 'utf-16',
    'utf32le': 'utf-32-le',
    'utf_32_le': 'utf-32-le',
    'u32le': 'utf-32-le',
    'utf32be': 'utf-32-be',
    'utf_32_be': 'utf-32-be',
    'u32be': 'utf-32-be',
    'utf32': 'utf-32',
    'utf_32': 'utf-32',
    'u32': 'utf-32',
}

# Bytes consumed per code unit (i.e. per character produced by the decoder) of
# a fully detected encoding. UTF-8 is handled separately since its width is
# variable.
_CODE_UNIT_WIDTH = {
    'utf-16-le': 2,
    'utf-16-be': 2,
    'utf-32-le': 4,
    'utf-32-be': 4,
}

# BOM signatures mapped to the encoding they announce. Longer signatures come
# first so e.g. the UTF-32 BOM is never mistaken for its UTF-16 prefix.
_BOMS = (
    (codecs.BOM_UTF32_LE, 'utf-32-le'),
    (codecs.BOM_UTF32_BE, 'utf-32-be'),
    (codecs.BOM_UTF16_LE, 'utf-16-le'),
    (codecs.BOM_UTF16_BE, 'utf-16-be'),
    (codecs.BOM_UTF8, 'utf-8'),
)


def normalize_encoding(encoding):
    '''
    Normalizes a user-provided encoding name into one of the canonical names
    in :data:`SUPPORTED_ENCODINGS`.

    Bare ``utf-16``/``utf-32`` names are rejected: without a BOM they do not
    identify a concrete byte layout, and BOM-based resolution is handled
    separately by leaving the encoding unspecified.
    '''
    if encoding is None:
        return None
    if not isinstance(encoding, str):
        raise common.JSONError(
            'input_encoding must be a string, got %r' % (encoding,)
        )
    name = encoding.strip().lower()
    name = _ENCODING_ALIASES.get(name, name)
    if name not in SUPPORTED_ENCODINGS:
        raise common.JSONError(
            'Unsupported input_encoding %r; supported encodings are: %s' % (
                encoding, ', '.join(sorted(SUPPORTED_ENCODINGS))
            )
        )
    return name


def _char_utf8_width(char):
    '''Width in bytes of ``char`` when encoded as UTF-8.'''
    codepoint = ord(char)
    if codepoint < 0x80:
        return 1
    if codepoint < 0x800:
        return 2
    if codepoint < 0x10000:
        return 3
    return 4


class InputTranscoder:
    '''
    Incrementally transcodes raw input bytes into unicode strings while
    keeping track of the byte offset (relative to the very first byte of the
    stream, BOM included) at which every produced character starts.

    When ``declared`` is None the stream encoding is detected solely from a
    leading BOM; a BOM-less stream is treated as UTF-8, which matches ijson's
    historical behavior. No probabilistic detection is attempted.
    '''

    def __init__(self, declared=None):
        self.declared = normalize_encoding(declared)
        self.encoding = None
        self._decoder = None
        self._pending = b''
        # Byte offset of the first character not yet emitted.
        self._cursor = 0
        # True when the last emitted character is a high surrogate whose low
        # surrogate is expected to arrive in a subsequent chunk.
        self._pending_high_surrogate = False

    def _activate(self, encoding, bom_length, remainder):
        self.encoding = encoding
        self._decoder = codecs.getincrementaldecoder(encoding)()
        self._pending = b''
        self._cursor = bom_length
        self._pending = remainder

    def _source_offsets(self, text):
        '''Byte offsets in the original stream for every char of ``text``.'''
        offsets = []
        encoding = self.encoding
        cursor = self._cursor
        if encoding == 'utf-8':
            for char in text:
                width = _char_utf8_width(char)
                offsets.append(cursor)
                cursor += width
        else:
            width = _CODE_UNIT_WIDTH[encoding]
            index = 0
            while index < len(text):
                if self._pending_high_surrogate:
                    # The low surrogate completes the character started in a
                    # previous chunk; its code unit was already reserved.
                    self._pending_high_surrogate = False
                    index += 1
                    continue
                offsets.append(cursor)
                codepoint = ord(text[index])
                if width == 2 and codepoint > 0xFFFF:
                    # Characters outside the BMP take two UTF-16 code units.
                    cursor += width * 2
                    index += 1
                elif width == 2 and 0xD800 <= codepoint <= 0xDBFF:
                    # A high surrogate decoded alone: its low surrogate is
                    # still buffered in the decoder and arrives in a later
                    # chunk. Reserve both code units now.
                    cursor += width * 2
                    self._pending_high_surrogate = True
                    index += 1
                else:
                    cursor += width
                    index += 1
        self._cursor = cursor
        return offsets

    def decode(self, data):
        '''
        Decodes one chunk of bytes, returning a ``(text, offsets)`` pair.
        Either member can be empty when the chunk only completed a BOM or fed
        an incomplete multi-byte code unit.
        '''
        if self.encoding is None:
            if not data:
                # No bytes yet (or an empty read); keep waiting for a BOM
                # until finish() is called.
                return '', []
            self._detect(data)
            if self.encoding is None:
                return '', []
            data = self._pending
            self._pending = b''
        return self._feed(data)

    def _detect(self, data):
        buf = self._pending + data
        self._pending = b''
        if self.declared is not None:
            # The encoding was explicitly given: a BOM is still consumed at
            # the very start of the stream (and must match), but partial BOM
            # prefixes must not hold back ordinary data such as NUL bytes.
            for bom, bom_encoding in _BOMS:
                if buf.startswith(bom):
                    if bom_encoding != self.declared:
                        raise common.JSONError(
                            'Declared input_encoding %r conflicts with BOM %r'
                            % (self.declared, bom)
                        )
                    self._activate(bom_encoding, len(bom), buf[len(bom):])
                    return
                if bom.startswith(buf) and len(buf) < len(bom):
                    # Only keep waiting when the bytes seen so far could still
                    # be the BOM of the *declared* encoding.
                    if bom_encoding == self.declared:
                        self._pending = buf
                        return
            self._activate(self.declared, 0, buf)
            return
        for bom, bom_encoding in _BOMS:
            if buf.startswith(bom):
                self._activate(bom_encoding, len(bom), buf[len(bom):])
                return
            if bom.startswith(buf):
                # The whole buffer is a (so far valid) BOM prefix, wait for
                # more bytes before deciding.
                self._pending = buf
                return
        # No BOM at the stream start.
        self._activate(
            self.declared if self.declared is not None else 'utf-8',
            0,
            buf,
        )

    def _feed(self, data):
        if not data:
            return '', []
        try:
            text = self._decoder.decode(data, False)
        except UnicodeDecodeError as exc:
            raise self._decode_error(exc)
        if not text:
            return '', []
        return text, self._source_offsets(text)

    def finish(self):
        '''Flushes the underlying decoder at end of stream.'''
        if self.encoding is None:
            if self._pending:
                raise common.JSONError(
                    'Truncated BOM at byte %d' % (len(self._pending),)
                )
            self._activate(
                self.declared if self.declared is not None else 'utf-8',
                0,
                b'',
            )
        try:
            text = self._decoder.decode(b'', True)
        except UnicodeDecodeError as exc:
            raise self._decode_error(exc)
        if not text:
            return '', []
        return text, self._source_offsets(text)

    def _decode_error(self, exc):
        position = self._cursor
        # For UTF-8 the decoder's relative offset applies to the bytes fed in
        # since the last emitted character; for fixed-width codecs the cursor
        # already points at the first not-yet-emitted code unit.
        if self.encoding == 'utf-8':
            position = self._cursor + exc.start
        return common.JSONError(
            'Malformed %s sequence at byte %d: %s' % (
                self.encoding, position, exc.reason
            )
        )
