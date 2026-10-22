# Alpha validation record

This is an experimental implementation of one finite H.324 profile. Installing a
package successfully does not establish compatibility with a modem or another
manufacturer's H.324 terminal.

## Automated software checks

The [core matrix run](https://github.com/danohn/h324/actions/runs/39717829292)
passed **165 tests per job** against installed wheels on all twelve combinations
of Python 3.11, 3.12, 3.13 and 3.14 with Ubuntu 24.04, macOS 15 and Windows 2022.
Checks run outside the source checkout. The library environment includes neither
Qt nor FFmpeg. CLI startup, worker startup and the installed H.245 resource are
checked separately from protocol tests.

The unchanged local lab archive still passes its original **191-test suite**.
The extracted projects retain the portable application/protocol tests; historical
V.34 experiments, PBX utilities and capture-analysis diagnostics remain in that
archive. Test totals across the new projects must not be described as a direct
replacement of all 191 historical tests.

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
The application aborted and restored the modems. Framing errors remain fatal;
this failure is retained as a known startup-reliability limitation.

## Verification boundaries

Physical Windows modem verification is pending the maintainer's manual test.
Physical Linux modem and capture-device verification is **unverified** until
hardware is available. Native launch and camera/microphone results belong to the
client's validation record. Cross-vendor H.324 terminal interoperability, other
modem chipsets, V.34 and higher bearer rates are outside the verified profile.
