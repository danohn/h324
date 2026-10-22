"""Synthetic RIFF structure tests, with no FFmpeg or camera dependency."""
import struct
import pytest
from h324.media import AviPictureParser


def chunk(tag, body):
    return tag+struct.pack('<I', len(body))+body+(b'\0' if len(body)&1 else b'')


def header(*, streams=1, handler=b'H263', compression=b'H263', stream_type=b'vids'):
    avih = bytearray(56)
    struct.pack_into('<I', avih, 24, streams)
    strh = stream_type+handler+bytes(48)
    strf = struct.pack('<IiiHH4s', 40, 128, 96, 1, 24, compression)+bytes(20)
    stream = chunk(b'LIST', b'strl'+chunk(b'strh', strh)+chunk(b'strf', strf))
    return chunk(b'LIST', b'hdrl'+chunk(b'avih', avih)+stream)


def avi(pictures, *, pipe=True, **options):
    packets = b''.join(chunk(b'00dc', picture) for picture in pictures)
    body = b'AVI '+header(**options)+chunk(b'LIST', b'INFO'+chunk(b'ISFT', b'Synthetic'))
    body += chunk(b'JUNK', b'odd')
    movie = b'LIST'+struct.pack('<I', 0xffffffff if pipe else 4+len(packets))+b'movi'+packets
    body += movie
    return b'RIFF'+struct.pack('<I', 0xffffffff if pipe else len(body))+body


PICTURES = [b'\0\0\x80first', b'\0\0\x81second', b'\0\0\x82last']


@pytest.mark.parametrize('pipe', [True, False])
@pytest.mark.parametrize('step', [1, 3, 17, 65536])
def test_complete_packets_emit_without_waiting_for_the_next_picture(pipe, step):
    data = avi(PICTURES, pipe=pipe)
    parser = AviPictureParser()
    got = []
    for offset in range(0, len(data), step):
        got.extend(parser.feed(data[offset:offset+step]))
    assert got == PICTURES
    assert parser.buffered_bytes == 0
    assert parser.finish() == []
    assert parser.finish() == []


def test_first_packet_is_emitted_before_second_packet_header_exists():
    data = avi(PICTURES)
    second = data.index(b'00dc', data.index(b'00dc')+4)
    parser = AviPictureParser()
    assert parser.feed(data[:second]) == PICTURES[:1]
    assert parser.buffered_bytes == 0
    assert parser.feed(data[second:]) == PICTURES[1:]
    parser.finish()


@pytest.mark.parametrize('options', [dict(streams=2), dict(handler=b'H264'),
    dict(compression=b'H264'), dict(stream_type=b'auds')])
def test_other_codec_and_stream_count_rejected_before_emitting(options):
    with pytest.raises(ValueError, match='stream|H263'):
        AviPictureParser().feed(avi(PICTURES, **options))


@pytest.mark.parametrize('cut', [1, 7, 11, 18, -1, -3, -9])
def test_finish_rejects_truncated_header_packet_or_padding(cut):
    data = avi([b'\0\0\x80ab'])  # Odd payload requires a final padding byte.
    parser = AviPictureParser()
    parser.feed(data[:cut])
    with pytest.raises(ValueError, match='Truncated'):
        parser.finish()


def test_packet_and_header_size_bounds_fail_without_accumulating_body():
    parser = AviPictureParser(max_picture=16)
    data = avi([])+b'00dc'+struct.pack('<I', 100000000)
    with pytest.raises(ValueError, match='bound'):
        parser.feed(data)
    assert parser.buffered_bytes <= 8
    with pytest.raises(ValueError, match='reset'):
        parser.feed(b'')
    parser.reset()
    assert parser.feed(avi(PICTURES)) == PICTURES
    parser.finish()
    with pytest.raises(ValueError, match='reset'):
        parser.feed(b'')
    with pytest.raises(ValueError, match='bound'):
        AviPictureParser(max_header=256).feed(avi(PICTURES))


def test_empty_skip_packets_do_not_emit_empty_pictures():
    parser = AviPictureParser()
    assert parser.feed(avi([b'', PICTURES[0], b''])) == [PICTURES[0]]
    parser.finish()


@pytest.mark.parametrize('packet', [b'bad picture', PICTURES[0]+PICTURES[1]])
def test_packet_must_contain_one_baseline_picture(packet):
    with pytest.raises(ValueError, match='picture|H.263'):
        AviPictureParser().feed(avi([packet]))


def test_unknown_movie_stream_and_nested_record_lists_are_rejected():
    for packet in (chunk(b'01dc', PICTURES[0]), chunk(b'LIST', b'rec ')):
        with pytest.raises(ValueError, match='Unsupported'):
            AviPictureParser().feed(avi([])+packet)


def test_declared_parent_boundaries_are_enforced():
    data = bytearray(avi(PICTURES, pipe=False))
    index = data.index(b'movi')-4
    struct.pack_into('<I', data, index, 6)
    with pytest.raises(ValueError, match='movi boundary'):
        AviPictureParser().feed(data)


def test_index_follows_finite_movie_and_truncated_eof_requires_reset():
    data = bytearray(avi(PICTURES, pipe=False))
    index = chunk(b'idx1', bytes(16))
    struct.pack_into('<I', data, 4, len(data)-8+len(index))
    parser = AviPictureParser()
    assert parser.feed(data+index) == PICTURES
    parser.finish()
    movie_size_offset = data.index(b'movi')-4
    size = struct.unpack_from('<I', data, movie_size_offset)[0]
    struct.pack_into('<I', data, movie_size_offset, size+len(index))
    with pytest.raises(ValueError, match='index must follow'):
        AviPictureParser().feed(data+index)
    parser.reset()
    parser.feed(b'RIFF')
    with pytest.raises(ValueError, match='Truncated'):
        parser.finish()
    with pytest.raises(ValueError, match='reset'):
        parser.feed(avi(PICTURES))
