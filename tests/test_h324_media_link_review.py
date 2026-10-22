"""Independent review checks for root's finite media integration."""
from pathlib import Path
from h324.h223 import Framer, Table, Slot, Demultiplexer
from h324.link import Link, Session
from h324.media import load_fixture
from h324.v80 import shield

FIXTURE = Path(__file__).parent / 'fixtures/media'


def test_priority_changes_only_at_complete_fragment_with_video_reassembly():
    link = Link(lambda *args,**kwargs:None)
    for mc in (1,2):
        link.tx_table.install(mc,[Slot(mc)])
    video = bytes(range(100))
    link.queue_sdu(2,video,2,True)
    # Consume startup flags and only the beginning of first video fragment.
    wire = bytearray(link.pull(20))
    link.queue_sdu(1,b'audio',1,False)
    link.queue_sdu(0,b'control',0,True)
    while link.pending:
        wire.extend(link.pull(7))
    # Continuous idle completes any closing flag straddling the final octet.
    wire.extend(link.pull(16))
    pdus = Framer().feed(wire)
    assert [(p.mc,len(p.information)) for p in pdus] == [(2,48),(0,7),(1,5),(2,48),(2,4),(2,0)]
    table = Table(); table.install(1,[Slot(1)]); table.install(2,[Slot(2)])
    demux = Demultiplexer(table,{0,2},{0,1,2})
    decoded = [s for p in pdus for s in demux.feed(p)]
    assert {(s.lcn,s.payload) for s in decoded} == {(0,b'control'),(1,b'audio'),(2,video)}
    assert not demux.events


def test_two_finite_sessions_schedule_codec_data_and_replay_without_duplicate_delivery():
    fixture = load_fixture(FIXTURE)
    sessions = [Session(str(i),i,stage='media',fixture=fixture,status_number=100+i) for i in (0,1)]
    for session in sessions:
        session.start(0)
    #18wirebytes/10ms equals14400bits/sec, simultaneously in each direction.
    for step in range(15000):
        now = step / 100
        for session in sessions:
            session.tick(now)
        wires = [session.link.pull(18) for session in sessions]
        for i,session in enumerate(sessions):
            session.feed(shield(wires[1-i]),now)
        assert not any(s.failure for s in sessions), [s.failure for s in sessions]
        if all(s.complete for s in sessions):
            break
    else:
        raise AssertionError([s.summary() for s in sessions])
    for session in sessions:
        assert len(session.received_media) == len(fixture)
        assert session.retransmission_verified
        assert session.al3_tx.counts['retransmitted'] == 1
        assert not session.media_errors
        for lcn in (1,2):
            expected = [s.payload for s in fixture if s.lcn == lcn]
            assert [s['payload'] for s in session.received_media if s['lcn'] == lcn] == expected
        assert not session.link.framer.errors and not session.link.demux.events
