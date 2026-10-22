# Alpha validation record

This is an experimental implementation of one finite H.324 profile. Installing a
package successfully does not establish compatibility with a modem or another
manufacturer's H.324 terminal.

## Automated software checks

The [core matrix run](https://github.com/danohn/h324/actions/runs/39727978028)
passed **284 tests per job** against installed wheels on all twelve combinations
of Python 3.11, 3.12, 3.13 and 3.14 with Ubuntu 24.04, macOS 15 and Windows 2022.
Checks run outside the source checkout. The library environment includes neither
Qt nor FFmpeg. CLI startup, worker startup and the installed H.245 resource are
checked separately from protocol tests.

The unchanged local lab archive still passes its original **191-test suite**.
The extracted projects retain the portable application/protocol tests; historical
V.34 experiments, PBX utilities and capture-analysis diagnostics remain in that
archive. Test totals across the new projects must not be described as a direct
replacement of all 191 historical tests. The current core total consists of 141
migrated tests and 143 new tests.

## Physical macOS check

On macOS 27.0.1 (Apple Silicon), two USB CX93001 modems connected through analogue
FXS ports and a local telephone switch completed the themed application's
bidirectional file call at an explicitly reported **14,400 bps** in both
directions. One endpoint sent twice, with pause/resume during its first send.
The received G.723.1 and H.263 streams matched the selected files byte for byte.
Both endpoints decoded audio and video, and both confirmed restoration to idle
and release of the serial port. Raw recordings used for this local acceptance
check are not distributed.

A final regression on core `3bac341` and client `eed05ea`, with freshly prepared
six-second clips, repeated this byte-exact result in 71.22 seconds including
setup and cleanup. It exercised the physical worker's 600 ms nominal final-drain
allowance, pause/resume and a second send. Both audio/video streams matched the
expected concatenated source bytes, both receivers decoded media, both observed
bearers were 14,400 bps, and both serial ports were independently checked free.
The drain allowance is elapsed nominal time, not a receiver acknowledgement.

One earlier startup attempt failed before H.245 negotiation with an H.223 framing
error. The captured flag stream contained a seven-bit flag interval during
initial acquisition; subsequent captured bits matched the transmitted stream.
The application aborted and restored the modems. The physical worker now sends a
one-second idle preamble and uses a bounded acquisition phase requiring eight
full flags and the first valid control PDU. It records rejected startup candidates
separately, then enforces strict framing. Acquisition is limited to five seconds
and 72,000 received bits. A subsequent physical camera call recorded one rejected
initial candidate, acquired framing and negotiated normally. The original failed
attempts remain part of the evidence; post-acquisition errors remain fatal.

V.80 in-band rates and initial AT carrier reports are distinct evidence. An
unspecified in-band rate stays unknown. The separately named `carrier_report`
requires consistent `+MCR`, `+MRR` and `CONNECT` result codes; it never uses the
requested modem configuration as a measurement.

A subsequent sustained physical file/camera call delivered 9,377 live audio
frames before a local transmit underrun, with the peer detecting damaged framing
afterward. Both modems restored successfully. This failed the ten-minute hardware
criterion and publishing was stopped before upload. The initial acquisition fix
and successful short file/API calls do not establish sustained pacing reliability;
the subsequent pacing investigation and successful retry are recorded below.

A second sustained attempt with a per-call macOS `NSProcessInfo` activity guard
failed after approximately 254 seconds of live capture. Both in-band and initial
AT reports identified 14,400 bps in both directions. Bounded timing evidence
showed clustered host/USB delays immediately before the underrun, including
44 ms and 51 ms loop gaps and increasing transmit deadline debt. Both modems
restored successfully. The guard was independently observed active, so this
result does not establish App Nap as the cause or the guard as a pacing fix.
The host-time buffer estimate is not a measurement of modem buffer occupancy.

Bounded debt batching alone also failed a subsequent live call after about 247
seconds of capture when a single 108 ms host gap exhausted the available lead.
A further instrumented attempt failed after about 62 seconds of capture. Its
recorded garbage-collection pauses peaked at 14–18 ms and did not coincide with
the fatal gap, so GC was neither disabled nor frozen. These results do not
support a fixed four-minute activity expiry or a complete fix from QoS alone.

A separate transparent V.80 diagnostic sent 1,025-, 2,049- and 4,096-byte framed
payloads in both directions. All six arrived byte for byte without overruns or
framing errors. About 2 KB was accepted in under 1 ms; the approximately 4 KB
writes blocked for about 980 ms while draining, demonstrating buffering and
backpressure on this USB/modem path. Intentional gaps between diagnostic frames
produced expected idle underruns; this was not an H.223 acceptance call. The
physical worker now uses an independent transmitter and a bounded 900-wire-octet
prime, with verified per-thread scheduling requests. This adds nominally 500 ms
of nominal lead, rather than a measurement of modem buffer occupancy.

The subsequent physical file/camera test, using the independent transmitter from
`4e864d8`, passed **600.04 seconds of actual live capture** at explicitly reported
14,400 bps in both directions. It recorded no active transmit underruns,
overruns, H.223 framing errors or cleanup errors. The live encoder produced
19,953 G.723.1 frames, approximately 99.76% of the expected count. The receiver
obtained 19,936 audio frames and decoded 343 H.263 pictures; the small buffered
tail was not fully delivered before hangup. Both directions received and decoded
audio and video, and both modems restored and released their ports.

This run included 164 video overload pauses/restarts while audio continued.
Transmit queue high-water marks were 362 and 714 bytes against an 8,192-byte
limit. Actual transmitter service gaps still reached approximately 167 ms and
135 ms, despite verified scheduling requests. The extra buffering and independent
writer accommodated those observed gaps; scheduling priority is not a real-time
guarantee. This establishes one ten-minute result on this hardware profile,
not indefinite clock stability or support for arbitrary modems.

## Installed async API physical check

A wheel built from `5b12998` was installed into a clean Python 3.14 environment
and used outside either repository, without Qt or FFmpeg in that environment.
Two `AsyncModem` contexts independently negotiated a physical call and sent
different six-second prepared streams simultaneously. Event consumers ran
concurrently, and the caller paused and resumed its source once.

| Measurement | Endpoint A | Endpoint B |
|---|---:|---:|
| Actual transmit and receive bearer rate | 14,400 bps | 14,400 bps |
| Received G.723.1 frames / bytes | 200 / 4,800 | 200 / 4,800 |
| Received H.263 pictures / AL3 SDUs | 12 / 15 | 12 / 15 |
| Received H.263 bytes | 572 | 594 |
| Codec bytes equal the peer's selected source | Yes | Yes |
| Active transmit underruns | 0 | 0 |
| H.223 framing/demultiplexer errors | 0 | 0 |
| Transmit queue high-water mark | 362 bytes | 362 bytes |
| Modem restoration confirmed | Yes | Yes |

The entire operation, including connection setup and restoration, took
**58.98 seconds**. Both calls hung up, both worker contexts exited, and both
serial ports were independently confirmed released. This validates the
installed async API and encoded-media lifecycle on the same macOS/CX93001
hardware profile; it does not imply camera or independent-terminal acceptance.
Raw evidence remains in the private lab archive.

## Verification boundaries

A subsequent camera/microphone call used the native Windows x64 and macOS Apple
Silicon candidates from core `4e864d8` and client `eed05ea`. The maintainer
reported Windows 11, modem COM6, and working incoming picture and sound.
Mac-side metadata recorded 608.61 seconds of continuous outgoing audio, 20,287
30 ms audio frames, 700 outgoing pictures, and 21,093 received audio frames with
1,172 received picture starts. Actual rate evidence was 14,400 bps. There were
no active underruns, overruns, H.223 framing/reassembly errors or session failure;
the transmit queue peaked at 685 bytes. Mac restoration and port release were
confirmed. The subsequently supplied Windows log independently confirmed 14,400
bps, 633.75 seconds of consecutive outgoing audio before the hangup error,
20,283 received audio frames and 700 received picture starts. Windows restoration
also succeeded, with its priority/timer settings released. Its exact OS build
and modem driver remain unverified.

At Mac hangup, Windows recorded one H.223 abort sequence with a partial video
SDU. Capture then continued while an error dialog was open, producing 246 stale
"No connected call" errors. Both modems restored, but orderly remote disconnect
acceptance **failed on that build**. Core `3547027` now
drains accepted media, closes H.245 channels and exchanges EndSession before
modem teardown. Automated delayed-transport tests cover either channel owner,
simultaneous closure and bounded cleanup. The corrected physical retest is
recorded below; it does not erase the earlier failure.

The corrected retest used the Windows native `preview-20261022-2` candidate and
a Mac source application harness with the same frozen core `3547027` and client
`812676b` runtime. It requested 60 seconds of Mac capture and recorded **65.10
seconds**. Mac-side evidence reports actual 14,400 bps, 1,291 received audio
frames and 129 received video AL-SDUs, with 64 decoded pictures and 589,440 bytes
of decoded PCM. It recorded no session errors, active underruns, overruns or
H.223 framing/reassembly errors.

Mac sent and received EndSession, completed with
`close.outcome="peer_end_received"`, stopped its transmitter and capture, and
returned to idle without a forced shutdown. The subsequent modem restoration
phase took **7.657 seconds**, confirmed cleanup and did not exceed its shared
12-second budget. The Mac serial port was independently checked released. The
maintainer separately confirmed that the Windows GUI returned to Idle, showed
no error and stopped capture. The corrected Windows log has **not** been
independently inspected, so its internal EndSession exchange and cleanup
telemetry are not claimed here.

The maintainer still reports **several seconds of audio/video delay**. Mac
capture emitted 2,117 audio frames against 2,170 expected (approximately 97.6%),
and only six outgoing video pictures, with 29 source video-budget pauses. Thus
the corrected call establishes the observed disconnect recovery and continued
media operation, not low latency, a sustained video frame rate or lossless live
capture. Video delay and repeated encoder overload pauses remain unresolved
client limitations. This short retest is separate from the earlier ten-minute
soak; it is not another ten-minute result.

Physical Linux modem and capture-device verification is **unverified** until
hardware is available. Native launch and camera/microphone results belong to the
client's validation record. Cross-vendor H.324 terminal interoperability, other
modem chipsets, V.34 and higher bearer rates are outside the verified profile.
