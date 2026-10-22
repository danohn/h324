from h324.session import StreamingSession
from h324.v80 import shield
import pytest


class Pair:
    def __init__(self):
        self.events = [[], []]
        self.sessions = [StreamingSession(lambda _i=i, **e: self.events[_i].append(e), status_number=i+1) for i in range(2)]
        self.now = 0
        for s in self.sessions: s.start(0)
        self.pump(3)
        assert all(s.ready for s in self.sessions)
    def pump(self, duration):
        for _ in range(round(duration/.02)):
            self.now += .02
            for s in self.sessions: s.tick(self.now)
            wire = [s.link.pull(36) for s in self.sessions]
            for i in range(2): self.sessions[1-i].feed(shield(wire[i]), self.now)
            assert not any(s.failure for s in self.sessions)


def test_complete_picture_admission_segmentation_and_audio():
    pair = Pair()
    sender = pair.sessions[0]
    sender.start_live(pair.now)
    picture = b'\0\0\x80'+b'v'*297
    assert sender.send_live(2, picture, pair.now)
    assert sender.send_live(1, bytes(24), pair.now)
    pair.pump(1)
    assert b''.join(e['payload'] for e in pair.events[1] if e['event']=='media' and e['lcn']==2) == picture
    assert len([e for e in pair.events[1] if e['event']=='media' and e['lcn']==1]) == 1
    sender.stop_media(pair.now)
    assert not sender.media_active
    assert pair.sessions[1].ready  # local source-end is not remote EOF


def test_overload_rejects_whole_picture_and_audio_continues():
    pair = Pair()
    sender = pair.sessions[0]
    sender.start_live(pair.now)
    huge = b'\0\0\x80'+b'v'*1997
    before = sender.al3_tx.vs
    assert not sender.send_live(2, huge, pair.now)
    assert sender.al3_tx.vs == before
    assert sender.send_live(1, bytes(24), pair.now)
    pair.pump(1.2)
    assert not [e for e in pair.events[1] if e['event']=='media' and e['lcn']==2]
    assert any(e['event']=='video_overload' for e in pair.events[0])
    assert any(e['event']=='video_reset' for e in pair.events[0])
    assert any(e['event']=='media' and e['lcn']==1 for e in pair.events[1])


def test_live_pause_resume_and_protocol_sequence_wrap():
    pair = Pair()
    sender = pair.sessions[0]
    sender.start_live(pair.now)
    sender.pause(pair.now)
    assert not sender.send_live(1, bytes(24), pair.now)
    sender.resume(pair.now)
    for i in range(150):
        picture = b'\0\0\x80'+bytes([i%256])*10
        assert sender.send_live(2, picture, pair.now)
        pair.pump(.5)
    assert pair.sessions[1].video_next == 150%128
    assert pair.sessions[1].counts['video_sdus'] == 150
    assert sender.link.high_water < 1536
