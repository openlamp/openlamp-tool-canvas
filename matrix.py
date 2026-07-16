#!/usr/bin/env python3
"""OpenLamp Matrix — compose N WLED devices into one canvas, driven from MIDI.

Part of the wled-midi convention (github.com/openlamp/wled-midi). Solves the club / large-rig
ask: many WLED instances that should behave as one surface. Two modes:

  - "mirror"  — the SAME wled-midi look/state is applied to EVERY device (broadcast). Uses each
                device's HTTP JSON API (POST /json/state). Event-driven; low rate. Good for
                "all strips flash red on the beat".

  - "unified" — the devices form ONE pixel canvas (concatenated, left→right by `offset`). MIDI
                paints POSITIONS on the global canvas (strip semantics), and each device is
                streamed ONLY its slice via WLED realtime **DDP** (UDP 4048). High rate, no
                HTTP cap. Good for "a note sweeps a light across the whole club".

Runs happily on a Raspberry Pi. Opens a virtual MIDI input port (default "OpenLampMatrix").

DDP header follows the 3waylabs spec as implemented by LedFx (a real WLED driver):
  byte0 flags = 0x40 (VER1) | 0x01 (PUSH on last chunk); byte1 = seq (frame%15+1);
  byte2 = 0x0B (RGB 8-bit); byte3 = id (1); then !LH = 4-byte data offset + 2-byte length (BE);
  then RGB bytes. Chunked at 480 px (1440 B) per packet.

Run:  python3 matrix.py         (Ctrl-C to quit)
Config: matrix-config.json (auto-written next to this file).

NOTE: untested against real WLED hardware (rig offline). The DDP framing is spec-correct and
self-tested at the byte level, but verify on a device before a show.
"""
import json, os, time, socket, struct, threading, urllib.request
import rtmidi

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "matrix-config.json")

DEFAULT = {
    "port_name": "OpenLampMatrix",
    "mode": "unified",                       # "mirror" | "unified"
    "devices": [                             # canvas = devices concatenated by offset
        {"host": "192.168.1.50", "leds": 300, "offset": 0},
        {"host": "192.168.1.51", "leds": 300, "offset": 300},
    ],
    "transport": "ddp",                      # unified transport: "ddp" | "artnet" | "e131"
    "ddp_port": 4048,
    "artnet_port": 6454,
    "e131_port": 5568,
    "e131_priority": 100,
    "fps": 40,                               # unified: canvas send rate
    # unified painting = wled-midi strip semantics over the WHOLE canvas
    "strip": {
        "posfn": "interpolate",              # "interpolate" | "keymap" | "direct"
        "lo": 21, "hi": 108,                 # interpolate: note range across the full canvas
        "lpk": 2.0, "firstnote": 21,         # keymap/direct calibration
        "color": [0, 255, 128],
        "hand_colors": {},                   # channel -> colour (Synthesia L/R): {"1":[0,120,255],"2":[0,255,120]}
        "velocity_to_bri": True,
        "fade_ms": 250,
    },
    # mirror = wled-midi lamp looks broadcast to every device
    "looks": {
        "59": [0, 0, 0], "60": [255, 0, 0], "61": [255, 85, 0], "62": [255, 200, 0],
        "63": [0, 255, 0], "64": [0, 200, 255], "65": [0, 0, 255], "66": [255, 0, 170],
        "67": [255, 255, 255],
    },
    "util": {"48": '{"on":false}', "50": '{"on":true}', "52": '{"on":"t"}'},
    "cc": {"1": "bri"},
}


def load_cfg():
    if os.path.exists(CONFIG):
        c = dict(DEFAULT); c.update(json.load(open(CONFIG))); return c
    json.dump(DEFAULT, open(CONFIG, "w"), indent=2)
    return dict(DEFAULT)


# ----- DDP transport (unified mode) -----

DDP_VER1, DDP_PUSH, DDP_TYPE_RGB8 = 0x40, 0x01, 0x0B
DDP_MAX_PIXELS = 480
DDP_MAX_BYTES = DDP_MAX_PIXELS * 3


def ddp_packets(pixels, seq, dst_id=1):
    """Yield DDP packets for one device's pixel bytes, chunked at 480 px, PUSH on the last."""
    total = len(pixels)
    off = 0
    while off < total or off == 0:                 # always emit ≥1 packet (even for 0 px = no-op)
        chunk = pixels[off:off + DDP_MAX_BYTES]
        last = (off + len(chunk) >= total)
        flags = DDP_VER1 | (DDP_PUSH if last else 0)
        header = struct.pack("!BBBBLH", flags, seq & 0x0F, DDP_TYPE_RGB8, dst_id, off, len(chunk))
        yield header + chunk
        off += len(chunk)
        if not chunk:
            break


# ----- Art-Net transport (unified mode; WLED-native alternative to DDP) -----

ARTNET_PORT = 6454
ARTNET_MAX_CH = 510                                 # 170 RGB pixels per universe (510 <= 512, even)


def artnet_packets(pixels, seq, universe_base=0):
    """Yield ArtDmx (OpOutput) packets for one device's pixel bytes, one universe per 170 px.
    Header per Art-Net 4: 'Art-Net\\0', OpCode 0x5000 (LE), ProtVer 14 (BE), seq, physical,
    SubUni + Net (15-bit universe), Length (BE), then DMX data."""
    total = len(pixels)
    off, uni = 0, universe_base
    while off < total or (off == 0 and total == 0):
        data = pixels[off:off + ARTNET_MAX_CH]
        if len(data) % 2:                            # DMX length must be even
            data = data + b"\x00"
        header = (b"Art-Net\x00"
                  + struct.pack("<H", 0x5000)        # OpDmx, little-endian
                  + struct.pack(">H", 14)            # protocol version 14, big-endian
                  + bytes([seq & 0xFF, 0])           # sequence, physical
                  + bytes([uni & 0xFF, (uni >> 8) & 0x7F])   # SubUni (low), Net (high 7 bits)
                  + struct.pack(">H", len(data)))    # data length, big-endian
        yield header + data
        off += ARTNET_MAX_CH
        uni += 1
        if not data:
            break


# ----- E1.31 / sACN transport (unified mode; WLED-native, DMX-over-IP) -----

E131_PORT = 5568
E131_ACN_ID = bytes([0x41, 0x53, 0x43, 0x2d, 0x45, 0x31, 0x2e, 0x31, 0x37, 0, 0, 0])  # "ASC-E1.17\0\0\0"
E131_CID = bytes(range(1, 17))                       # fixed 16-byte source CID (stable per sender)
E131_MAX_CH = 510                                    # 170 RGB pixels (510 <= 512 DMX slots)


def e131_packets(pixels, seq, universe, cid=E131_CID, source="OpenLamp Matrix", priority=100):
    """Yield E1.31 (sACN) data packets for one device's pixels, one universe per 170 px.
    Layout per ANSI E1.31: Root (ACN id, vector 4, CID) + Framing (vector 2, source, priority,
    seq, universe) + DMP (vector 2, addr-type 0xA1, start code 0 + DMX data). Flags = 0x7 in the
    top nibble of each 16-bit flags/length field."""
    total = len(pixels)
    off = 0
    while off < total or (off == 0 and total == 0):
        data = pixels[off:off + E131_MAX_CH]
        dlen = len(data)
        plen = 126 + dlen                            # total packet length (byte 125 = start code)
        pkt = bytearray(plen)
        struct.pack_into(">H", pkt, 0, 0x0010)       # preamble size (postamble stays 0)
        pkt[4:16] = E131_ACN_ID
        struct.pack_into(">H", pkt, 16, 0x7000 | (plen - 16))     # root flags & length
        struct.pack_into(">I", pkt, 18, 4)                        # VECTOR_ROOT_E131_DATA
        pkt[22:38] = cid
        struct.pack_into(">H", pkt, 38, 0x7000 | (plen - 38))     # framing flags & length
        struct.pack_into(">I", pkt, 40, 2)                        # VECTOR_E131_DATA_PACKET
        sn = source.encode()[:63]; pkt[44:44 + len(sn)] = sn      # source name (64 B, null-padded)
        pkt[108] = priority & 0xFF                                # priority (0-200, default 100)
        pkt[111] = seq & 0xFF                                     # sequence number
        struct.pack_into(">H", pkt, 113, universe)               # universe (1-63999)
        struct.pack_into(">H", pkt, 115, 0x7000 | (plen - 115))  # DMP flags & length
        pkt[117] = 2                                             # VECTOR_DMP_SET_PROPERTY
        pkt[118] = 0xA1                                          # address type & data type
        struct.pack_into(">H", pkt, 119, 0)                     # first property address
        struct.pack_into(">H", pkt, 121, 1)                     # address increment
        struct.pack_into(">H", pkt, 123, dlen + 1)              # property value count (start code + data)
        pkt[125] = 0                                            # DMX512 start code
        pkt[126:126 + dlen] = data
        yield bytes(pkt)
        off += E131_MAX_CH
        universe += 1
        if not data:
            break


class Matrix:
    def __init__(self, cfg):
        self.cfg = cfg
        self.total = sum(d["leds"] for d in cfg["devices"])
        self.canvas = bytearray(self.total * 3)        # unified pixel buffer (RGB)
        self.held = {}                                 # note -> {"leds":[..], "col":(r,g,b)}
        self.fading = {}                               # led -> {"col":(r,g,b), "t0":monotonic}
        self.lock = threading.Lock()
        self.dirty = True
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # --- shared: note pitch -> LED position(s) on the FULL canvas ---
    def _leds(self, note):
        s = self.cfg["strip"]; N = self.total; fn = s.get("posfn", "interpolate")
        if fn == "direct":
            i = note - int(s.get("firstnote", 0)); return [i] if 0 <= i < N else []
        if fn == "keymap":                             # LEDs-per-key — prior art: onlaj/Piano-LED-Visualizer (no affiliation)
            lpk = float(s.get("lpk", 2.0)); first = int(s.get("firstnote", 21))
            c = round((note - first) * lpk); span = max(1, round(lpk))
            return [i for i in range(c, c + span) if 0 <= i < N]
        lo = int(s.get("lo", 21)); hi = int(s.get("hi", 108))
        if hi <= lo: return []
        i = round((note - lo) / (hi - lo) * (N - 1)); return [i] if 0 <= i < N else []

    def _set(self, led, rgb):
        self.canvas[led * 3:led * 3 + 3] = bytes(rgb)

    # --- unified mode ---
    def unified_note_on(self, note, vel, chan=1):
        s = self.cfg["strip"]; leds = self._leds(note)
        if not leds: return
        r, g, b = s.get("hand_colors", {}).get(str(chan)) or s.get("color", [0, 255, 128])
        if s.get("velocity_to_bri", True):
            k = vel / 127.0; r, g, b = round(r * k), round(g * k), round(b * k)
        with self.lock:
            self.held[note] = {"leds": leds, "col": (r, g, b)}
            for i in leds:
                self.fading.pop(i, None); self._set(i, (r, g, b))
            self.dirty = True

    def unified_note_off(self, note):
        fade_ms = self.cfg["strip"].get("fade_ms", 0)
        with self.lock:
            e = self.held.pop(note, None)
            if not e: return
            if fade_ms and fade_ms > 0:
                now = time.monotonic()
                for i in e["leds"]:
                    self.fading[i] = {"col": e["col"], "t0": now}
            else:
                for i in e["leds"]:
                    self._set(i, (0, 0, 0))
                self.dirty = True

    def _fade_step(self):
        fade_ms = self.cfg["strip"].get("fade_ms", 0)
        if not fade_ms or fade_ms <= 0: return
        now = time.monotonic()
        with self.lock:
            if not self.fading: return
            done = []
            for i, st in self.fading.items():
                k = 1.0 - (now - st["t0"]) * 1000.0 / fade_ms
                if k <= 0:
                    self._set(i, (0, 0, 0)); done.append(i)
                else:
                    r, g, b = st["col"]; self._set(i, (round(r * k), round(g * k), round(b * k)))
            for i in done:
                del self.fading[i]
            self.dirty = True

    def frame(self):
        """One canvas send: stream each device its slice via DDP. Skips when nothing changed."""
        self._fade_step()
        with self.lock:
            if not self.dirty and not self.fading:
                return
            self.dirty = False
            snapshot = bytes(self.canvas)
        self.seq += 1
        transport = self.cfg.get("transport", "ddp")
        for d in self.cfg["devices"]:
            o = d["offset"] * 3
            slice_ = snapshot[o:o + d["leds"] * 3]
            if transport == "artnet":
                port = self.cfg.get("artnet_port", ARTNET_PORT)
                for pkt in artnet_packets(slice_, self.seq, d.get("universe", 0)):
                    self.sock.sendto(pkt, (d["host"], port))
            elif transport in ("e131", "e1.31", "sacn"):
                port = self.cfg.get("e131_port", E131_PORT)
                prio = self.cfg.get("e131_priority", 100)
                for pkt in e131_packets(slice_, self.seq, d.get("universe", 1), priority=prio):
                    self.sock.sendto(pkt, (d["host"], port))
            else:                                          # ddp (default)
                port = self.cfg.get("ddp_port", 4048)
                for pkt in ddp_packets(slice_, self.seq):
                    self.sock.sendto(pkt, (d["host"], port))

    # --- mirror mode ---
    def mirror_dispatch(self, msg):
        typ = msg[0] & 0xF0
        cmd = None
        if typ == 0x90 and msg[2] > 0:
            n = str(msg[1])
            if n in self.cfg.get("looks", {}):
                r, g, b = self.cfg["looks"][n]; cmd = '{"col":[%d,%d,%d],"fx":0}' % (r, g, b)
            elif n in self.cfg.get("util", {}):
                cmd = self.cfg["util"][n]
        elif typ == 0xB0 and self.cfg.get("cc", {}).get(str(msg[1])) == "bri":
            cmd = '{"bri":%d}' % round(msg[2] / 127 * 255)
        elif typ == 0xC0:
            cmd = '{"ps":%d}' % (msg[1] + 1)
        if cmd is None: return
        for d in self.cfg["devices"]:                  # broadcast to every device's HTTP API
            self._http_state(d["host"], cmd)

    def _http_state(self, host, body):
        try:
            req = urllib.request.Request("http://%s/json/state" % host,
                                         data=body.encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        except Exception as e:
            print("  ! %s unreachable: %s" % (host, e))

    def dispatch(self, msg):
        if self.cfg.get("mode") == "mirror":
            self.mirror_dispatch(msg); return
        typ, chan = msg[0] & 0xF0, (msg[0] & 0x0F) + 1  # unified
        if typ == 0x90 and msg[2] > 0:
            self.unified_note_on(msg[1], msg[2], chan)
        elif typ == 0x80 or (typ == 0x90 and msg[2] == 0):
            self.unified_note_off(msg[1])


def main():
    cfg = load_cfg()
    mx = Matrix(cfg)
    midi_in = rtmidi.MidiIn()
    midi_in.open_virtual_port(cfg["port_name"])

    def cb(event, data=None):
        try:
            mx.dispatch(event[0])
        except Exception as e:
            print("  ! dispatch error:", e)

    midi_in.set_callback(cb)

    if cfg.get("mode") == "unified":                   # run the canvas send loop
        fps = max(1, int(cfg.get("fps", 40)))
        def loop():
            while True:
                time.sleep(1.0 / fps)
                try:
                    mx.frame()
                except Exception as e:
                    print("  ! frame error:", e)
        threading.Thread(target=loop, daemon=True).start()

    print("OpenLamp Matrix — mode '%s', %d device(s), %d px canvas — port '%s' open." % (
        cfg.get("mode"), len(cfg["devices"]), mx.total, cfg["port_name"]))
    print("Route your DAW/controller MIDI to it. Ctrl-C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        midi_in.close_port()


if __name__ == "__main__":
    main()
