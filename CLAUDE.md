# CLAUDE.md — openlamp/matrix

A MIDI-driven router that composes **N WLED devices into one canvas**. Part of the
[wled-midi](https://github.com/openlamp/wled-midi) convention. Single file: `matrix.py`.

## Two modes, two transports

- **mirror** → same wled-midi state to every device via **HTTP** `POST /json/state` (event-driven).
- **unified** → one pixel canvas; MIDI strip-paints positions; each device streamed its slice via
  **DDP** (UDP 4048) at `fps`. The canvas send is skipped when unchanged (idle = silent); fades
  keep it going until black.

## DDP framing — the contract (don't break it)

Header = `struct.pack("!BBBBLH", flags, seq, 0x0B, id, offset, length)`:
- `flags = 0x40 (VER1) | 0x01 (PUSH)` — PUSH only on the **last** chunk of a device's frame.
- `seq = frame % 15 + 1` (1–15); `type = 0x0B` (RGB 8-bit); `id = 1` (default output).
- `offset`/`length` are **byte** offsets into the device's own buffer, big-endian.
- Chunk at **480 px = 1440 B** per packet. Port **4048**.

Modelled on LedFx's WLED driver (a verified real-world sender). If you touch the framing, re-run the
byte-level self-test that shipped in the initial commit (header values, chunk split at 1440 B,
per-device canvas slicing) before pushing.

## Untested on hardware

No WLED rig in the dev loop → DDP is spec-correct + self-tested but **unverified on a device**. A
wrong byte fails silently on the wire. Say so in any status claim; verify before a show.

## Position math

Reuses wled-midi strip semantics over the FULL canvas: `interpolate` (note range → whole canvas),
`keymap` (LEDs-per-key), `direct` (note = index). Keep it identical to the engine / wled-midi-web
`strip` implementations so a note lands on the same relative position everywhere.
