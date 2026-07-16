# openlamp/matrix

> Compose **N WLED devices into one canvas**, driven from MIDI — the
> [wled-midi](https://github.com/openlamp/wled-midi) answer to the club / large-rig use case.

Many WLED instances that should behave as one surface. Point your DAW or controller at one
virtual MIDI port; this router fans out to every device. Runs on a Raspberry Pi.

## Two modes

- **`mirror`** — the **same** wled-midi look/state hits **every** device (broadcast), via each one's
  HTTP JSON API (`POST /json/state`). Event-driven, low rate. *"All strips flash red on the beat."*
- **`unified`** — the devices form **one pixel canvas** (concatenated left→right by `offset`). MIDI
  paints **positions** on the global canvas (strip semantics: interpolate / keymap / direct, with
  velocity→brightness, note-off fade and channel→hand colour), and each device is streamed **only its
  slice** via a WLED realtime transport — **`ddp`** (UDP 4048, default) or **`artnet`** (ArtDmx, UDP
  6454, 170 px/universe; set `"universe"` per device) — no HTTP rate cap. *"A note sweeps a light
  across the whole club."* Pick via `"transport": "ddp" | "artnet"`.

## Setup

1. `pip install python-rtmidi`
2. Run once — `python3 matrix.py` — it writes `matrix-config.json`, then edit it:
   ```json
   {
     "mode": "unified",
     "devices": [
       {"host": "192.168.1.50", "leds": 300, "offset": 0},
       {"host": "192.168.1.51", "leds": 300, "offset": 300}
     ],
     "fps": 40,
     "strip": {"posfn": "interpolate", "lo": 21, "hi": 108, "color": [0,255,128], "fade_ms": 250}
   }
   ```
   - `offset` = where this device starts in the global canvas (px). Total canvas = Σ `leds`.
   - **unified** needs each WLED in a realtime-friendly state; DDP overrides the segment while packets
     flow. **mirror** just needs the HTTP API reachable.
3. Route your DAW/controller MIDI to the **`OpenLampMatrix`** virtual port.

## How the DDP framing works

Per the [DDP spec](https://kno.wled.ge/interfaces/ddp/) as implemented by real WLED drivers:
each frame, every device gets its canvas slice as DDP packets — header
`flags(0x40 | 0x01 push on last) · seq(frame%15+1) · type(0x0B RGB8) · id(1) · offset(4B BE) · len(2B BE)`,
RGB payload, chunked at 480 px (1440 B) per packet, to UDP 4048. The canvas send is skipped when
nothing changed (idle = no traffic); fades keep sending until pixels reach black.

## Status

**Untested against real WLED hardware** (rig offline). The DDP framing + chunking + per-device canvas
slicing are **self-tested at the byte level** (`matrix.py` ships that test in its commit). Verify on a
device before a show — a wrong byte fails silently on the wire.

## Credits

Part of [OpenLamp](https://github.com/openlamp) / [wled-midi](https://github.com/openlamp/wled-midi).
DDP framing modelled on the [LedFx](https://github.com/LedFx/LedFx) reference. MIT licensed.
