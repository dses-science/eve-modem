#!/usr/bin/env python
"""Read the bench HP/Agilent 8595E spectrum analyzer's marker over the testbench Prologix
GPIB-Ethernet adapter and report the amplifier output power through the output pad.

The driver amplifier on the control-station tray is terminated through a 40 dB power
attenuator into the 8595E (Rick's hookup of 2026-10-09), so the marker amplitude plus the
pad loss is the amplifier output. This is the manual half of design item O18 (meter
integration into the signal-generator sweep report): run it after each KEY / gain step of
the Signal generator mode and keep the printed table.

Lab rules (C:/CNS-Systems/TEST_EQUIPMENT.md) as applied here:
  * the analyzer's measurement set-up (center, span, RBW, reference level, attenuation) is
    never changed - the operator sets it at the front panel; this tool only READS it back
    and prints it so the record carries it;
  * the marker is a reading, not a configuration: `--peak` performs a peak search (MKPK HI)
    before reading, which moves the marker - use it only with the operator's OK;
  * one connection per visit, the analyzer is returned to local (++loc) and the socket
    closed before the tool exits.

Usage (any python with the standard library):
  python tools/eve_8595e.py                 read the marker as it stands
  python tools/eve_8595e.py --peak          peak search first (operator's OK)
  python tools/eve_8595e.py --pad 40.0 --gain 45 --note "CW 1299.5"
        also print the amplifier output (marker + pad) and tag the row with the B210 TX gain
  python tools/eve_8595e.py --setup         print the analyzer's set-up only (no marker)
Exit status 2 when the marker is off (the 8595E answers 999999999999).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import socket
import sys
import time

PROLOGIX = ("192.168.10.67", 1234)   # testbench bus
GPIB_ADDR = 18
MARKER_OFF = 999999999999.0


class Prologix:
    """Minimal Prologix GPIB-Ethernet session: controller mode, one address, manual reads."""

    def __init__(self, host_port=PROLOGIX, addr=GPIB_ADDR, timeout=5.0):
        self.s = socket.create_connection(host_port, timeout=timeout)
        for c in ("++mode 1", f"++addr {addr}", "++auto 0", "++eoi 1", "++eos 0",
                  "++read_tmo_ms 1000"):
            self.cmd(c)
            time.sleep(0.03)

    def cmd(self, text: str) -> None:
        self.s.sendall((text + "\n").encode())

    def read(self, timeout=2.0) -> str:
        self.s.settimeout(timeout)
        out = b""
        try:
            while True:
                chunk = self.s.recv(4096)
                if not chunk:
                    break
                out += chunk
                if out.endswith(b"\n"):
                    break
        except socket.timeout:
            pass
        return out.decode(errors="replace").strip()

    def query(self, text: str) -> str:
        self.cmd(text)
        self.cmd("++read eoi")
        return self.read()

    def close(self) -> None:
        try:
            self.cmd("++loc")          # hand the analyzer back to the front panel
        finally:
            self.s.close()


def read_setup(p: Prologix) -> dict:
    """The analyzer's measurement set-up, read back (never written)."""
    keys = {"CF?": "center_hz", "SP?": "span_hz", "RB?": "rbw_hz", "VB?": "vbw_hz",
            "RL?": "ref_level_dbm", "AT?": "atten_db", "ST?": "sweep_s", "AUNITS?": "units"}
    out = {"id": p.query("ID?")}
    for q, k in keys.items():
        v = p.query(q)
        try:
            out[k] = float(v)
        except ValueError:
            out[k] = v
    return out


def read_marker(p: Prologix, peak: bool = False) -> tuple[float, float]:
    """(marker frequency Hz, marker amplitude dBm); MARKER_OFF values when the marker is off."""
    if peak:
        p.cmd("MKPK HI;")
        time.sleep(0.3)
    f = float(p.query("MKF?"))
    a = float(p.query("MKA?"))
    return f, a


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--peak", action="store_true", help="peak search (MKPK HI) before reading - moves the marker")
    ap.add_argument("--pad", type=float, default=None, help="output pad loss in dB (added to the marker reading)")
    ap.add_argument("--gain", type=float, default=None, help="B210 TX gain of this step (dB), for the record")
    ap.add_argument("--note", default="", help="free text for the record")
    ap.add_argument("--setup", action="store_true", help="print the analyzer set-up only")
    a = ap.parse_args(argv)

    p = Prologix()
    try:
        su = read_setup(p)
        if not su["id"].startswith("HP8595"):
            print(f"unexpected instrument at GPIB {GPIB_ADDR}: {su['id']!r}", file=sys.stderr)
            return 1
        print(f"{su['id']}: center {su['center_hz']/1e6:.6f} MHz span {su['span_hz']/1e6:.3f} MHz "
              f"RBW {su['rbw_hz']/1e3:.0f} kHz VBW {su['vbw_hz']/1e3:.0f} kHz ref {su['ref_level_dbm']:+.1f} dBm "
              f"att {su['atten_db']:.0f} dB sweep {su['sweep_s']*1e3:.0f} ms")
        if a.setup:
            return 0
        f, amp = read_marker(p, peak=a.peak)
    finally:
        p.close()

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if f >= MARKER_OFF or amp >= MARKER_OFF:
        print(f"{stamp} marker OFF (turn it on at the front panel, or use --peak with the operator's OK)")
        return 2
    row = f"{stamp} marker {f/1e6:.6f} MHz {amp:+.2f} dBm"
    if a.pad is not None:
        pout = amp + a.pad
        row += f" | pad {a.pad:.1f} dB -> amplifier output {pout:+.2f} dBm = {10 ** (pout / 10) / 1e3:.3f} W"
    if a.gain is not None:
        row += f" | TX gain {a.gain:.1f} dB"
    if a.note:
        row += f" | {a.note}"
    print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
