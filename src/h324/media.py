"""Real, pre-encoded H.324 lab media; no camera/microphone or live codec claim.

G.723.1: 8 kHz mono, 30 ms frames, AL2 without sequence octet.
H.263 (1996): SQCIF 128x96, 2 pictures/sec, AL3 one-octet control.
The fixture profile is a subset, not a complete H.324 terminal implementation.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from h324.avi import AviPictureParser

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from h324.h223 import (PDU, Multiplexer, Table, Slot, al2_encode, al3_encode,
                        encode_pdu)

AUDIO_FRAME_MS = 30
VIDEO_FRAME_MS = 500
PROFILE = dict(audio_codec='g723_1', audio_bit_rate=6300, audio_sample_rate=8000,
               audio_channels=1, audio_frame_ms=30, audio_al='al2WithoutSequenceNumbers',
               video_codec='h263', video_width=128, video_height=96, video_fps=2,
               video_al='al3', video_control_octets=1, video_send_buffer=1024)


@dataclass(frozen=True)
class MediaSDU:
    codec: str
    lcn: int
    timestamp_ms: int
    duration_ms: int
    payload: bytes
    picture_start: bool = False
    picture_end: bool = False


def g723_frames(data):
    """Parse native G.723.1 frame-type bits; reject partial final frames."""
    frames, offset = [], 0
    while offset < len(data):
        size = (24, 20, 4, 1)[data[offset] & 3]
        if offset + size > len(data):
            raise ValueError('truncated G.723.1 frame')
        frames.append(data[offset:offset+size])
        offset += size
    return frames


def h263_pictures(data):
    """Split octet-aligned baseline H.263 22-bit picture start codes.

    H.324 requires each picture start at an AL-SDU boundary. This parser is
    deliberately for the baseline byte-aligned encoder used by this fixture.
    """
    starts = [i for i in range(len(data)-2)
              if data[i:i+2] == b'\0\0' and data[i+2] & 0xfc == 0x80]
    if not starts or starts[0] != 0:
        raise ValueError('H.263 picture start required at offset zero')
    return [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)])]


def load_fixture(directory, max_video_sdu=96):
    """Return timestamped codec AL-SDUs, sorted by release time, audio first.

    Times are local scheduling metadata, never private bytes in the codec data.
    Each video fragment becomes one AL3 I-PDU; each picture begins a new SDU.
    """
    if not 1 <= max_video_sdu <= 4096:
        raise ValueError('max_video_sdu must be 1..4096')
    directory = Path(directory)
    result = [MediaSDU('g723_1', 1, i * AUDIO_FRAME_MS, AUDIO_FRAME_MS, frame)
              for i, frame in enumerate(g723_frames((directory/'audio.g723').read_bytes()))]
    for i, picture in enumerate(h263_pictures((directory/'video.h263').read_bytes())):
        for offset in range(0, len(picture), max_video_sdu):
            result.append(MediaSDU('h263', 2, i * VIDEO_FRAME_MS, VIDEO_FRAME_MS,
                picture[offset:offset+max_video_sdu], offset == 0,
                offset + max_video_sdu >= len(picture)))
    return sorted(result, key=lambda item: (item.timestamp_ms, item.lcn))


def adapt(sdu, sequence=0):
    if sdu.codec == 'g723_1':
        return al2_encode(sdu.payload)  # G.723.1: no AL2 sequence octet.
    if sdu.codec == 'h263':
        return al3_encode(sdu.payload, sequence % 128, 1)
    raise ValueError('unsupported fixture codec')


def media_pdus(sdus, max_mux_payload=48):
    """Yield (SDU, list[PDU]) with exact H.223 segmentation and PM closure.

    This conservative budget puts a PM-only closure after each video SDU.
    A live interleaving scheduler can carry PM in the next media PDU instead.
    """
    if max_mux_payload < 25:
        raise ValueError('MUX payload must fit one unsegmentable audio frame')
    table = Table()
    table.install(1, [Slot(1)])
    table.install(2, [Slot(2)])
    mux, video_sequence = Multiplexer(table, {0, 2}), 0
    for sdu in sdus:
        encoded = adapt(sdu, video_sequence)
        if sdu.codec == 'h263':
            video_sequence += 1
        pdus = []
        for offset in range(0, len(encoded), max_mux_payload):
            end = offset + max_mux_payload >= len(encoded)
            pdu = mux.send(sdu.lcn, encoded[offset:offset+max_mux_payload],
                           end_sdu=end and sdu.codec == 'h263')
            if pdu is not None:
                pdus.append(pdu)
        final = mux.finish()
        if final is not None:
            pdus.append(final)
        yield sdu, pdus


def budget(sdus, line_bps=14400, control_reserve_bps=1000):
    """Exact stuffed bits for this conservative MUX, plus explicit reservation.

    Mean budget is not proof of real-time service. Serialization lateness uses
    an offline FIFO release simulation, no retransmissions/control contention.
    V.80 shielding bytes occupy the USB link; they do not consume modem line bits.
    """
    if line_bps <= 0 or not 0 <= control_reserve_bps < line_bps:
        raise ValueError('invalid line capacity/control reservation')
    duration_ms = max((s.timestamp_ms + s.duration_ms for s in sdus), default=0)
    if not duration_ms:
        raise ValueError('nonempty media required')
    totals = {'g723_1': 0, 'h263': 0}
    wire_bits, pdu_count, cursor, max_lateness = 0, 0, 0.0, 0.0
    for sdu, pdus in media_pdus(sdus):
        n = sum(len(encode_pdu(pdu)) for pdu in pdus)
        totals[sdu.codec] += n
        wire_bits += n
        pdu_count += len(pdus)
        cursor = max(cursor, sdu.timestamp_ms) + n * 1000 / line_bps
        max_lateness = max(max_lateness, cursor - sdu.timestamp_ms)
    seconds = duration_ms / 1000
    required_bps = wire_bits / seconds
    return dict(duration_ms=duration_ms, codec_bytes=sum(len(s.payload) for s in sdus),
        al_sdus=len(sdus), mux_pdus=pdu_count, exact_stuffed_media_bits=wire_bits,
        media_wire_bps=required_bps, per_codec_wire_bps={k:v/seconds for k,v in totals.items()},
        line_bps=line_bps, control_reserve_bps=control_reserve_bps,
        remaining_bps=line_bps-required_bps-control_reserve_bps,
        fits_average_budget=required_bps+control_reserve_bps <= line_bps,
        offline_fifo_max_completion_lateness_ms=max_lateness,
        offline_fifo_final_completion_ms=cursor,
        limitations='No ARQ, actual H.245 traffic, codec execution or host/USB timing in this model')


MAX_DURATION_SECONDS = 60
LINE_BPS = 14400
CONTROL_RESERVE_BPS = 1000
MAX_VIDEO_BPS = 4800
MAX_LATE_MS = 1500
CODEC_FILES = ("audio.g723", "video.h263")

def _file_info(path: Path) -> dict:
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

def _validate(directory: Path) -> tuple[list[MediaSDU], dict]:
    sdus = load_fixture(directory, max_video_sdu=96)
    audio = [s for s in sdus if s.lcn == 1]
    pictures = h263_pictures((directory / 'video.h263').read_bytes())
    if not audio or not pictures:
        raise ValueError('Both audio and video streams must be nonempty.')
    if any(len(s.payload) != 24 or s.payload[0] & 3 for s in audio):
        raise ValueError('Audio must use the negotiated G.723.1 6.3 kbit/s mode.')
    duration_ms = len(audio) * 30
    if duration_ms != len(pictures) * 500 or duration_ms > MAX_DURATION_SECONDS * 1000:
        raise ValueError('Audio/video durations must match and be at most 60 seconds.')
    report = budget(sdus, LINE_BPS, CONTROL_RESERVE_BPS)
    reserved = budget(sdus, LINE_BPS - CONTROL_RESERVE_BPS, 0)
    report['reserved_fifo_max_completion_lateness_ms'] = reserved['offline_fifo_max_completion_lateness_ms']
    report['video_codec_bps'] = (directory / 'video.h263').stat().st_size * 8000 / duration_ms
    report['fits_video_capability'] = report['video_codec_bps'] <= MAX_VIDEO_BPS
    report['fits_burst_budget'] = reserved['offline_fifo_max_completion_lateness_ms'] <= MAX_LATE_MS
    if not (report['fits_average_budget'] and report['fits_video_capability'] and report['fits_burst_budget']):
        raise ValueError('This clip exceeds the 14,400 bps profile or its 1.5-second burst allowance.')
    return sdus, report

def load_bundle(path: str | Path) -> list[MediaSDU]:
    """Validate codec bytes, manifest integrity and line budget before loading.

    Existing checked lab fixtures are accepted too; a desktop bundle additionally
    records its source and conversion parameters. Paths come from the caller,
    never from manifest filenames.
    """
    directory = Path(path).expanduser().resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    for name in CODEC_FILES:
        expected = manifest.get('files', {}).get(name)
        if not expected or _file_info(directory / name) != expected:
            raise ValueError(f'{name} is missing or does not match the bundle manifest.')
    sdus, _ = _validate(directory)
    actual_ms = max(s.timestamp_ms + s.duration_ms for s in sdus)
    if manifest.get('duration_ms') != actual_ms:
        raise ValueError('Bundle duration does not match the codec streams.')
    return sdus
