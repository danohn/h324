# Alpha validation record

This is an experimental implementation of one finite H.324 profile. Installing a
package successfully does not establish compatibility with a modem or another
manufacturer's H.324 terminal.

## Automated software checks

The [core matrix run](https://github.com/danohn/h324/actions/runs/39719024612)
passed **191 tests per job** against installed wheels on all twelve combinations
of Python 3.11, 3.12, 3.13 and 3.14 with Ubuntu 24.04, macOS 15 and Windows 2022.
Checks run outside the source checkout. The library environment includes neither
Qt nor FFmpeg. CLI startup, worker startup and the installed H.245 resource are
checked separately from protocol tests.

The unchanged local lab archive still passes its original **191-test suite**.
The extracted projects retain the portable application/protocol tests; historical
V.34 experiments, PBX utilities and capture-analysis diagnostics remain in that
archive. Test totals across the new projects must not be described as a direct
replacement of all 191 historical tests. The current core total consists of 141
migrated tests and 50 new tests; its equality to the archive total is coincidental.

## Physical macOS check

On macOS 27.0.1 (Apple Silicon), two USB CX93001 modems connected through analogue
FXS ports and a local telephone switch completed the themed application's
bidirectional file call at an explicitly reported **14,400 bps** in both
directions. One endpoint sent twice, with pause/resume during its first send.
The received G.723.1 and H.263 streams matched the selected files byte for byte.
Both endpoints decoded audio and video, and both confirmed restoration to idle
and release of the serial port. Raw recordings used for this local acceptance
check are not distributed.

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

Physical Windows modem verification is pending the maintainer's manual test.
Physical Linux modem and capture-device verification is **unverified** until
hardware is available. Native launch and camera/microphone results belong to the
client's validation record. Cross-vendor H.324 terminal interoperability, other
modem chipsets, V.34 and higher bearer rates are outside the verified profile.
