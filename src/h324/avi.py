"""Bounded packet framing for one FFmpeg H.263 video stream in pipe AVI.

This local encoder container never crosses the modem wire. RIFF and movi can
have FFmpeg's unknown-length 0xffffffff value; individual chunks cannot.
See Microsoft's AVI RIFF File Reference and FFmpeg libavformat/avienc.c.
"""
import struct


class AviPictureParser:
    """Extract whole 00dc packets without waiting for another start code.

    Accepts one H263 video stream, bounded header/metadata and flat movi chunks.
    Nested record lists, audio, OpenDML continuations and other codecs are
    intentionally unsupported. finish() rejects truncated chunks or padding.
    Checks packet completeness and picture-start alignment/count, not H.263
    macroblock syntax; the external decoder validates the codec bitstream.
    """
    def __init__(self, max_header=65536, max_picture=8192):
        if not 256 <= max_header <= 1048576 or not 3 <= max_picture <= 1048576:
            raise ValueError('Invalid AVI parser bounds')
        self.max_header, self.max_picture = max_header, max_picture
        self.reset()

    def reset(self):
        self._buffer = bytearray()
        self._state = 'riff'
        self._position = 0
        self._riff_end = self._movi_end = None
        self._validated = self._seen_movi = self._finished = False
        self._failure = None

    @property
    def buffered_bytes(self):
        return len(self._buffer)

    def _take(self, count):
        value = bytes(self._buffer[:count])
        del self._buffer[:count]
        self._position += count
        if self._riff_end is not None and self._position > self._riff_end:
            raise ValueError('AVI chunk extends beyond RIFF boundary')
        return value

    @staticmethod
    def _chunks(data):
        offset = 0
        while offset < len(data):
            if len(data)-offset < 8:
                raise ValueError('Truncated AVI header chunk')
            tag, size = struct.unpack_from('<4sI', data, offset)
            end = offset+8+size
            padded = end+(size & 1)
            if padded > len(data):
                raise ValueError('AVI header chunk exceeds its LIST boundary')
            yield tag, data[offset+8:end]
            offset = padded

    def _validate_header(self, data):
        main, streams = [], []
        for tag, body in self._chunks(data):
            if tag == b'avih':
                main.append(body)
            elif tag == b'LIST' and body[:4] == b'strl':
                streams.append(list(self._chunks(body[4:])))
            elif tag == b'LIST':
                raise ValueError('Unsupported nested AVI header LIST')
        if len(main) != 1 or len(main[0]) < 56:
            raise ValueError('AVI requires one complete avih header')
        if struct.unpack_from('<I', main[0], 24)[0] != 1 or len(streams) != 1:
            raise ValueError('AVI requires exactly one video stream')
        headers = [body for tag, body in streams[0] if tag == b'strh']
        formats = [body for tag, body in streams[0] if tag == b'strf']
        if any(tag == b'LIST' for tag, _ in streams[0]):
            raise ValueError('Unsupported nested AVI stream LIST')
        if len(headers) != 1 or len(headers[0]) < 56 or headers[0][:8] != b'vidsH263':
            raise ValueError('AVI stream must be H263 video')
        if len(formats) != 1 or len(formats[0]) < 40:
            raise ValueError('AVI requires a complete video format')
        fmt = formats[0]
        if struct.unpack_from('<I', fmt)[0] != 40 or fmt[16:20] != b'H263':
            raise ValueError('AVI video format must be H263 BITMAPINFOHEADER')
        width, height = struct.unpack_from('<ii', fmt, 4)
        if width != 128 or height != 96 or struct.unpack_from('<H', fmt, 12)[0] != 1:
            raise ValueError('AVI H263 profile requires 128 by 96 video')
        self._validated = True

    def _parse(self):
        pictures = []
        while True:
            if self._state == 'riff':
                if len(self._buffer) < 12:
                    break
                tag, size, kind = struct.unpack_from('<4sI4s', self._buffer)
                if tag != b'RIFF' or kind != b'AVI ' or size < 4:
                    raise ValueError('Expected RIFF AVI header')
                self._riff_end = None if size == 0xffffffff else size+8
                self._take(12)
                self._state = 'header'
            elif self._state == 'header':
                if len(self._buffer) < 8:
                    break
                tag, size = struct.unpack_from('<4sI', self._buffer)
                if tag == b'LIST':
                    if size < 4:
                        raise ValueError('AVI LIST must include its type')
                    if len(self._buffer) < 12:
                        break
                    kind = bytes(self._buffer[8:12])
                    if kind == b'movi':
                        if not self._validated or size < 4:
                            raise ValueError('AVI movi requires a preceding H263 header')
                        if size != 0xffffffff and size & 1:
                            raise ValueError('AVI movi size is not chunk aligned')
                        if self._position+12 > self.max_header:
                            raise ValueError('AVI header exceeds configured bound')
                        self._movi_end = None if size == 0xffffffff else self._position+8+size
                        if self._riff_end is not None and (self._movi_end is None or self._movi_end > self._riff_end):
                            raise ValueError('AVI movi exceeds RIFF boundary')
                        self._take(12)
                        self._state, self._seen_movi = 'movi', True
                        continue
                    if kind not in (b'hdrl', b'INFO'):
                        raise ValueError('Unsupported AVI header LIST')
                elif tag != b'JUNK':
                    raise ValueError('Unsupported AVI header chunk')
                total = 8+size+(size & 1)
                if self._position+total > self.max_header:
                    raise ValueError('AVI header exceeds configured bound')
                if len(self._buffer) < total:
                    break
                packet = self._take(total)
                if tag == b'LIST' and packet[8:12] == b'hdrl':
                    if self._validated:
                        raise ValueError('Duplicate AVI hdrl LIST')
                    self._validate_header(packet[12:8+size])
            else:
                if self._movi_end is not None and self._position == self._movi_end:
                    self._state = 'trailer'
                if len(self._buffer) < 8:
                    break
                tag, size = struct.unpack_from('<4sI', self._buffer)
                if tag == b'00dc' and self._state == 'movi':
                    limit = self.max_picture
                elif tag in (b'JUNK', b'idx1'):
                    if tag == b'idx1' and self._state == 'movi' and self._movi_end is not None:
                        raise ValueError('AVI index must follow the finite movi LIST')
                    limit = self.max_header
                else:
                    raise ValueError('Unsupported AVI movie chunk or stream')
                total = 8+size+(size & 1)
                if size > limit:
                    raise ValueError('AVI chunk exceeds configured bound')
                if self._movi_end is not None and self._state == 'movi' and self._position+total > self._movi_end:
                    raise ValueError('AVI packet exceeds movi boundary')
                if len(self._buffer) < total:
                    break
                packet = self._take(total)
                if tag == b'idx1':
                    self._state = 'trailer'
                elif tag == b'00dc' and size:
                    picture = packet[8:8+size]
                    from h324.media import h263_pictures
                    if len(h263_pictures(picture)) != 1:
                        raise ValueError('AVI packet must contain one H263 picture start')
                    pictures.append(picture)
                # Zero-length 00dc represents a skipped frame, not a picture.
        return pictures

    def feed(self, data):
        if self._failure:
            raise ValueError('AVI parser failed; reset required') from self._failure
        if self._finished:
            raise ValueError('AVI parser is finished; reset required')
        result = []
        view = memoryview(data)
        capacity = max(self.max_header, self.max_picture)+12
        try:
            while view:
                count = min(len(view), capacity-len(self._buffer))
                if count <= 0:
                    raise ValueError('AVI retained input exceeds configured bound')
                self._buffer.extend(view[:count])
                view = view[count:]
                result.extend(self._parse())
        except ValueError as exc:
            self._failure = exc
            raise
        return result

    def finish(self):
        if self._failure:
            raise ValueError('AVI parser failed; reset required') from self._failure
        if self._finished:
            return []
        if (not self._seen_movi or self._buffer or
                self._movi_end is not None and self._position < self._movi_end or
                self._riff_end is not None and self._position != self._riff_end):
            self._failure = ValueError('Truncated AVI stream, chunk or padding')
            raise self._failure
        self._finished = True
        return []
