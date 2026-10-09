#!/usr/bin/env python
"""Measure the driver amplifier's output power against the modem's TX gain with the bench
8595E spectrum analyzer: the manual half of design item O18 (meter integration into the
signal-generator sweep), run as one script on the control-station tray.

Hookup of record (Rick, 2026-10-09): B210 TX/RX A -> driver amplifier input (no pad);
amplifier output -> 40 dB power attenuator -> 8595E (testbench Prologix GPIB 18). The
amplifier output is the marker amplitude plus the pad loss. The sequencer (USB relay board
+ B210 GPIO) keys exactly as the application's Signal generator mode does, so the keying
path is exercised too.

What it does, in order:
  1. GPS clock preflight (refuses to transmit unlocked) and the B210 open on the external
     reference, the same RadioConfig as the application's worker.
  2. A GeneratorSession (eve/siggen.py) with a CW at the dial frequency, started with the
     key up; the 8595E's set-up is read back and recorded (or, with --set-analyzer, set to
     centre / span / reference level for this measurement and RESTORED afterwards).
  3. KEY, then TX gain steps from --start to --stop: each step is held --hold seconds, the
     analyzer's marker is read (peak search, then the marker counter for the frequency and
     the marker amplitude), and the row is recorded. The sweep stops early when the
     amplifier output reaches --target-dbm (5 W = +37 dBm) or when the output stops
     following the gain (1 dB compression: a step that gains less than step - 1 dB).
     Above --fine-above-dbm the step shrinks to --fine-step so the target is not overshot.
  4. The TX gain for the target power is interpolated between the two bracketing rows,
     set, measured, corrected once, and measured again; UNKEY, STOP.
  5. Outputs in --archive: <sid>_pout.json (every row, the analyzer set-up, the radio and
     GPS state), <sid>_pout.png (TX gain vs output power with the 1 dB compression line
     and the 5 W point), <sid>_report.pdf (the generator session's own one-page report).

Lab rules (C:/CNS-Systems/TEST_EQUIPMENT.md): nothing is *RST or IP; the analyzer's
measurement set-up is changed only with --set-analyzer (the operator's OK) and restored
in a finally; one GPIB connection for the visit, ++loc at the end. The amplifier is never
driven past --stop dB of TX gain or past the target power; the absolute ceiling is
--max-dbm (8 W = +39 dBm, the module's published maximum).

Usage (project env active, or PATH prefixed with .conda/Library/bin; the application must
be CLOSED - the B210 is single-user):
    python tools/eve_pout_sweep.py --gpsdo --keyer sequencer
    python tools/eve_pout_sweep.py --gpsdo --keyer sequencer --set-analyzer --span 2e6
    python tools/eve_pout_sweep.py --dry-run              # simulated radio, real relay board, no RF
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from eve import EveParams  # noqa: E402

PROLOGIX = ("192.168.10.67", 1234)
GPIB_ADDR = 18
MARKER_OFF = 999999999999.0


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Analyzer:
    """The 8595E over the testbench Prologix: read-back of the set-up, peak search, marker
    counter, marker amplitude; set-up changes only through set_measurement()."""

    SETUP = {"CF?": "center_hz", "SP?": "span_hz", "RB?": "rbw_hz", "VB?": "vbw_hz",
             "RL?": "ref_level_dbm", "AT?": "atten_db", "ST?": "sweep_s", "AUNITS?": "units"}

    def __init__(self, host_port=PROLOGIX, addr=GPIB_ADDR, tries: int = 4):
        # the Prologix accepts one TCP session; a just-closed one can be refused for a while
        for attempt in range(tries):
            try:
                self.s = socket.create_connection(host_port, timeout=8.0)
                break
            except OSError as e:
                if attempt == tries - 1:
                    raise RuntimeError(f"8595E: no connection to the Prologix at {host_port[0]}:{host_port[1]} ({e})") from e
                print(f"Prologix connect failed ({e}); retrying in 5 s", flush=True)
                time.sleep(5.0)
        for c in ("++mode 1", f"++addr {addr}", "++auto 0", "++eoi 1", "++eos 0", "++read_tmo_ms 1000"):
            self.cmd(c)
            time.sleep(0.03)
        ident = self.query("ID?")
        if not ident.startswith("HP8595"):
            raise RuntimeError(f"unexpected instrument at GPIB {addr}: {ident!r}")
        self.ident = ident
        self.found = None
        self.changed = False

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

    def query(self, text: str, timeout: float = 2.0, tries: int = 3) -> str:
        """Send a query and read the reply; an empty reply (the analyzer still busy, e.g.
        the marker counter gating) is read again up to `tries` times."""
        self.cmd(text)
        for attempt in range(tries):
            self.cmd("++read eoi")
            r = self.read(timeout)
            if r:
                return r
            time.sleep(1.0)
        return ""

    def setup(self) -> dict:
        out = {"id": self.ident}
        for q, k in self.SETUP.items():
            v = self.query(q)
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
        out["counter_on"] = self.query("MKFC?").strip() in ("1", "ON")
        return out

    @staticmethod
    def describe(su: dict) -> str:
        return (f"{su['id']}: centre {su['center_hz']/1e6:.6f} MHz, span {su['span_hz']/1e6:.3f} MHz, "
                f"RBW {su['rbw_hz']/1e3:.0f} kHz, VBW {su['vbw_hz']/1e3:.0f} kHz, ref {su['ref_level_dbm']:+.1f} dBm, "
                f"att {su['atten_db']:.0f} dB, sweep {su['sweep_s']*1e3:.0f} ms")

    def set_measurement(self, center_hz: float, span_hz: float, ref_dbm: float) -> None:
        """Centre / span / reference level for this measurement (operator's OK); the found
        set-up is kept for restore()."""
        if self.found is None:
            self.found = self.setup()
        self.cmd(f"CF {center_hz/1e6:.6f}MZ;SP {span_hz/1e6:.6f}MZ;RL {ref_dbm:.1f}DM;")
        self.changed = True
        time.sleep(0.5)

    def restore(self) -> None:
        if self.found is not None and self.changed:
            f = self.found
            self.cmd(f"CF {f['center_hz']/1e6:.6f}MZ;SP {f['span_hz']/1e6:.6f}MZ;RL {f['ref_level_dbm']:.1f}DM;")
            if not f.get("counter_on"):
                self.cmd("MKFC OFF;")
            self.changed = False
            time.sleep(0.3)

    def marker(self, peak: bool = True, counter: bool = True) -> tuple[float, float]:
        """(frequency Hz, amplitude dBm) of the marker after a peak search and, with the
        marker counter on, a counted frequency (10 Hz resolution)."""
        if peak:
            self.cmd("MKPK HI;")
            time.sleep(0.25)
        if counter:
            self.cmd("MKFC ON;MKFCR 100HZ;")
            time.sleep(2.0)                 # the counter gates for about a second at 100 Hz
        f = float(self.query("MKF?", timeout=6.0))
        a = float(self.query("MKA?", timeout=6.0))
        return f, a

    def close(self) -> None:
        try:
            self.restore()
        finally:
            try:
                self.cmd("++loc")
            finally:
                self.s.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--f-dial", type=float, default=1299.5e6, help="dial frequency Hz; the CW sits at the dial")
    ap.add_argument("--variant", default="A")
    ap.add_argument("--start", type=float, default=30.0, help="first TX gain dB")
    ap.add_argument("--stop", type=float, default=80.0, help="last TX gain dB (B210 maximum 89.75; +10 dBm into the amplifier at the top)")
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--fine-step", type=float, default=2.0)
    ap.add_argument("--fine-above-dbm", type=float, default=30.0, help="use --fine-step once the output passes this (dBm)")
    ap.add_argument("--hold", type=float, default=4.0, help="seconds at each step before the reading")
    ap.add_argument("--pad", type=float, default=40.0, help="output attenuator loss dB (nominal 40; calibrate at the dial frequency when the E4438C is free)")
    ap.add_argument("--target-dbm", type=float, default=37.0, help="the power to find: 5 W = +37 dBm")
    ap.add_argument("--max-dbm", type=float, default=39.0, help="never exceed: the module's published 8 W")
    ap.add_argument("--scale-db", type=float, default=-3.0)
    ap.add_argument("--clock", default="external")
    ap.add_argument("--serial", default="")
    ap.add_argument("--lo-offset", type=float, default=-300e3)
    ap.add_argument("--tx-port", default="A", choices=["A", "B"])
    ap.add_argument("--gpsdo", action="store_true", help="check the Leo Bodnar GPS clock first (refuse unlocked)")
    ap.add_argument("--keyer", default="sequencer", choices=["none", "sequencer", "gpio", "usb_relay"])
    ap.add_argument("--port", default="auto", help="USB relay board COM port ('auto' = search)")
    ap.add_argument("--lna-guard-ms", type=float, default=150.0)
    ap.add_argument("--lna-release-ms", type=float, default=150.0)
    ap.add_argument("--set-analyzer", action="store_true", help="set the 8595E centre/span/ref level for this run (restored afterwards)")
    ap.add_argument("--span", type=float, default=2e6)
    ap.add_argument("--ref-dbm", type=float, default=0.0)
    ap.add_argument("--no-peak", action="store_true", help="do not peak-search (read the marker where the operator left it)")
    ap.add_argument("--no-counter", action="store_true", help="do not use the marker frequency counter")
    ap.add_argument("--no-analyzer", action="store_true", help="transmit the steps without reading the 8595E (manual readings)")
    ap.add_argument("--ignore-compression", action="store_true", help="keep stepping past 1 dB compression (saturation check, operator's OK); the power ceilings still apply")
    ap.add_argument("--dry-run", action="store_true", help="simulated radio (no RF), real relay board: exercises the keying and the analyzer path")
    ap.add_argument("--archive", default="archive_bench")
    a = ap.parse_args(argv)

    p = EveParams.named(a.variant)
    archive = Path(a.archive)
    archive.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        log_lines.append(line)
        print(line, flush=True)

    gps_text = ""
    if a.gpsdo and not a.dry_run:
        from eve import gpsdo
        rep = gpsdo.preflight()
        gps_text = f"{rep['config']} | locked {rep['locked']}"
        log("GPS clock: " + gps_text)
        if not rep["locked"]:
            print("GPS clock not locked; refusing to transmit", file=sys.stderr)
            return 3

    # ---- radio ---------------------------------------------------------------------------
    if a.dry_run:
        from eve.station import SimRadio
        radio = SimRadio(p, cn0_db=20.0)
        radio_text = "SIMULATED radio (dry run)"
    else:
        from eve.radio import EveRadio, RadioConfig
        rc = RadioConfig(serial=a.serial, f_dial_hz=a.f_dial, tx_gain_db=a.start, rx_gain_db=0.0,
                         clock_source=a.clock, require_ref_lock=(a.clock != "internal"), lo_offset_hz=a.lo_offset,
                         tx_frontend=a.tx_port)
        radio = EveRadio(p, rc)
        radio.create()
        st = radio.configure(p, rc)
        radio_text = st.summary()
        log(radio_text)
        if not st.tx_lo_offset_ok:
            print("TX LO offset not honored; the CW frequency would be uncertain. Stopping.", file=sys.stderr)
            radio.close()
            return 4
        log(f"rate error {radio.rate_error_ppm():+.3f} ppm; PPS verify {radio.verify_pps()}")

    # ---- session -------------------------------------------------------------------------
    from eve import siggen as G
    from eve import keyer as K
    from eve.station import SessionOptions
    t_start = time.time() + 2.0
    sid = f"POUT-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(t_start))}"
    sched = G.build_generator_schedule(sid, t_start, a.f_dial, p, "K0PRT K0PRT", pilot=False)
    key = K.make_keyer(a.keyer, radio=radio, port=a.port, log=log,
                       lna_guard_s=a.lna_guard_ms / 1e3, lna_release_s=a.lna_release_ms / 1e3)
    key.open()
    log(f"key line: {key.name}")
    if getattr(key, "usb_missing", False):
        log("WARNING: USB relay board NOT FOUND; keying through the B210 GPIO lines only")
    opts = SessionOptions(out_dir=str(archive), live_decode=False, pa_in_chain=False, tx_precompensate=False,
                          rx_doppler_removal=False, start_margin_s=2.5 if a.dry_run else 2.0,
                          realtime_mode=not a.dry_run)
    sess = G.GeneratorSession(sched, radio, opts, signal="cw", offset_hz=0.0, scale_db=a.scale_db,
                              tx_gain_db=a.start, sweep=None, log=log, keyer=key)
    probs = sess.preflight()
    if probs:
        print("preflight failed: " + "; ".join(probs), file=sys.stderr)
        return 5

    an = None
    if not a.no_analyzer:
        an = Analyzer()
        su = an.setup()
        log("8595E as found: " + Analyzer.describe(su))

    result = {"session_id": sid, "started_utc": utc_now(), "f_dial_hz": a.f_dial, "pad_db": a.pad,
              "target_dbm": a.target_dbm, "radio": radio_text, "gps": gps_text, "keyer": key.name,
              "dry_run": a.dry_run, "analyzer": None, "rows": [], "verify": [], "result": {}}

    rep_holder = {}

    def runner():
        try:
            rep_holder["rep"] = sess.run()
        except Exception as e:      # noqa: BLE001
            rep_holder["err"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    t_wait = time.time()
    while sess.tb is None and "err" not in rep_holder and time.time() - t_wait < 30:
        time.sleep(0.1)
    if "err" in rep_holder:
        print(f"generator failed to start: {rep_holder['err']}", file=sys.stderr)
        return 6
    time.sleep(1.0)

    rc_code = 0
    try:
        if an is not None:
            result["analyzer_found"] = su
            if a.set_analyzer:
                an.set_measurement(a.f_dial, a.span, a.ref_dbm)
                su = an.setup()
                log("8595E set for the measurement: " + Analyzer.describe(su))
            result["analyzer"] = su

        def measure(gain: float, why: str) -> dict:
            sess.set_level(tx_gain_db=gain, why=why)
            time.sleep(a.hold)
            row = {"utc": utc_now(), "tx_gain_db": gain, "why": why}
            if an is not None:
                try:
                    f, amp = an.marker(peak=not a.no_peak, counter=not a.no_counter)
                except Exception as e:      # noqa: BLE001  (a bad GPIB reply must not end the run)
                    row.update({"marker": f"read failed: {e}"})
                    log(f"TX gain {gain:5.1f} dB: analyzer read failed ({e})")
                    return row
                if f >= MARKER_OFF or amp >= MARKER_OFF:
                    row.update({"marker": "off"})
                    log(f"TX gain {gain:5.1f} dB: marker OFF")
                else:
                    pout = amp + a.pad
                    row.update({"marker_hz": f, "marker_dbm": amp, "pout_dbm": pout, "pout_w": 10 ** (pout / 10) / 1e3,
                                "freq_error_hz": f - a.f_dial})
                    log(f"TX gain {gain:5.1f} dB: marker {amp:+7.2f} dBm at {f/1e6:.6f} MHz "
                        f"({f - a.f_dial:+.0f} Hz) -> amplifier {pout:+6.2f} dBm = {row['pout_w']:.3f} W")
            else:
                log(f"TX gain {gain:5.1f} dB held {a.hold:.0f} s (read the analyzer now)")
            return row

        # ---- key down and sweep ------------------------------------------------------------
        if not sess.key(True, why="power sweep"):
            print("key-down refused", file=sys.stderr)
            return 7
        log("KEYED; stepping")
        gain = a.start
        last = None
        stop_reason = "reached --stop"
        while gain <= a.stop + 1e-9:
            row = measure(gain, "sweep")
            result["rows"].append(row)
            pout = row.get("pout_dbm")
            if pout is not None:
                if pout >= a.max_dbm:
                    stop_reason = f"ceiling {a.max_dbm:.0f} dBm reached"
                    break
                if pout >= a.target_dbm:
                    stop_reason = f"target {a.target_dbm:.0f} dBm reached"
                    break
                if last is not None and last.get("pout_dbm") is not None:
                    dg = gain - last["tx_gain_db"]
                    dp = pout - last["pout_dbm"]
                    if dg > 0 and dp < dg - 1.0 and pout > a.fine_above_dbm - 10:
                        row["compression"] = True
                        if not a.ignore_compression:
                            stop_reason = f"compression: +{dg:.0f} dB of gain gave +{dp:.1f} dB of output"
                            break
                        log(f"compression noted: +{dg:.0f} dB of gain gave +{dp:.1f} dB of output; continuing (operator's OK)")
            last = row
            step = a.fine_step if (pout is not None and pout >= a.fine_above_dbm) else a.step
            gain = round(gain + step, 2)
        result["result"]["stop_reason"] = stop_reason
        log("sweep stopped: " + stop_reason)

        # ---- the target power: interpolate, verify, correct once --------------------------------
        rows = [r for r in result["rows"] if r.get("pout_dbm") is not None]
        g_target = None
        if len(rows) >= 2:
            above = [r for r in rows if r["pout_dbm"] >= a.target_dbm]
            below = [r for r in rows if r["pout_dbm"] < a.target_dbm]
            if above and below:
                lo, hi = below[-1], above[0]
                g_target = lo["tx_gain_db"] + (a.target_dbm - lo["pout_dbm"]) * (hi["tx_gain_db"] - lo["tx_gain_db"]) / (hi["pout_dbm"] - lo["pout_dbm"])
            elif below and not above and "compression" not in stop_reason:
                r1, r2 = rows[-2], rows[-1]
                slope = (r2["pout_dbm"] - r1["pout_dbm"]) / (r2["tx_gain_db"] - r1["tx_gain_db"])
                if slope > 0.5:
                    g_target = r2["tx_gain_db"] + (a.target_dbm - r2["pout_dbm"]) / slope
                    if g_target > a.stop:
                        g_target = None
        if g_target is not None and an is not None:
            for attempt in range(2):
                g_target = round(min(max(g_target, 0.0), 89.75), 2)
                row = measure(g_target, f"target verify {attempt + 1}")
                result["verify"].append(row)
                if row.get("pout_dbm") is None:
                    break
                err = row["pout_dbm"] - a.target_dbm
                if abs(err) <= 0.3 or row["pout_dbm"] >= a.max_dbm:
                    break
                g_target = g_target - err
            final = result["verify"][-1]
            if final.get("pout_dbm") is not None:
                result["result"].update({"target_gain_db": final["tx_gain_db"], "target_pout_dbm": final["pout_dbm"],
                                         "target_pout_w": final["pout_w"], "freq_error_hz": final.get("freq_error_hz")})
                log(f"TX gain for {a.target_dbm:.0f} dBm: {final['tx_gain_db']:.2f} dB -> {final['pout_dbm']:+.2f} dBm "
                    f"= {final['pout_w']:.2f} W; CW at {final['marker_hz']/1e6:.6f} MHz ({final['freq_error_hz']:+.0f} Hz from the dial)")
        elif g_target is None:
            log("target power not bracketed within the sweep (see the rows)")
    except KeyboardInterrupt:
        log("interrupted")
        rc_code = 130
    finally:
        try:
            sess.key(False, why="sweep done")
        except Exception:
            pass
        log("key up; stopping the generator")
        sess.stop("sweep done")
        th.join(timeout=20)
        try:
            key.close()
        except Exception:
            pass
        if an is not None:
            try:
                an.close()
            except Exception:
                pass
        if not a.dry_run:
            try:
                radio.close()
            except Exception:
                pass

    # ---- outputs ---------------------------------------------------------------------------
    result["finished_utc"] = utc_now()
    result["steps"] = sess.steps
    result["log"] = log_lines
    out_json = archive / f"{sid}_pout.json"
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"wrote {out_json}")
    try:
        png = plot(result, archive / f"{sid}_pout.png")
        log(f"wrote {png}")
    except Exception as e:      # noqa: BLE001
        log(f"plot failed: {e}")
    rep = rep_holder.get("rep")
    if rep is not None:
        try:
            pdf = G.write_generator_report(sess, rep, archive / f"{sid}_report.pdf",
                                           extra={"8595E": Analyzer.describe(result["analyzer"]) if result.get("analyzer") else "not read",
                                                  "pad": f"{a.pad:.1f} dB", "result": json.dumps(result["result"])})
            log(f"wrote {pdf}")
        except Exception as e:      # noqa: BLE001
            log(f"generator report failed: {e}")
    return rc_code


def plot(result: dict, out_png: Path) -> Path:
    """TX gain vs amplifier output (dBm, with a W axis on the right), the linear-gain line
    through the low-power rows, the 1 dB compression line and the target point."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    rows = [r for r in result["rows"] if r.get("pout_dbm") is not None]
    if not rows:
        raise ValueError("no rows with a reading")
    g = np.array([r["tx_gain_db"] for r in rows])
    pw = np.array([r["pout_dbm"] for r in rows])
    fig, ax = plt.subplots(figsize=(7.0, 4.6), dpi=150)
    ax.plot(g, pw, "o-", color="#1b6f6a", label="measured (8595E marker + pad)")
    # linear reference through the lowest rows (up to 4, all below target - 6 dB)
    lin = [r for r in rows if r["pout_dbm"] < result["target_dbm"] - 6.0][:4]
    if len(lin) >= 2:
        gl = np.array([r["tx_gain_db"] for r in lin]); pl = np.array([r["pout_dbm"] for r in lin])
        slope, icpt = np.polyfit(gl, pl, 1)
        gg = np.linspace(g.min(), g.max(), 50)
        ax.plot(gg, slope * gg + icpt, "--", color="#999999", label=f"linear fit, slope {slope:.2f} dB/dB")
        ax.plot(gg, slope * gg + icpt - 1.0, ":", color="#cc6600", label="1 dB compression line")
    ax.axhline(result["target_dbm"], color="#b00020", lw=0.8)
    ax.text(g.min(), result["target_dbm"] + 0.3, f"{result['target_dbm']:.0f} dBm = {10 ** (result['target_dbm'] / 10) / 1e3:.0f} W", color="#b00020", fontsize=8)
    res = result.get("result", {})
    if res.get("target_gain_db") is not None:
        ax.plot([res["target_gain_db"]], [res["target_pout_dbm"]], "s", color="#b00020", ms=7,
                label=f"TX gain {res['target_gain_db']:.1f} dB -> {res['target_pout_w']:.2f} W")
    ax.set_xlabel("B210 TX gain (dB)")
    ax.set_ylabel("amplifier output (dBm)")
    ax2 = ax.twinx()
    lo, hi = ax.get_ylim()
    ax2.set_ylim(lo, hi)
    ticks = [t for t in ax.get_yticks() if lo <= t <= hi]
    ax2.set_yticks(ticks)
    ax2.set_yticklabels([f"{10 ** (t / 10) / 1e3:.2g}" for t in ticks])
    ax2.set_ylabel("amplifier output (W)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title(f"Driver amplifier output vs TX gain, CW at {result['f_dial_hz']/1e6:.4f} MHz  ({result['session_id']})", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    return out_png


if __name__ == "__main__":
    sys.exit(main())
