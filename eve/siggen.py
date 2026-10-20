"""Signal generator: the B210 as a bench source for the RF package (design document 5.8,
decision D25, 2026-09-23; made interactive the same day at Rick's request: key, unkey and
rekey, change the signal, the gain and the scale, all without stopping the run).

The station's RF package (the driver, the kilowatt amplifiers, the LNA, the coax and power
switching) is integrated and tuned on the bench before it goes into the feed. For that the
modem has to do three things it already does on the air, without any of the air's risk:
run the sequencer exactly as in a session (LNA off, guard, TX on; TX off, release, LNA on;
the amplifier duty limits when an amplifier is in the chain), send a continuous signal
whose kind the operator chooses, and let the operator set the drive level while it runs
so power-out and compression can be read off the bench instruments step by step.

The generator is an instrument, not a schedule: once started it stays up until the
operator stops it. The transmitter is keyed and released from the Run tab (KEY / UNKEY),
each key-down going through the sequencer with the key lead and lag of a session, and the
signal, the B210 TX gain and the digital scale change at once from the Setup tab. With an
amplifier in the chain the 7.2 limits are enforced per key-down (T_on,max, then a forced
release; T_off,min before the next key-down) and a watchdog releases the transmitter if
the host stalls. Every key event, level step and signal change is time-stamped in the
log and the one-page report so meter readings line up with what the B210 was told.

Signals: **CW** (one tone at the dial frequency plus an offset), **two-tone** (two equal
tones spaced by `spacing_hz` around the offset, the intermodulation test), and the
**EVE waveform** itself (the message's tone hops at the design frame rate, the pilot
first, cycling for as long as the key is down: the real comb the amplifier will see).
The level has two handles: the B210 TX gain (0 to 89.75 dB, the coarse control) and a
digital scale in dB below full scale (fine steps, no relock of the radio). An optional
timed sweep steps the TX gain from start to stop by `step_db` every `hold_s` seconds,
automatically on the first key-down and again whenever the operator asks; unkeying stops it.

`GeneratorSession` is a `Session` with the receive side removed and the schedule-driven
keyer thread replaced by operator commands; the abort, log and report machinery are the
session's own. In the software simulation the samples go to a null sink and the sequencer
still switches the real relay board, so the bench wiring can be exercised with nothing on
the air.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
from gnuradio import gr, blocks

from . import schedule as S
from .doppler import iso_utc
from .modem import OFF, FrameMap
from .station import PA_T_OFF_MIN_S, PA_T_ON_MAX_S, Session, SessionOptions

SIGNALS = ("cw", "two_tone", "eve")
SIGNAL_NAMES = {"cw": "CW", "two_tone": "two-tone", "eve": "EVE waveform"}
WATCHDOG_S = 5.0            # a keyed transmitter is released this long after the run loop's last liveness tick


class GeneratorSource(gr.sync_block):
    """A phase-continuous source at the radio rate `fs` whose kind, frequencies and on/off
    state change while it runs (set_signal, set_on; all thread-safe). Silent until keyed.

    CW: one tone at `offset_hz`. Two-tone: tones at offset +/- spacing/2, each at half
    amplitude so the peak envelope equals the CW case. EVE: the frame map's tone sequence
    (pilot first when enabled, then the message symbols at n_frames frames each) at the
    IF comb (f_IF + d x spacing), restarting at frame 0 on every key-down and cycling the
    message while the key stays down; the phase is carried across the hops as the modem's
    synthesizer does. `samples_on` and `frames_sent` mirror EveToneSource for the report."""

    def __init__(self, fs: float, frame_map: Optional[FrameMap] = None, kind: str = "cw", offset_hz: float = 0.0,
                 spacing_hz: float = 10e3, amplitude: float = 0.8, block_len: int = 65536):
        gr.sync_block.__init__(self, name="eve_generator_source", in_sig=None, out_sig=[np.complex64])
        self.fs = float(fs)
        self.amp = float(amplitude)
        self.map = frame_map
        self.p = frame_map.p if frame_map is not None else None
        self._lock = threading.Lock()
        self._on = False
        self._stop = False
        self.kind = "cw"
        self.offset_hz = 0.0
        self.spacing_hz = float(spacing_hz)
        self._freqs: List[float] = [0.0]
        self._phases: List[float] = [0.0, 0.0]
        self._eve_seq: Optional[np.ndarray] = None
        self._n_on = 0                  # samples since the current key-down (the EVE frame clock)
        self.n_out = 0
        self.samples_on = 0
        self.frames_sent: set = set()
        self.done = False
        self._block_len = block_len
        self.set_signal(kind, offset_hz, spacing_hz)
        self.set_min_output_buffer(1 << 22)

    # ---- control (any thread) ------------------------------------------------------------
    def set_signal(self, kind: str, offset_hz: Optional[float] = None, spacing_hz: Optional[float] = None) -> None:
        """Change the signal kind and its frequencies; takes effect at the next work call."""
        if kind not in SIGNALS:
            raise ValueError(f"signal must be one of {SIGNALS}")
        if kind == "eve" and self.map is None:
            raise ValueError("the EVE waveform needs a frame map")
        with self._lock:
            self.kind = kind
            if offset_hz is not None:
                self.offset_hz = float(offset_hz)
            if spacing_hz is not None:
                self.spacing_hz = float(spacing_hz)
            if kind == "cw":
                self._freqs = [self.offset_hz]
            elif kind == "two_tone":
                self._freqs = [self.offset_hz - self.spacing_hz / 2.0, self.offset_hz + self.spacing_hz / 2.0]
            else:
                if self._eve_seq is None:
                    n_cycle = self.p.n_frames_msg + (self.p.pilot_frames if self.map.pilot else 0)
                    self._eve_seq = self.map.tones(0, n_cycle - 1)
                self._freqs = []

    def set_on(self, on: bool) -> None:
        """RF on or off (the sequencer has already keyed the transmitter). A key-down restarts
        the EVE frame clock at frame 0."""
        with self._lock:
            if on and not self._on:
                self._n_on = 0
            self._on = bool(on)

    def stop(self) -> None:
        """End the stream: the next work call returns -1."""
        with self._lock:
            self._stop = True
            self._on = False

    @property
    def on(self) -> bool:
        return self._on

    def describe(self) -> str:
        """One phrase: 'CW at dial +0.000 kHz', 'two tones 10.000 kHz apart at dial +0.000 kHz',
        or 'EVE waveform (pilot + message, cycling)'."""
        if self.kind == "cw":
            return f"CW at dial {self.offset_hz / 1e3:+.3f} kHz"
        if self.kind == "two_tone":
            return f"two tones {self.spacing_hz / 1e3:.3f} kHz apart at dial {self.offset_hz / 1e3:+.3f} kHz"
        return "EVE waveform (pilot + message, cycling)"

    # ---- GNU Radio ----------------------------------------------------------------------
    def work(self, input_items, output_items):
        out = output_items[0]
        if self.done:
            return -1
        n = min(len(out), self._block_len)
        with self._lock:
            if self._stop:
                self.done = True
                return -1
            on, kind, freqs = self._on, self.kind, list(self._freqs)
            n_on0 = self._n_on
            if on:
                self._n_on += n
        if not on:
            out[:n] = 0
            self.n_out += n
            return n
        if kind == "eve":
            idx = np.arange(n_on0, n_on0 + n, dtype=np.float64)
            k = np.floor(idx / self.fs * self.p.r_bw + 1e-9).astype(np.int64)
            seq = self._eve_seq
            kk = k % len(seq)
            tones = seq[kk]
            f = np.where(tones == OFF, 0.0, self.p.f_if + tones * self.p.spacing)
            phase = self._phases[0] + np.cumsum(2.0 * np.pi * f / self.fs)
            y = (self.amp * np.exp(1j * phase)).astype(np.complex64)
            y[tones == OFF] = 0
            self._phases[0] = float(phase[-1] % (2.0 * np.pi))
            self.frames_sent.update(int(u) for u in np.unique(kk))
        else:
            idx = np.arange(n, dtype=np.float64)
            y = np.zeros(n, dtype=np.complex64)
            a = self.amp / len(freqs)
            for i, fr in enumerate(freqs):
                y += (a * np.exp(1j * (self._phases[i] + 2.0 * np.pi * fr * idx / self.fs))).astype(np.complex64)
                self._phases[i] = float((self._phases[i] + 2.0 * np.pi * fr * n / self.fs) % (2.0 * np.pi))
        out[:n] = y
        self.n_out += n
        self.samples_on += n
        return n


class _NoSink:
    """Stands in for EveRxSink: the generator receives nothing."""
    samples_in = 0

    def close(self) -> Dict:
        return {}


@dataclass
class SweepSpec:
    """A timed TX-gain sweep: from start_db to stop_db in steps of step_db, holding each
    for hold_s seconds, run while the transmitter is keyed."""
    start_db: float
    stop_db: float
    step_db: float
    hold_s: float

    def levels(self) -> List[float]:
        if self.step_db <= 0:
            return [self.start_db]
        n = int(math.floor(abs(self.stop_db - self.start_db) / self.step_db + 1e-9)) + 1
        sign = 1.0 if self.stop_db >= self.start_db else -1.0
        return [round(self.start_db + sign * i * self.step_db, 3) for i in range(n)]

    def __str__(self) -> str:
        return f"{self.start_db:g} -> {self.stop_db:g} dB by {self.step_db:g} every {self.hold_s:g} s"


def build_generator_schedule(session_id: str, t_start: float, f_dial_hz: float, params, text: str,
                             pilot: bool = True) -> S.Schedule:
    """The container schedule a generator session needs (session id, dial frequency,
    parameters, the message symbols for the EVE waveform, one nominal on-window at
    t_start): the generator's keying is the operator's, not this table's."""
    from . import doppler as D
    now = t_start - 60.0
    model = D.DopplerModel(D.synthetic_table("siggen", D.DSES_HASWELL, now, now + 8 * 3600.0, range_km=0.15))
    n = max(1, int(round(2.0 / params.t_frame)))
    sched = S.build_schedule(session_id, "siggen", model, t_start, f_dial_hz, params=params, text=text,
                             repeat_count=1, pilot=pilot, chunk_s=n * params.t_frame + 1e-6, rtt_guard_s=0.0,
                             t_off_min_s=0.0, t_on_max_s=3600.0, mode="bistatic_tx",
                             notes="signal generator: keyed by the operator; this table is nominal")
    sched.chunks = sched.chunks[:1]
    return sched


class GeneratorSession(Session):
    """`Session` with the receive side removed, the transmit source replaced by the chosen
    signal, and the operator in charge of the key: key(True/False), set_signal, set_level,
    start_sweep, stop, all thread-safe (the worker calls them from its command loop).
    `steps` records every level change, signal change and key event as {t, utc, ..., why}
    for the report; `on_step` is called with the same dict (the worker forwards it)."""

    def __init__(self, schedule, radio, opts: SessionOptions, signal: str = "cw", offset_hz: float = 0.0,
                 spacing_hz: float = 10e3, scale_db: float = -3.0, tx_gain_db: float = 0.0,
                 sweep: Optional[SweepSpec] = None, log: Callable[[str], None] = None, keyer=None,
                 on_step: Optional[Callable[[Dict], None]] = None):
        super().__init__(schedule, None, radio, opts, log=log, keyer=keyer)
        if signal not in SIGNALS:
            raise ValueError(f"signal must be one of {SIGNALS}")
        self.signal = signal
        self.offset_hz = float(offset_hz)
        self.spacing_hz = float(spacing_hz)
        self.scale_db = float(scale_db)
        self.tx_gain_db = float(tx_gain_db)
        self.sweep = sweep
        self.on_step = on_step
        self.steps: List[Dict] = []
        self.scale = None
        self._stop = threading.Event()
        self._key_lock = threading.Lock()
        self._sweep_thread: Optional[threading.Thread] = None
        self._sweep_stop = threading.Event()
        self.t_key_on: Optional[float] = None       # device time of the current key-down
        self.t_key_off: Optional[float] = None      # device time of the last release
        self.key_downs = 0
        self.keyed_total_s = 0.0
        self._wd: Optional[threading.Timer] = None

    # ---- preflight: the radio, not the nominal table ---------------------------------------
    def preflight(self) -> List[str]:
        """The generator has no chunk table to check; the radio must give a time."""
        problems = []
        try:
            self.radio.device_time()
        except Exception as e:      # noqa: BLE001
            problems.append(f"radio: {e}")
        return problems

    # ---- flowgraph ----------------------------------------------------------------------
    def build(self, t0: float) -> gr.top_block:
        """The generator source through a live digital scale into the transmit sink (a null
        sink in the simulation); nothing on the receive side."""
        s, p = self.sched, self.p
        tb = gr.top_block(f"eve_generator_{s.session_id}")
        sim = getattr(self.radio, "sim", False)
        rate = self.radio.tx_rate
        fmap = FrameMap(s.symbols, p, on_windows=None, pilot=s.pilot_enabled, repeat_count=None)
        self.tone = GeneratorSource(rate, fmap, kind=self.signal, offset_hz=self.offset_hz,
                                    spacing_hz=self.spacing_hz, amplitude=p.amplitude)
        self.scale = blocks.multiply_const_cc(complex(10 ** (self.scale_db / 20.0)))
        tb.connect(self.tone, self.scale)
        if sim:
            tb.connect(self.scale, blocks.null_sink(gr.sizeof_gr_complex))
        else:
            tb.connect(self.scale, self.radio.tx)
            getattr(self.radio, "connect_tx_events", lambda _tb: None)(tb)
        self.sink = _NoSink()
        self.decim = None
        self.acc = None
        self.tb = tb
        return tb

    # ---- records --------------------------------------------------------------------------
    def _record(self, why: str) -> None:
        t = self.radio.device_time()
        d = {"t": t, "utc": iso_utc(t, 1), "tx_gain_db": self.tx_gain_db, "scale_db": self.scale_db,
             "signal": self.signal, "keyed": bool(self.keyer.keyed), "why": why}
        self.steps.append(d)
        if self.on_step is not None:
            try:
                self.on_step(d)
            except Exception:
                pass

    # ---- operator commands (any thread) ---------------------------------------------------
    def set_level(self, tx_gain_db: Optional[float] = None, scale_db: Optional[float] = None, why: str = "operator") -> None:
        """Apply a new TX gain (B210, dB) and/or digital scale (dB below full scale) at once,
        keyed or not; records the step."""
        if tx_gain_db is not None:
            self.tx_gain_db = float(tx_gain_db)
            tx = getattr(self.radio, "tx", None)
            if tx is not None and hasattr(tx, "set_gain"):
                tx.set_gain(self.tx_gain_db, 0)
        if scale_db is not None:
            self.scale_db = float(scale_db)
            if self.scale is not None:
                self.scale.set_k(complex(10 ** (self.scale_db / 20.0)))
        self.log(f"level: TX gain {self.tx_gain_db:.2f} dB, scale {self.scale_db:+.1f} dB ({why})")
        self._record("level: " + why)

    def set_signal(self, signal: str, offset_hz: Optional[float] = None, spacing_hz: Optional[float] = None) -> None:
        """Change the signal (CW / two-tone / EVE waveform) and its frequencies live."""
        if signal not in SIGNALS:
            raise ValueError(f"signal must be one of {SIGNALS}")
        self.signal = signal
        if offset_hz is not None:
            self.offset_hz = float(offset_hz)
        if spacing_hz is not None:
            self.spacing_hz = float(spacing_hz)
        if self.tone is not None:
            self.tone.set_signal(signal, self.offset_hz, self.spacing_hz)
        self.log(f"signal: {self._signal_text()}")
        self._record("signal")

    def _signal_text(self) -> str:
        if self.tone is not None:
            return self.tone.describe()
        return {"cw": f"CW at dial {self.offset_hz / 1e3:+.3f} kHz",
                "two_tone": f"two tones {self.spacing_hz / 1e3:.3f} kHz apart at dial {self.offset_hz / 1e3:+.3f} kHz",
                "eve": "EVE waveform (pilot + message, cycling)"}[self.signal]

    def key(self, on: bool, why: str = "operator") -> bool:
        """Key the transmitter (sequencer first, then the RF after the key lead) or release
        it (RF off, key lag, sequencer). Refused while the amplifier's minimum off time has
        not passed, or when the session is not running. Returns True when the state changed."""
        with self._key_lock:
            if self.tb is None or self._abort.is_set() or self._stop.is_set():
                self.log(f"key {'down' if on else 'up'} ignored: the generator is not running")
                return False
            now = self.radio.device_time()
            if on:
                if self.keyer.keyed:
                    return False
                if self.opts.pa_in_chain and self.t_key_off is not None and now - self.t_key_off < PA_T_OFF_MIN_S:
                    wait = PA_T_OFF_MIN_S - (now - self.t_key_off)
                    self.log(f"KEY REFUSED: the amplifier needs {PA_T_OFF_MIN_S:.0f} s off; {wait:.0f} s to go")
                    self.phase = f"key up: amplifier cooling, {wait:.0f} s to go"
                    return False
                # the on-time counts from the key, not from the RF: set before the key lead so
                # the run loop's amplifier limit never sees the previous key-down's time
                self.t_key_on = self.radio.device_time()
                self.keyer.key(True)
                if self.keyer.fault:
                    self.log(f"KEY LINE FAULT: {self.keyer.fault}")
                time.sleep(self.opts.t_lead_s)
                self.tone.set_on(True)
                self.key_downs += 1
                self.report.chunks_keyed = self.key_downs
                self.report.key_events.append({"t": self.t_key_on, "on": True, "chunk": self.key_downs - 1, "why": why})
                self.log(f"KEY DOWN ({why}): {self._signal_text()}, TX gain {self.tx_gain_db:.2f} dB, scale {self.scale_db:+.1f} dB")
                self.phase = "KEYED: transmitting"
                self._record("key down: " + why)
                self._arm_watchdog()
                if self.sweep is not None and self.key_downs == 1:
                    self.start_sweep()          # automatic on the first key-down only; the Run tab's Sweep reruns it
                return True
            if not self.keyer.keyed:
                return False
            self._sweep_stop.set()
            self.tone.set_on(False)
            time.sleep(self.opts.t_lag_s)
            self.keyer.key(False)
            if self._wd is not None:
                self._wd.cancel()
            self.t_key_off = self.radio.device_time()
            on_s = self.t_key_off - self.t_key_on if self.t_key_on is not None else 0.0
            self.keyed_total_s += on_s
            self.t_key_on = None
            self.report.key_events.append({"t": self.t_key_off, "on": False, "chunk": self.key_downs - 1, "why": why})
            self.log(f"key up ({why}): {on_s:.1f} s keyed")
            self.phase = "key up: ready"
            self._record("key up: " + why)
            return True

    def _arm_watchdog(self) -> None:
        """A timer that releases the transmitter if the run loop stops ticking (a stalled
        host); re-armed by every tick of run(). The amplifier on-time limit is enforced by
        the tick itself."""
        if self._wd is not None:
            self._wd.cancel()
        self._wd = threading.Timer(WATCHDOG_S, self._watchdog_fire)
        self._wd.daemon = True
        self._wd.start()

    def _watchdog_fire(self) -> None:
        if self.keyer.keyed and not self._abort.is_set():
            self.log(f"KEYER WATCHDOG: no liveness for {WATCHDOG_S:.0f} s while keyed; releasing the transmitter")
            try:
                self.tone.set_on(False)
                self.keyer.key(False)
            except Exception:
                pass
            self.abort("keyer watchdog")

    def start_sweep(self) -> bool:
        """Run the timed TX-gain sweep now (the key must be down); a running sweep restarts
        from its first level."""
        if self.sweep is None:
            self.log("no sweep configured")
            return False
        if not self.keyer.keyed:
            self.log("sweep needs the transmitter keyed")
            return False
        self._sweep_stop.set()
        if self._sweep_thread is not None and self._sweep_thread.is_alive():
            self._sweep_thread.join(1.0)
        self._sweep_stop = threading.Event()
        self._sweep_thread = threading.Thread(target=self._run_sweep, args=(self._sweep_stop,), name="eve-sweep", daemon=True)
        self._sweep_thread.start()
        return True

    def _run_sweep(self, stop: threading.Event) -> None:
        sw = self.sweep
        self.log(f"sweep {sw}")
        for lvl in sw.levels():
            if stop.is_set() or self._abort.is_set() or not self.keyer.keyed:
                self.log("sweep stopped")
                return
            self.set_level(tx_gain_db=lvl, why="sweep")
            t_stop = time.monotonic() + sw.hold_s
            while not stop.is_set() and not self._abort.is_set() and time.monotonic() < t_stop:
                time.sleep(0.05)
        self.log("sweep done")

    def stop(self, why: str = "operator stop") -> None:
        """End the run cleanly (the key is released first); not an abort."""
        self.log(f"generator stop ({why})")
        self._stop.set()

    def abort(self, reason: str) -> None:
        """Abort: RF off before the sequencer releases (Session.abort keys the line off)."""
        try:
            if self.tone is not None:
                self.tone.set_on(False)
        except Exception:
            pass
        super().abort(reason)

    # ---- the run ------------------------------------------------------------------------------
    def run(self):
        """Start the flowgraph with the key up and stay up until stop() or abort(); every
        0.2 s tick re-arms the watchdog and, with an amplifier in the chain, releases the key
        at T_on,max. Returns the SessionReport (Session._finish: log, key events, samples)."""
        probs = self.preflight()
        if probs:
            raise ValueError("preflight failed:\n  " + "\n  ".join(probs))
        self.report.started_utc = iso_utc(time.time(), 0)
        self.report.radio = getattr(self.radio, "status_text", lambda: "")()
        tb = self.build(self.radio.device_time())
        rt = None
        if self.opts.realtime_mode:
            try:
                from ._workbench import import_dses_radio
                rt = import_dses_radio().RealtimeMode()
                rt.begin()
            except Exception:
                rt = None
        self.log(f"generator {self.sched.session_id}: {self._signal_text()}; TX gain {self.tx_gain_db:.2f} dB, "
                 f"scale {self.scale_db:+.1f} dB" + (f"; sweep {self.sweep}" if self.sweep else "")
                 + (f"; amplifier limits {PA_T_ON_MAX_S:.0f} s on / {PA_T_OFF_MIN_S:.0f} s off" if self.opts.pa_in_chain else ""))
        self._record("start")
        try:
            tb.start()
            self.phase = "key up: ready"
            while not self._abort.is_set() and not self._stop.is_set():
                if self.keyer.keyed:
                    self._arm_watchdog()
                    if self.opts.pa_in_chain and self.t_key_on is not None:
                        if self.radio.device_time() - self.t_key_on >= PA_T_ON_MAX_S:
                            self.log(f"amplifier on-time limit {PA_T_ON_MAX_S:.0f} s reached: releasing the key")
                            self.key(False, why="amplifier on-time limit")
                time.sleep(0.2)
            if self.keyer.keyed:
                self.key(False, why="stop")
        finally:
            if self._wd is not None:
                self._wd.cancel()
            self._sweep_stop.set()
            try:
                self.tone.stop()
            except Exception:
                pass
            try:
                tb.stop()
                tb.wait()
            except Exception as e:      # noqa: BLE001
                self.log(f"flowgraph stop: {e}")
            self.phase = "aborted" if self._abort.is_set() else "finished"
        return self._finish(rt, None)

    # ---- for the panel ----------------------------------------------------------------------
    def status_line(self) -> str:
        """One line for the panel: signal, level, key state and on-time, sweep."""
        keyed = self.keyer.keyed
        if keyed and self.t_key_on is not None:
            on_s = self.radio.device_time() - self.t_key_on
            ks = f"KEYED {on_s:.0f} s" + (f" of {PA_T_ON_MAX_S:.0f}" if self.opts.pa_in_chain else "")
        elif self.opts.pa_in_chain and self.t_key_off is not None:
            wait = PA_T_OFF_MIN_S - (self.radio.device_time() - self.t_key_off)
            ks = f"key up (amplifier cooling, {wait:.0f} s to go)" if wait > 0 else "key up"
        else:
            ks = "key up"
        sw = f"  sweep {self.sweep}" if self.sweep is not None else ""
        return f"GENERATOR: {self._signal_text()}; TX gain {self.tx_gain_db:.2f} dB, scale {self.scale_db:+.1f} dB; {ks}{sw}"

    def sched_text(self) -> str:
        """The schedule pane's text for the generator: key-downs and on-time so far."""
        total = self.keyed_total_s
        if self.keyer.keyed and self.t_key_on is not None:
            total += self.radio.device_time() - self.t_key_on
        return (f"signal generator: {self.key_downs} key-down(s), {total:.0f} s keyed so far\n"
                f"KEY / UNKEY and STOP on this tab; TX gain, scale and signal from the Setup tab")


def write_generator_report(sess: GeneratorSession, rep, out_pdf: Path, extra: Optional[Dict[str, str]] = None) -> Path:
    """A one-page PDF: the settings, every level step, signal change and key event with its
    UTC time, so bench readings can be tied to what the B210 was commanded to do."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    lines = [f"DSES EVE modem signal generator - {sess.sched.session_id}",
             f"started {rep.started_utc}   finished {rep.finished_utc}   aborted={rep.aborted} {rep.abort_reason}",
             f"dial {sess.sched.f_dial_hz / 1e6:.4f} MHz   last: {sess.status_line()}",
             f"radio: {rep.radio}",
             f"key-downs {sess.key_downs}; keyed {sess.keyed_total_s:.1f} s in total"
             + (f"; amplifier limits {PA_T_ON_MAX_S:.0f} s on / {PA_T_OFF_MIN_S:.0f} s off" if sess.opts.pa_in_chain else ""),
             ""]
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["", "events (UTC, TX gain dB, scale dB, signal, key, what):"]
    for d in sess.steps:
        lines.append(f"  {d['utc'][11:21]}   {d['tx_gain_db']:7.2f}   {d['scale_db']:+6.1f}   "
                     f"{SIGNAL_NAMES.get(d.get('signal', ''), ''):12s} {'KEYED' if d.get('keyed') else 'up   '}  {d['why']}")
    with PdfPages(str(out_pdf)) as pdf:
        for start in range(0, max(1, len(lines)), 58):
            fig = Figure(figsize=(8.5, 11))
            fig.text(0.06, 0.96, "\n".join(lines[start:start + 58]), va="top", ha="left", family="monospace", fontsize=8)
            pdf.savefig(fig)
    return out_pdf
