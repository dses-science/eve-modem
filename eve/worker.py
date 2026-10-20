"""The run worker: everything a run does with the radio, in its own process.

Why a process: a second uhd.usrp_source for the same B210 in one process crashed the
program in the constructor (access violation, 2026-09-12, twice on the desktop) however
carefully the first one was released, and reconfiguring one source across waveforms
gave a sample rate 0.37 % off and a retune 10 kHz low. A fresh process per run gets a
fresh UHD every time, and a crash in the radio code cannot take the window down.

The worker talks to the application over a multiprocessing pipe with small tuples:
  ("log", text)                     a line for the session log
  ("state", name)                   preparing | running | decoding | idle
  ("preview", text)                 the schedule description (preview_only)
  ("session", payload)              schedule dict, time offset, radio text: bind the panel
  ("frame", k, metric_bytes)        one frame's metric (float32[M]) for the live view
  ("status", dict)                  phase, keyed, radio text, GPS clock text, ephemeris text
  ("finished", result)              {"ok", "pdf", "session_id", "summary", "error", "aborted"}
and receives ("abort", reason).

`run_job(cfg, preview_only, conn)` is the process target; `Runner` is the same logic
usable in-process (the tools and tests).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional

import numpy as np

from .params import EveParams
from . import doppler as D
from . import schedule as S
from .doppler import iso_utc

PREFIX = {"sim": "SIM", "bench": "BENCH", "interop": "INTEROP", "eme": "EME", "eve": "EVE"}
TARGET = {"sim": "sim", "bench": "bench", "interop": "partner", "eme": "moon", "eve": "venus"}
PREVIEW_MIN_EL_DEG = 10.0     # a preview with the target below the horizon shifts to when it reaches this


class Runner:
    """One job: build the radio, ephemeris, schedule, keyer, and session from a settings
    dict, run it, decode the archive, and write the report, reporting through `send` (a
    callable taking the pipe tuples listed in the module docstring). `poll_abort` returns the
    abort reason when the GUI asked for one, else None. Used in-process by the tools and
    tests, and by run_job in the worker process."""
    def __init__(self, send, poll_abort=None, poll_commands=None):
        self.send = send                    # callable(tuple)
        self.poll_abort = poll_abort        # callable() -> Optional[str]
        self.poll_commands = poll_commands  # callable() -> list of ("level", gain_db, scale_db) tuples
        self.session = None

    def say(self, s: str) -> None:
        """Send a ("log", s) message: one session log line."""
        self.send(("log", s))

    def phase(self, text: str, kind: str = "busy") -> None:
        """What the operator's badge says. kind: run | busy | ok | fail."""
        self.send(("status", {"phase": text, "kind": kind}))

    # ---- the job -------------------------------------------------------------------------
    def run(self, cfg: Dict, preview_only: bool = False) -> Dict:
        """Run one job from `cfg` (the Setup form as EveApp._values() builds it) and return
        {ok, pdf, session_id, summary, aborted}, or {ok: False, error, pdf: None} on any
        exception. Steps: parameters and archive folder; the radio (SimRadio for sim,
        otherwise EveRadio, opened before the GPS clock preflight); the start time and the
        ephemeris model (synthetic tables for sim, bench, and interop; JPL Horizons or
        astropy for the Moon and Venus, saved as <sid>_<target>_haswell.csv); the schedule
        (design 4.1, 4.2, 6.4), refused when a chunk cannot hold pilot + 2 frames. With
        preview_only the description is sent as ("preview", text) and the job ends. Otherwise
        the schedule JSON is written, the keyer opened (a missing USB board sends a notice
        and the GPIO lines carry on), the session preflighted and run beside a status thread,
        and decode_and_report finishes the job."""
        radio = None
        gps_text = ""
        result: Dict = {"ok": False, "pdf": None}
        try:
            self.send(("state", "preparing"))
            mode = cfg["mode"]
            p = EveParams.named(cfg["variant"])
            if not cfg["full_symbol"]:
                p = replace(p, n_frames=int(cfg["n_frames_test"]))
            # the pilot length is its own setting, per chunk, in every mode (1.0.10); a config
            # from before carries pilot_frames_test, which applied at test length only
            n_pilot = cfg.get("pilot_frames", None if cfg["full_symbol"] else cfg.get("pilot_frames_test"))
            if n_pilot is not None:
                p = replace(p, pilot_frames=int(n_pilot))
            f_dial = float(cfg["f_dial_mhz"]) * 1e6
            archive = Path(cfg["archive"])
            archive.mkdir(parents=True, exist_ok=True)
            site = D.DSES_HASWELL

            want_gps = mode != "sim" and cfg["gpsdo"] and cfg["clock"] == "external" and not preview_only

            # radio
            if mode == "sim" or (mode == "siggen" and cfg.get("siggen_sim")):
                # the software simulation, or the generator on the simulated radio (no B210:
                # the sequencer, the level commands, the sweep and the report are exercised)
                from .station import SimRadio
                radio = SimRadio(p, cn0_db=float(cfg["sim_cn0"]), seed=int(cfg["sim_seed"]))
                now = time.time()
            elif preview_only:
                now = time.time()
            else:
                from .radio import EveRadio, RadioConfig
                rc = RadioConfig(serial=cfg["serial"], f_dial_hz=f_dial, tx_gain_db=float(cfg["tx_gain"]),
                                 rx_gain_db=float(cfg["rx_gain"]), clock_source=cfg["clock"],
                                 time_source="host" if cfg["time_host"] else None,
                                 require_ref_lock=(cfg["clock"] != "internal"), lo_offset_hz=float(cfg["lo_offset_khz"]) * 1e3,
                                 tx_frontend=str(cfg.get("tx_port", "A"))[:1], rx_antenna=str(cfg.get("rx_port", "A : RX2")))
                radio = EveRadio(p, rc)
                self.send(("state", "opening the radio"))
                time.sleep(2.0)                 # let the USB stack settle after the previous worker's close
                radio.create()                  # USB device open first (see EveRadio.create)
                if want_gps:
                    from . import gpsdo
                    rep = gpsdo.preflight()
                    gps_text = f"{rep['config']} | locked {rep['locked']}"
                    self.say("GPS clock: " + gps_text)
                    if not rep["locked"]:
                        raise RuntimeError("GPS clock not locked; refusing to start")
                    time.sleep(0.5)
                st = radio.configure(p, rc)
                self.say(st.summary())
                self.say(f"rate error {radio.rate_error_ppm():+.3f} ppm; PPS verify {radio.verify_pps()}")
                now = radio.device_time()

            if mode == "siggen":
                return self._run_generator(cfg, p, radio, f_dial, archive, now, gps_text, preview_only)

            # start time and ephemeris
            lead = float(cfg["lead_s"])
            if mode in ("eme", "eve", "interop") and not cfg["sky_start_now"]:
                t_start = D.to_unix(cfg["sky_start_utc"])
                if t_start < now + 12.0:
                    raise RuntimeError(f"start {iso_utc(t_start, 0)} is in the past or too close; use 'start now' or a later time")
            else:
                t_start = now + lead
            sid = f"{PREFIX[mode]}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(t_start))}"
            table_name = ""
            if mode == "sim":
                model = D.DopplerModel(D.synthetic_table("sim", site, now - 60, now + 8 * 3600, range_km=float(cfg["sim_range_km"])))
            elif mode == "bench":
                model = D.DopplerModel(D.synthetic_table("bench", site, now - 60, now + 8 * 3600, range_km=float(cfg["bench_range_km"])))
            elif mode == "interop":
                model = D.DopplerModel(D.synthetic_table("partner", site, now - 60, now + 8 * 3600, range_km=0.001))
            else:
                target = TARGET[mode]
                table_path = archive / f"{sid}_{target}_haswell.csv"
                self.say(f"ephemeris for {target} from {cfg['sky_source']} ...")
                model = D.make_model(target, site, t_start - 1800, t_start + 8 * 3600, source=cfg["sky_source"],
                                     table_path=None if preview_only else table_path, step="1 m")
                table_name = table_path.name if not preview_only else ""
                el = model.elevation_deg(t_start)
                self.say(f"{target} at start: elevation {el:.1f} deg, RTT {model.rtt_s(t_start):.2f} s, "
                         f"Doppler {model.doppler_hz(t_start, f_dial):+.1f} Hz")
                if el < 0 and preview_only:
                    # Rick, 2026-10-20: keep the warning, but show the NEXT viable schedule
                    t_next = self._next_viable_start(target, site, t_start, cfg["sky_source"])
                    if t_next is None:
                        raise RuntimeError(f"{target} is below the horizon at the start time (elevation {el:.1f} deg) "
                                           f"and does not reach {PREVIEW_MIN_EL_DEG:.0f} deg within two days")
                    msg = (f"{target} is below the horizon at the start time (elevation {el:.1f} deg). The preview "
                           f"shows the next viable schedule, from {iso_utc(t_next, 0)} when {target} reaches "
                           f"{PREVIEW_MIN_EL_DEG:.0f} deg; set that start time (untick 'start now') before running.")
                    self.send(("notice", msg))
                    self.say("NOTE: " + msg)
                    t_start = t_next
                    sid = f"{PREFIX[mode]}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(t_start))}"
                    model = D.make_model(target, site, t_start - 1800, t_start + 8 * 3600, source=cfg["sky_source"],
                                         table_path=None, step="1 m")
                    el = model.elevation_deg(t_start)
                    self.say(f"{target} at the shifted start: elevation {el:.1f} deg, RTT {model.rtt_s(t_start):.2f} s, "
                             f"Doppler {model.doppler_hz(t_start, f_dial):+.1f} Hz")
                if el < 0:
                    raise RuntimeError(f"{target} is below the horizon at the start time (elevation {el:.1f} deg)")

            # schedule
            common = dict(params=p, text=cfg["message"], repeat_count=int(cfg["repeat"]), pilot=cfg["pilot"])
            if mode == "sim":
                if model.rtt_s(t_start) > 0.6:
                    sched = S.build_schedule(sid, "sim", model, t_start, f_dial, chunk_s=None, rtt_guard_s=0.1,
                                             t_off_min_s=float(cfg["bench_t_off_min"]), t_on_max_s=3600.0, **common)
                else:
                    sched = S.build_schedule(sid, "sim", model, t_start, f_dial, chunk_s=float(cfg["bench_chunk_s"]),
                                             rtt_guard_s=0.0, t_off_min_s=float(cfg["bench_t_off_min"]), t_on_max_s=3600.0,
                                             mode="bistatic_tx", **common)
                opts_kw = dict(pa_in_chain=False, tx_precompensate=False, rx_doppler_removal=False,
                               start_margin_s=2.5, realtime_mode=False)     # room for the sequencer's lead before chunk 1
            elif mode == "bench":
                sched = S.build_schedule(sid, "bench", model, t_start, f_dial, chunk_s=float(cfg["bench_chunk_s"]),
                                         rtt_guard_s=0.0, t_off_min_s=float(cfg["bench_t_off_min"]), t_on_max_s=3600.0,
                                         mode="bistatic_tx", **common)
                opts_kw = dict(pa_in_chain=False, tx_precompensate=False, rx_doppler_removal=False, start_margin_s=2.0)
            elif mode == "interop":
                rx_only = cfg["interop_dir"].startswith("Receive")
                sched = S.build_schedule(sid, "partner", model, t_start, f_dial, chunk_s=float(cfg["interop_chunk_s"]),
                                         rtt_guard_s=0.0, t_off_min_s=0.0, t_on_max_s=3600.0,
                                         mode="bistatic_rx" if rx_only else "bistatic_tx",
                                         notes=("receive only: partner transmits" if rx_only else "transmit only: partner receives"),
                                         **common)
                opts_kw = dict(pa_in_chain=False, tx_precompensate=False, rx_doppler_removal=False, tx_enabled=not rx_only)
            elif mode == "eme":
                sched = S.build_schedule(sid, "moon", model, t_start, f_dial, mode=cfg["sky_mode"],
                                         chunk_s=float(cfg["eme_chunk_s"]), rtt_guard_s=float(cfg["eme_rtt_guard"]),
                                         t_on_max_s=float(cfg["eme_t_on_max"]), t_off_min_s=float(cfg["eme_t_off_min"]),
                                         doppler_table=table_name, **common)
                opts_kw = dict(pa_in_chain=bool(cfg["eme_pa"]), tx_precompensate=bool(cfg["sky_precomp"]),
                               rx_doppler_removal=bool(cfg["sky_rx_doppler"]))
            else:
                sched = S.build_schedule(sid, "venus", model, t_start, f_dial, mode=cfg["sky_mode"],
                                         chunk_s=float(cfg["sky_chunk_s"]), rtt_guard_s=float(cfg["sky_rtt_guard"]),
                                         t_on_max_s=float(cfg["sky_t_on_max"]), t_off_min_s=float(cfg["sky_t_off_min"]),
                                         doppler_table=table_name, **common)
                opts_kw = dict(pa_in_chain=bool(cfg["sky_pa"]), tx_precompensate=bool(cfg["sky_precomp"]),
                               rx_doppler_removal=bool(cfg["sky_rx_doppler"]))
            if sched.pilot_enabled and sched.chunks:
                n_min = min(c.n_frames for c in sched.chunks[:-1] or sched.chunks)
                need = p.pilot_frames + 2
                if n_min < need:
                    raise RuntimeError(f"a chunk of {n_min} frames cannot hold {p.pilot_frames} pilot frames plus data: "
                                       f"use a chunk of at least {need / p.r_bw:.1f} s ({need} frames of {p.t_frame:.3f} s) "
                                       f"or fewer pilot frames")
            desc = S.describe(sched, model)
            dur = sched.t_end - sched.t_start
            desc += (f"\nduration {dur / 60:.1f} min ({int(dur // 3600)}h {int(dur % 3600 // 60):02d}m); symbol {p.n_frames} frames = "
                     f"{p.t_sym:.1f} s; message {p.t_sym * p.n_sym / 60:.1f} min per pass")
            if preview_only:
                self.send(("preview", desc))
                return {"ok": True, "pdf": None, "preview": True}
            self.say(desc)
            sched.to_json(archive / f"{sid}.json")

            from .station import Session, SessionOptions
            opts = SessionOptions(out_dir=str(archive), live_decode=True, **opts_kw)
            from . import keyer as _keyer
            kind = {"B210 GPIO": "gpio", "USB relay board": "usb_relay", "gpio": "gpio", "usb_relay": "usb_relay",
                    "sequencer": "sequencer"}.get(cfg.get("keyer_kind", "none"), "none")
            if kind != "none":
                self.send(("state", "finding the USB relay board / setting the GPIO lines"))
            key = _keyer.make_keyer(kind, radio=radio, port=cfg.get("keyer_port", ""), log=self.say,
                                    lna_guard_s=float(cfg.get("keyer_lna_guard_ms", 50)) / 1e3,
                                    lna_release_s=float(cfg.get("keyer_lna_release_ms", 50)) / 1e3)
            key.open()                      # fails here, before any RF, if the port is wrong
            self.say(f"key line: {key.name}")
            if getattr(key, "usb_missing", False):
                msg = ("USB relay board NOT FOUND on any COM port: the run continues on the B210 GPIO lines only "
                       "(GPIO_0 = TX key, GPIO_1 = LNA). If the station is keyed through the relay board, plug it in "
                       "and start again.")
                self.say("WARNING: " + msg)
                self.send(("notice", msg))
            sess = Session(sched, model, radio, opts, log=self.say, keyer=key)
            probs = sess.preflight()
            if probs:
                raise RuntimeError("preflight failed: " + "; ".join(probs))
            self.session = sess

            def on_frame(k, x, metric):
                try:
                    self.send(("frame", int(k), np.asarray(metric, dtype=np.float32).tobytes()))
                except Exception:
                    pass
            sess.listeners.append(on_frame)
            self.send(("session", {"schedule": sched.to_dict(), "t_offset": radio.device_time() - time.time(),
                                   "radio": getattr(radio, "status_text", lambda: "")(), "gps": gps_text,
                                   "sim": bool(getattr(radio, "sim", False))}))
            stop = threading.Event()
            th = threading.Thread(target=self._status_loop, args=(sess, radio, model, sched, stop, gps_text), daemon=True)
            th.start()
            self.send(("state", "running"))
            try:
                rep = sess.run()
            finally:
                stop.set()
                self.phase("last chunk done: draining and closing the archive")
                try:
                    sess.keyer.close()
                except Exception:
                    pass
                sess.release()
                if radio is not None:
                    self.phase("closing the radio")
                    try:
                        radio.close()
                    except Exception:
                        pass
                    radio = None
            self.say(f"session finished: aborted={rep.aborted} {rep.abort_reason}; chunks keyed {rep.chunks_keyed}; "
                     f"frames sent {rep.frames_sent}; live decode {rep.live_decode}")
            search = int(cfg["interop_search_frames"]) if mode == "interop" and cfg["interop_dir"].startswith("Receive") else 0
            result = self.decode_and_report(archive, sched, extra={"GPS clock": gps_text} if gps_text else None, search=search)
            result["aborted"] = rep.aborted
            if rep.aborted:
                self.phase(f"ABORTED: {rep.abort_reason}", "fail")
            elif result.get("ok"):
                self.phase(f"DECODED '{result['summary']['combined']['text']}'  -  report on the Report tab", "ok")
            else:
                self.phase("NOT DECODED  -  report on the Report tab", "fail")
            return result
        except Exception as e:      # noqa: BLE001
            self.say("ERROR: " + "".join(traceback.format_exception_only(type(e), e)).strip())
            tb = traceback.format_exc().splitlines()
            if len(tb) >= 3:
                self.say(tb[-3].strip())
            self.phase(f"FAILED: {e}", "fail")
            return {"ok": False, "error": str(e), "pdf": None}
        finally:
            if radio is not None:
                try:
                    radio.close()
                except Exception:
                    pass
            self.session = None
            self.send(("state", "idle"))

    def _next_viable_start(self, target: str, site, t_from: float, source: str) -> Optional[float]:
        """The first time after t_from when the target is at or above PREVIEW_MIN_EL_DEG, found on a
        coarse two-day ephemeris (10 min steps, then refined to the minute); None if it never is."""
        coarse = D.make_model(target, site, t_from, t_from + 2 * 86400.0, source=source, table_path=None, step="10 m")
        t = float(t_from)
        while t < t_from + 2 * 86400.0:
            if coarse.elevation_deg(t) >= PREVIEW_MIN_EL_DEG:
                while t - 60.0 > t_from and coarse.elevation_deg(t - 60.0) >= PREVIEW_MIN_EL_DEG:
                    t -= 60.0
                return 60.0 * math.ceil(t / 60.0)       # a whole minute
            t += 600.0
        return None

    def _run_generator(self, cfg: Dict, p, radio, f_dial: float, archive: Path, now: float, gps_text: str,
                       preview_only: bool) -> Dict:
        """The signal-generator job (eve/siggen.py, design 5.8 / D25): a schedule of on-windows
        for the requested duration, the sequencer, the chosen signal through the transmit
        sink, live level commands from the GUI, an optional gain sweep, a one-page report."""
        from . import siggen as G
        from .station import SessionOptions
        from . import keyer as _keyer
        pa = bool(cfg.get("siggen_pa", False))
        t_start = now + 2.0
        sid = f"SIGGEN-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(t_start))}"
        sched = G.build_generator_schedule(sid, t_start, f_dial, p, cfg["message"], pilot=cfg["pilot"])
        signal = {"CW": "cw", "Two-tone": "two_tone", "EVE waveform": "eve"}.get(str(cfg.get("siggen_signal", "CW")), "cw")
        sweep = None
        if cfg.get("siggen_sweep"):
            sweep = G.SweepSpec(float(cfg["siggen_sweep_start_db"]), float(cfg["siggen_sweep_stop_db"]),
                                float(cfg["siggen_sweep_step_db"]), float(cfg["siggen_sweep_hold_s"]))
        desc = (f"signal generator {sid}: dial {f_dial / 1e6:.4f} MHz, {G.SIGNAL_NAMES[signal]}, "
                f"offset {float(cfg.get('siggen_offset_khz', 0.0)):+.3f} kHz"
                + (f", spacing {float(cfg.get('siggen_spacing_khz', 10.0)):.3f} kHz" if signal == "two_tone" else "")
                + f"\nTX gain {float(cfg['tx_gain']):.2f} dB, scale {float(cfg.get('siggen_level_db', -3.0)):+.1f} dB"
                + (f"; amplifier limits {G.PA_T_ON_MAX_S:.0f} s on / {G.PA_T_OFF_MIN_S:.0f} s off" if pa else "; no amplifier limits")
                + (f"\nsweep {sweep} on every key-down" if sweep else "")
                + ("\nDRY RUN on the simulated radio" if cfg.get("siggen_sim") else "")
                + "\nRuns until STOP; KEY / UNKEY on the Run tab; TX gain, scale and signal change live from the Setup tab")
        if preview_only:
            self.send(("preview", desc))
            return {"ok": True, "pdf": None, "preview": True}
        self.say(desc)
        sched.to_json(archive / f"{sid}.json")
        kind = {"sequencer": "sequencer", "gpio": "gpio", "usb_relay": "usb_relay"}.get(cfg.get("keyer_kind", "none"), "none")
        if kind != "none":
            self.send(("state", "finding the USB relay board / setting the GPIO lines"))
        key = _keyer.make_keyer(kind, radio=radio, port=cfg.get("keyer_port", ""), log=self.say,
                                lna_guard_s=float(cfg.get("keyer_lna_guard_ms", 50)) / 1e3,
                                lna_release_s=float(cfg.get("keyer_lna_release_ms", 50)) / 1e3)
        key.open()
        self.say(f"key line: {key.name}")
        if getattr(key, "usb_missing", False):
            msg = ("USB relay board NOT FOUND on any COM port: the generator runs with the B210 GPIO lines only "
                   "(GPIO_0 = TX key, GPIO_1 = LNA).")
            self.say("WARNING: " + msg)
            self.send(("notice", msg))
        opts = SessionOptions(out_dir=str(archive), live_decode=False, pa_in_chain=pa, tx_precompensate=False,
                              rx_doppler_removal=False, start_margin_s=2.5 if getattr(radio, "sim", False) else 2.0,
                              realtime_mode=not getattr(radio, "sim", False))
        sess = G.GeneratorSession(sched, radio, opts, signal=signal, offset_hz=float(cfg.get("siggen_offset_khz", 0.0)) * 1e3,
                                  spacing_hz=float(cfg.get("siggen_spacing_khz", 10.0)) * 1e3,
                                  scale_db=float(cfg.get("siggen_level_db", -3.0)), tx_gain_db=float(cfg["tx_gain"]),
                                  sweep=sweep, log=self.say, keyer=key,
                                  on_step=lambda d: self.send(("status", {"phase": sess.phase, "generator": sess.status_line()})))
        probs = sess.preflight()
        if probs:
            raise RuntimeError("preflight failed: " + "; ".join(probs))
        self.session = sess
        self.send(("session", {"schedule": sched.to_dict(), "t_offset": radio.device_time() - time.time(),
                               "radio": getattr(radio, "status_text", lambda: "")(), "gps": gps_text,
                               "sim": bool(getattr(radio, "sim", False)), "generator": sess.status_line()}))
        stop = threading.Event()
        th = threading.Thread(target=self._status_loop, args=(sess, radio, None, sched, stop, gps_text), daemon=True)
        th.start()
        self.send(("state", "running"))
        try:
            rep = sess.run()
        finally:
            stop.set()
            self.phase("generator stopped: releasing the key line")
            try:
                sess.keyer.key(False)
            except Exception:
                pass
            try:
                sess.keyer.close()
            except Exception:
                pass
            sess.release()
            if radio is not None:
                self.send(("state", "closing the radio"))
                try:
                    radio.close()
                except Exception:
                    pass
            self.send(("session_done",))
        pdf = archive / f"{sid}_report.pdf"
        self.phase("writing the report")
        try:
            G.write_generator_report(sess, rep, pdf, extra={"GPS clock": gps_text} if gps_text else None)
            self.say(f"report written: {pdf}")
        except Exception as e:      # noqa: BLE001
            self.say(f"report failed: {e}")
            pdf = None
        self.phase("ABORTED" if rep.aborted else "GENERATOR DONE", "fail" if rep.aborted else "ok")
        return {"ok": not rep.aborted, "pdf": str(pdf) if pdf else None, "session_id": sid, "aborted": rep.aborted,
                "summary": {"steps": sess.steps, "chunks_keyed": rep.chunks_keyed}}

    def _status_loop(self, sess, radio, model, sched, stop: threading.Event, gps_text: str) -> None:
        """Half-second status for the panel: phase, key, radio text, ephemeris; abort polls."""
        from . import gpsdo as _gpsdo
        from .report import tx_events_text
        g = None
        g_absent = False
        tx_warned = False
        n = 0
        while not stop.is_set():
            if self.poll_commands is not None and hasattr(sess, "set_level"):
                for cmd in self.poll_commands():
                    try:
                        if cmd[0] == "level":
                            sess.set_level(tx_gain_db=cmd[1], scale_db=cmd[2])
                        elif cmd[0] == "key":
                            threading.Thread(target=sess.key, args=(bool(cmd[1]),), kwargs={"why": "operator"}, daemon=True).start()
                        elif cmd[0] == "signal":
                            sess.set_signal(cmd[1], offset_hz=cmd[2], spacing_hz=cmd[3])
                        elif cmd[0] == "sweep":
                            sess.start_sweep()
                        elif cmd[0] == "stop":
                            sess.stop()
                    except Exception as e:      # noqa: BLE001
                        self.say(f"{cmd[0]} command failed: {e}")
            d = {"phase": sess.phase, "keyed": bool(sess.keyer.keyed), "key_text": sess.keyer.status_text()}
            if hasattr(sess, "status_line"):
                d["generator"] = sess.status_line()
                d["sched_text"] = sess.sched_text()
            try:
                if hasattr(radio, "status") and n % 4 == 0:
                    st = radio.status()
                    d["radio"] = (f"B210 {st.serial}  ref {st.clock_source}/{st.time_source}  "
                                  f"{'LOCKED' if st.ref_locked else 'not locked'}\n"
                                  f"rx {st.rx_rate:.2f} S/s  {st.rx_freq / 1e6:.6f} MHz  gain {st.rx_gain:.0f} dB  {st.rx_antenna}\n"
                                  f"tx {st.tx_rate:.2f} S/s  {st.tx_freq / 1e6:.6f} MHz  gain {st.tx_gain:.0f} dB  {st.tx_antenna}\n"
                                  f"LO offset rx {'ok' if st.rx_lo_offset_ok else 'FELL BACK'} / tx {'ok' if st.tx_lo_offset_ok else 'FELL BACK'}"
                                  f"   PPS set error {st.pps_error_s if st.pps_error_s is None else f'{st.pps_error_s:+.6f} s'}")
                elif not hasattr(radio, "status"):
                    d["radio"] = getattr(radio, "status_text", lambda: "radio")()
            except Exception as e:      # noqa: BLE001
                d["radio"] = f"radio status unavailable: {e}"
            # transmit underflows / late packets: the radio line carries the warning, the
            # operator gets one notice box, the log one line (2026-10-20, Raspberry Pi 5)
            ev = getattr(radio, "tx_event_counts", lambda: {})()
            if ev:
                d["tx_events"] = ev
                warn = tx_events_text(ev)
                if warn and "radio" in d:
                    d["radio"] = d["radio"] + "\n" + warn
                if warn and not tx_warned:
                    tx_warned = True
                    self.send(("notice", warn))
                    self.say("WARNING: " + warn)
            if n % 4 == 0 and gps_text and not g_absent:
                try:
                    if g is None:
                        g = _gpsdo.LeoBodnarGPSDO()
                    st = g.status(300)
                    d["gps"] = (f"GPS clock {g.serial}: sat {'LOCK' if st.sat_lock else 'no lock'}, "
                                f"PLL {'LOCK' if st.pll_lock else 'no lock'}, signal losses {st.loss_count}")
                except Exception as e:      # noqa: BLE001
                    d["gps"] = f"GPS clock: {e}"
                    g_absent = True
            try:
                now = radio.device_time()
                if model is not None:
                    f = sched.f_dial_hz
                    d["eph"] = (f"{sched.target}: az {model.azimuth_deg(now):6.1f}  el {model.elevation_deg(now):5.1f}  "
                                f"RTT {model.rtt_s(now):7.2f} s  Doppler {model.doppler_hz(now, f):+8.1f} Hz  "
                                f"rate {model.rate_hz_s(now, f):+.3f} Hz/s")
            except Exception as e:      # noqa: BLE001
                d["eph"] = f"ephemeris: {e}"
            self.send(("status", d))
            if self.poll_abort is not None:
                why = self.poll_abort()
                if why:
                    sess.abort(why)
            n += 1
            stop.wait(0.5)
        if g is not None:
            try:
                g.close()
            except Exception:
                pass

    def decode_and_report(self, archive: Path, sched, extra=None, search: int = 0) -> Dict:
        """Offline decode of the archived receive windows (eve.decode, the decision of
        record, design 5.3) and the session report PDF (eve.report, 8.4) for schedule `sched`
        in `archive`. `extra` lines go into the report header; `search` is the epoch search
        range in frames (30 by default for a bistatic_rx schedule). Returns {ok, pdf,
        session_id, summary}; a decode or report failure is logged and leaves summary None or
        pdf None."""
        from .decode import decode_archive, summarize
        from .report import write_report
        self.send(("state", "decoding"))
        self.say("offline decode (decision of record) ...")
        summary, windows = None, []
        n_files = len(list(Path(archive).glob(f"{sched.session_id}_*.eve.iq")))
        done = {"n": 0}

        def decode_log(s: str) -> None:
            self.say(s)
            if s.startswith(sched.session_id + "_"):
                done["n"] += 1
                self.phase(f"offline decode: window {done['n']} of {n_files}")
        self.phase(f"offline decode: {n_files} windows to file")
        try:
            if search == 0 and sched.mode == "bistatic_rx":
                search = 30
            acc, windows = decode_archive(archive, sched, log=decode_log, epoch_search_frames=search)
            summary = summarize(acc, sched)
            c = summary["combined"]
            self.say(f"OFFLINE DECODE: {'OK' if c['ok'] else 'FAIL'} '{c['text']}' symbols {c['symbols']} expected {c['expected']}; "
                     f"margins dB {[round(m, 1) for m in c['margins_db']]}")
        except Exception as e:      # noqa: BLE001
            self.say(f"offline decode failed: {e}")
        rep_path = archive / f"{sched.session_id}_session.json"
        rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
        pdf = archive / f"{sched.session_id}_report.pdf"
        self.phase("writing the report")
        try:
            write_report(sched, rep, summary, windows, pdf, extra=extra)
            self.say(f"report written: {pdf}")
        except Exception as e:      # noqa: BLE001
            self.say(f"report failed: {e}")
            pdf = None
        ok = bool(summary and summary["combined"]["ok"])
        return {"ok": ok, "pdf": str(pdf) if pdf else None, "session_id": sched.session_id, "summary": summary}

    def redecode(self, session_json: Path) -> Dict:
        """Re-run decode_and_report on an archived session from its schedule JSON (the Report
        tab's Re-decode); returns the result dict, {ok: False, error} on failure."""
        try:
            sched = S.Schedule.from_json(session_json)
            return self.decode_and_report(session_json.parent, sched)
        except Exception as e:      # noqa: BLE001
            self.say(f"ERROR: {e}")
            return {"ok": False, "error": str(e), "pdf": None}
        finally:
            self.send(("state", "idle"))


# ---- process target ------------------------------------------------------------------------
def locked_sender(conn, on_error=None):
    """A send(msg) for `conn` that one lock serializes across threads. The worker sends from
    three threads (the sink's thread: one "frame" message of a little over 16 KB per received
    frame; the status thread every 0.5 s; the main thread: log lines). CPython writes a
    message over 16 KB to a Unix socket as TWO writes (header, then body), so another
    thread's send can land between them and corrupt the stream: the GUI's reader died with
    "invalid load key" and the run aborted "GUI pipe lost" (the Haswell Pi 5, 2026-10-20
    21:50 UTC, a full-length bench run, frame 247). Windows writes the message in one call,
    which is why the bench here never showed it. on_error (optional) runs when a send fails
    (the GUI is gone)."""
    lock = threading.Lock()

    def send(msg):
        try:
            with lock:
                conn.send(msg)
        except Exception:       # noqa: BLE001
            if on_error is not None:
                on_error()
    return send


def run_job(cfg: Dict, preview_only: bool, conn, redecode: Optional[str] = None) -> None:
    """The multiprocessing (spawn) target: enable faulthandler into fault.log under
    log_dir(), run Runner.run(cfg, preview_only) or, with `redecode` set to a schedule JSON
    path, Runner.redecode, sending every message down `conn` and polling it for ("abort",
    reason); ends with ("finished", result) and closes the pipe."""
    import faulthandler
    try:
        from . import log_dir
        faulthandler.enable(file=open(os.path.join(str(log_dir()), "fault.log"), "a"), all_threads=True)
    except Exception:
        pass
    pending = {"abort": None, "commands": []}

    send = locked_sender(conn, on_error=lambda: pending.__setitem__("abort", pending["abort"] or "GUI pipe lost"))

    def poll_abort():
        try:
            while conn.poll():
                m = conn.recv()
                if m and m[0] == "abort":
                    pending["abort"] = m[1] if len(m) > 1 else "operator abort"
                elif m:
                    pending["commands"].append(tuple(m))
        except Exception:
            pass
        why, pending["abort"] = pending["abort"], None
        return why

    def poll_commands():
        cmds, pending["commands"] = pending["commands"], []
        return cmds

    r = Runner(send, poll_abort, poll_commands)
    if redecode:
        result = r.redecode(Path(redecode))
    else:
        result = r.run(cfg, preview_only)
    send(("finished", result))
    try:
        conn.close()
    except Exception:
        pass
