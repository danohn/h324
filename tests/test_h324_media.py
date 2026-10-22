import hashlib
import json
from pathlib import Path
import pytest
from h324.media import (g723_frames, h263_pictures, load_fixture, adapt,
                             budget, media_pdus)
from h324.h223 import (al2_decode, al3_decode, Table, Slot, Demultiplexer,
                        Framer, encode_pdu)

FIXTURE = Path(__file__).parent / 'fixtures/media'


def test_g723_frame_type_sizes_and_truncation():
    data = b'\0'*24 + b'\1'+b'\0'*19 + b'\2'+b'\0'*3 + b'\3'
    assert list(map(len,g723_frames(data))) == [24,20,4,1]
    with pytest.raises(ValueError): g723_frames(b'\0'*23)


def test_h263_picture_boundaries_and_fragment_alignment():
    pictures = [b'\0\0\x80'+b'a'*120, b'\0\0\x83'+b'b'*2]
    assert h263_pictures(b''.join(pictures)) == pictures
    with pytest.raises(ValueError): h263_pictures(b'x'+pictures[0])
    actual = h263_pictures((FIXTURE/'video.h263').read_bytes())
    sdus = [s for s in load_fixture(FIXTURE) if s.codec == 'h263']
    assert len(actual) == 12
    assert len([s for s in sdus if s.picture_start]) == 12
    assert all(s.payload[:2] == b'\0\0' for s in sdus if s.picture_start)
    assert all(len(s.payload) <= 96 for s in sdus)
    for i, picture in enumerate(actual):
        assert b''.join(s.payload for s in sdus if s.timestamp_ms == i*500) == picture


def test_real_fixture_timestamps_adaptation_crc_and_unchanged_codec_bytes():
    sdus = load_fixture(FIXTURE)
    audio = [s for s in sdus if s.codec == 'g723_1']
    assert len(audio) == 200
    assert [s.timestamp_ms for s in audio] == list(range(0,6000,30))
    assert all(len(s.payload) == 24 and s.payload[0]&3 == 0 for s in audio)
    assert b''.join(s.payload for s in audio) == (FIXTURE/'audio.g723').read_bytes()
    for s in sdus:
        encoded = adapt(s,3)
        decoded = al2_decode(encoded,False) if s.codec == 'g723_1' else al3_decode(encoded,1)
        assert decoded == (s.payload,None if s.codec == 'g723_1' else 3)
        corrupt = bytearray(encoded); corrupt[-1] ^= 1
        with pytest.raises(ValueError):
            (al2_decode(corrupt,False) if s.codec == 'g723_1' else al3_decode(corrupt,1))


def test_exact_wire_budget_and_h223_recovery_with_arbitrary_feed_chunks():
    sdus = load_fixture(FIXTURE)
    profile = budget(sdus)
    assert profile['fits_average_budget'] and profile['remaining_bps'] > 4000
    assert not budget(sdus,line_bps=8000)['fits_average_budget']
    table = Table(); table.install(1,[Slot(1)]); table.install(2,[Slot(2)])
    demux = Demultiplexer(table,{0,2},{0,1,2})
    framer, decoded, wire = Framer(), [], ''
    expected = []
    sequence = 0
    for sdu, pdus in media_pdus(sdus):
        expected.append((sdu.lcn,adapt(sdu,sequence)))
        sequence += sdu.codec == 'h263'
        wire += ''.join(encode_pdu(pdu) for pdu in pdus)
    assert len(wire) == profile['exact_stuffed_media_bits']
    for i in range(0,len(wire),37):
        for pdu in framer.feed_bits(wire[i:i+37]):
            decoded.extend((s.lcn,s.payload) for s in demux.feed(pdu))
    assert decoded == expected
    assert not framer.errors and not demux.events


def test_fixture_manifest_matches_independently_decoded_media():
    manifest = json.loads((FIXTURE/'manifest.json').read_text())
    for name, metadata in manifest['files'].items():
        data = (FIXTURE/name).read_bytes()
        assert len(data) == metadata['bytes']
        assert hashlib.sha256(data).hexdigest() == metadata['sha256']
    assert manifest['verification']['audio_samples'] == 48000
    assert manifest['verification']['decoded_video_frames'] == 12
    assert manifest['verification']['unique_decoded_video_frames'] > 1
