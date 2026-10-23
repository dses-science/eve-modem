"""Session controller: key-down and cooldown interlocks, PA duty enforcement, abort, the
flowgraph, the session log (design document 5.1, 7.2, 8.4).

One flowgraph for the whole session (monostatic Venus and EME alike): the schedule-driven
EveToneSource streams from t0 into the usrp_sink (zeros between chunks keep the sample
clock running, so sample n is device time t0 + n / fs exactly), the usrp_source streams
from the same t0 into the decimator and the EveRxSink, which files only the receive
windows. A keyer thread raises the sequencer line T_lead before each chunk and drops it
T_lag after the RF stops; a watchdog drops it if a chunk overruns. Abort (Ctrl-C or a
fault) releases the key first, then stops the flowgraph, then closes the archive.

SimRadio stands in for the B210 for the software bench: the tone source feeds an AWGN
adder whose output is the receive stream, device time is the wall clock, keying is
logged. Everything else in the session is identical, which is the point.
"""
from __future__ import annotations

import json
import signal
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
from gnuradio import gr, blocks, analog

from . import __version__
from .params import EveParams
from .schedule import Schedule
from .doppler import DopplerModel, iso_utc
from . import gr_blocks, modem, sync

WATCHDOG_MARGIN_S = 2.0          # the keyer watchdog releases the transmitter this long after a chunk's end at the latest
PA_T_ON_MAX_S = 300.0       # 7.2: enforced regardless of the schedule when an amplifier is in the chain
PA_T_OFF_MIN_S = 240.0


@dataclass
class SessionOptions:
    """How a session runs, as opposed to what it transmits (that is the Schedule). Times in
    seconds: t_lead_s / t_lag_s are the key-line lead before the first RF sample and the lag
    after the last (design document 7.2: 200 / 100 ms); start_margin_s / end_margin_s pad
    the flowgraph around the schedule; arm_lead_s starts the flowgraph early so the timed
    USRP streams are armed (D22). pa_in_chain enforces the 7.2 duty limits whatever the
    schedule says; tx_precompensate / rx_doppler_removal choose which end applies the
    Doppler model (6.5); tx_enabled False is the receive-only interop mode (no RF, no
    keying); realtime_mode asks the Workbench's RealtimeMode helper for the run."""
    out_dir: str = "archive"
    t_lead_s: float = 0.2
    t_lag_s: float = 0.1
    pa_in_chain: bool = True            # enforce the 7.2 hard limits
    rx_doppler_removal: bool = False    # True when this receiver is not the pre-compensated one
    tx_precompensate: bool = True       # monostatic: pre-compensate for our own receiver
    live_decode: bool = True
    start_margin_s: float = 2.0         # flowgraph starts this long before the first chunk
    end_margin_s: float = 2.0
    arm_lead_s: float = 8.0             # start the flowgraph this long before t0: tb.start() with two USRP
                                        # streamers took 3.8 s on the Windows bench (2026-09-11), and a late timed
                                        # command drops the whole TX stream ('L' storm) and the RX start (LATE_COMMAND)
    realtime_mode: bool = True
    tx_enabled: bool = True             # False = receive only (a partner transmits): no RF, no keying


class SimRadio:
    """Software stand-in for EveRadio: AWGN channel from TX to RX, wall-clock device time."""

    def __init__(self, params: EveParams, cn0_db: Optional[float] = 20.0, rate: Optional[float] = None, seed: int = 1,
                 realtime: bool = True):
        self.p = params
        self.rate = float(rate or params.radio_rate)
        self.rx_rate = self.tx_rate = self.rate
        self.cn0_db = cn0_db
        self.seed = seed
        self.realtime = realtime            # pace the stream at the wall clock (see build_channel)
        self.key_log: List[tuple] = []
        self._keyed = False
        self.sim = True
        self.time_offset = 0.0              # tests: jump the device clock (a stalled host seen from the stream)
        self.resync_test = False            # tests: let the tone source watch this clock and resync

    def open(self):
        """No hardware to open; returns self so callers can treat it like EveRadio.open()."""
        return self

    def close(self):
        """Nothing to release."""
        pass

    def device_time(self) -> float:
        """The wall clock (time.time(), Unix seconds) stands in for the USRP clock."""
        return time.time() + self.time_offset

    def key(self, on: bool) -> None:
        """Record the TX key line as (host time, state) in key_log instead of driving a pin."""
        self._keyed = bool(on)
        self.key_log.append((time.time(), bool(on)))

    def set_lines(self, tx: bool, lna_active: bool) -> None:
        """The two station lines, recorded rather than driven (the sequencer runs in the
        simulation exactly as on the air; a USB relay board, if present, clicks as well)."""
        self._keyed = bool(tx)
        self.lines = (bool(tx), bool(lna_active))
        self.key_log.append((time.time(), bool(tx), bool(lna_active)))

    @property
    def keyed(self):
        """Last commanded TX state."""
        return self._keyed

    def build_channel(self, tb: gr.top_block, tone_source, rtt_s: float = 0.0):
        """Connect the tone source through a round-trip delay and AWGN; return the block
        whose output is the RX stream."""
        amp = self.p.amplitude
        src = tone_source
        if rtt_s > 0:
            self.delay = blocks.delay(gr.sizeof_gr_complex, int(round(rtt_s * self.rate)))
            tb.connect(src, self.delay)
            src = self.delay
        if self.cn0_db is not None:
            sigma = np.sqrt(amp ** 2 / (10 ** (self.cn0_db / 10.0)) * self.rate)
            # GR's complex Gaussian source puts `ampl` std on each component: total variance 2 ampl^2
            noise = analog.noise_source_c(analog.GR_GAUSSIAN, sigma / np.sqrt(2.0), self.seed)
            adder = blocks.add_cc()
            tb.connect(src, (adder, 0))
            tb.connect(noise, (adder, 1))
            src = adder
        if self.realtime:
            # Without a throttle the flowgraph runs as fast as the CPU allows, so the receive
            # stream (tables, tone strip) races minutes ahead of the wall-clock schedule the
            # keying, phase badge and schedule pane follow (seen on the Linux VM 2026-09-13).
            # The throttle goes LAST: the delay block emits its round-trip's worth of silence
            # at once, so a throttle ahead of it left the stream a whole RTT early.
            self.throttle = blocks.throttle(gr.sizeof_gr_complex, self.rate, ignore_tags=True)
            tb.connect(src, self.throttle)
            src = self.throttle
        return src

    def status_text(self) -> str:
        """One-line description for the session log's radio entry."""
        return f"SimRadio: rate {self.rate:.2f} S/s, C/N0 {self.cn0_db} dB-Hz"


@dataclass
class SessionReport:
    """What a run produced, written to <session_id>_session.json (design document 8.4):
    start and finish UTC, the radio's one-line status, chunks keyed and the key events
    (device time, state, chunk index or reason), transmitted samples and frames, the
    receive sink's report (samples, gaps, files), the live decode, and the abort state."""
    session_id: str
    started_utc: str = ""
    finished_utc: str = ""
    radio: str = ""
    chunks_keyed: int = 0
    key_events: List[Dict] = field(default_factory=list)
    tone_samples_on: int = 0
    frames_sent: int = 0
    rx: Dict = field(default_factory=dict)
    live_decode: Dict = field(default_factory=dict)
    aborted: bool = False
    abort_reason: str = ""
    stopped_early: bool = False       # Session.stop(): ended cleanly before the last chunk
    stop_reason: str = ""
    notes: List[str] = field(default_factory=list)
    tx_events: Dict = field(default_factory=dict)      # underflow / late / seq_error counts (EveRadio)
    tx_resyncs: List[Dict] = field(default_factory=list)   # the stream re-timed itself after a stall (D26)
    level_events: List[Dict] = field(default_factory=list)   # TX gain changed live by the operator (device time, dB)


class Session:
    """One session on one radio: preflight the schedule against the interlocks, build the
    flowgraph, arm it before t0, run the keyer thread through the chunk table, drain, and
    write the session log (design document 5.1, 7.2, 8.4). `radio` is an EveRadio or a
    SimRadio; `model` the DopplerModel behind the schedule (None only in simulation);
    `keyer` a Keyer (default: the radio's own GPIO_0 through GpioKeyer). Callables
    appended to `listeners` receive (frame k, samples, metric) for every received frame,
    called from the sink's worker thread; `phase` is a short text for the operator display."""
    def __init__(self, schedule: Schedule, model: Optional[DopplerModel], radio, opts: Optional[SessionOptions] = None,
                 log: Callable[[str], None] = None, keyer=None):
        self.sched = schedule
        self.model = model
        self.radio = radio
        # the key line to the sequencer (7.2): a Keyer object; default = the radio's own
        # GPIO line (EveRadio.key) or the simulated radio's log
        from . import keyer as _keyer
        self.keyer = keyer if keyer is not None else _keyer.GpioKeyer(radio)
        self.opts = opts or SessionOptions()
        self.log = log or (lambda s: print(s, file=sys.stderr, flush=True))
        self.p = schedule.params
        self.report = SessionReport(schedule.session_id)
        self._abort = threading.Event()
        self._stop = threading.Event()          # Session.stop(): finish the current chunk, key no more
        self._t_end_eff: Optional[float] = None  # the run's end once a stop cut the chunk table short
        self.tb: Optional[gr.top_block] = None
        self.tone = None
        self.sink = None
        self.acc: Optional[modem.SymbolAccumulator] = None
        self.listeners: List[Callable[[int, np.ndarray, np.ndarray], None]] = []   # (frame k, samples, metric)
        self.phase = "idle"

    # ---- interlocks (7.2) ------------------------------------------------------------------
    def preflight(self) -> List[str]:
        """Check the schedule against the station interlocks and return the problems found
        (empty = go). Schedule self-consistency (Schedule.validate); with pa_in_chain the
        7.2 hard limits (T_on <= 300 s, T_off >= 240 s) regardless of the schedule's own
        limits; enough silence between chunks for the sequencer (T_lead + T_lag + the
        keyer's settle_s and release_s, see keyer.sequencer_gap_s); an ephemeris model
        wherever Doppler is applied."""
        problems = []
        s = self.sched
        try:
            s.validate()
        except ValueError as e:
            problems.append(f"schedule: {e}")
        if s.target in ("moon", "venus") and self.opts.tx_enabled:
            # an on-air run stays inside the US amateur bands, comb and all (Rick, 2026-10-23)
            from . import bands
            bad = bands.check_on_air(s.f_dial_hz, s.params)
            if bad:
                problems.append("on the air: " + bad)
        if self.opts.pa_in_chain:
            for c in s.chunks:
                if c.tx_stop - c.tx_start > PA_T_ON_MAX_S + 1e-6:
                    problems.append(f"chunk {c.index}: {c.tx_stop - c.tx_start:.0f} s on exceeds the PA limit {PA_T_ON_MAX_S:.0f} s")
            for a, b in zip(s.chunks, s.chunks[1:]):
                if b.tx_start - a.tx_stop < PA_T_OFF_MIN_S - 1e-6:
                    problems.append(f"chunk {b.index}: off time {b.tx_start - a.tx_stop:.0f} s under the PA minimum {PA_T_OFF_MIN_S:.0f} s")
        # the sequencer needs its guard, lead, lag and release between two chunks' RF
        need = (self.opts.t_lead_s + self.opts.t_lag_s + float(getattr(self.keyer, "settle_s", 0.0))
                + float(getattr(self.keyer, "release_s", 0.0)))
        if self.opts.tx_enabled and need > 0.0:
            for a, b in zip(s.chunks, s.chunks[1:]):
                gap = b.tx_start - a.tx_stop
                if gap < need - 1e-6:
                    problems.append(f"chunk {b.index}: off time {gap:.2f} s is shorter than the sequencer needs "
                                    f"({need:.2f} s: LNA guard, key lead and lag, LNA release); raise the minimum off time")
                    break
        if self.opts.rx_doppler_removal and self.model is None:
            problems.append("receiver-side Doppler removal needs an ephemeris model")
        if self.opts.tx_precompensate and self.model is None and self.sched.mode != "sim":
            problems.append("transmit pre-compensation needs an ephemeris model")
        return problems

    def abort(self, reason: str) -> None:
        """Stop the run from any thread: record the reason, set the abort flag the keyer and
        run loops watch, and release the key line first (a Sequencer drops the transmitter
        before it restores the LNA). A second call does nothing."""
        if not self._abort.is_set():
            self.report.aborted = True
            self.report.abort_reason = reason
            self._abort.set()
            try:
                self.keyer.key(False)
                self.report.key_events.append({"t": time.time(), "on": False, "why": f"abort: {reason}"})
            except Exception:
                pass

    def stop(self, reason: str = "operator stop") -> None:
        """End the run cleanly from any thread: the chunk being transmitted (if any) runs to
        its end and its echo is received, no further chunk is keyed, the stream ends after
        that echo, and the offline decode and the report follow as after a complete run.
        The report records stopped_early and the reason. ABORT (abort()) is the immediate
        version: key released at once, nothing more received."""
        if not self._stop.is_set() and not self._abort.is_set():
            self.report.stopped_early = True
            self.report.stop_reason = reason
            self.log(f"STOP requested ({reason}): the current chunk completes, no further chunk is keyed")
            self._stop.set()

    def set_level(self, tx_gain_db: Optional[float] = None, scale_db: Optional[float] = None,
                  why: str = "operator", rx_gain_db: Optional[float] = None) -> None:
        """Change the B210 TX gain and/or RX gain (dB) during the run, keyed or not, from any
        thread: applied at once, logged, and recorded in the session JSON (level_events) so
        the report's reader knows what drive the amplifier saw and what receive gain the
        archive was taken at (Rick, 2026-10-23: the final amplifier is being brought up
        during the Venus days). The digital scale is the generator's knob only; here it is
        noted and ignored."""
        if tx_gain_db is not None:
            g = float(tx_gain_db)
            tx = getattr(self.radio, "tx", None)
            if tx is not None and hasattr(tx, "set_gain"):
                tx.set_gain(g, 0)
            t = self.radio.device_time()
            self.report.level_events.append({"t": t, "tx_gain_db": g, "why": why})
            self.log(f"TX gain {g:.2f} dB ({why}) at {iso_utc(t, 0)[11:19]}Z, "
                     f"{'keyed' if self.keyer.keyed else 'not keyed'}")
        if rx_gain_db is not None:
            g = float(rx_gain_db)
            rx = getattr(self.radio, "rx", None)
            if rx is not None and hasattr(rx, "set_gain"):
                rx.set_gain(g)
            t = self.radio.device_time()
            self.report.level_events.append({"t": t, "rx_gain_db": g, "why": why})
            self.log(f"RX gain {g:.2f} dB ({why}) at {iso_utc(t, 0)[11:19]}Z")
        if scale_db is not None:
            self.log(f"digital scale {float(scale_db):+.1f} dB requested: not adjustable in a session (the generator's knob)")

    # ---- keyer ---------------------------------------------------------------------------------
    def _end_after(self, c) -> None:
        """A stop took effect: `c` is the last chunk keyed (None if none was). Mute the tone
        source from that chunk's end and end the stream after its echo (plus the end
        margin); the run loop then ends there instead of at the schedule's end."""
        n_total = len(self.sched.chunks)
        if c is None:
            now = self.radio.device_time()
            t_mute, t_end = now, now + self.opts.end_margin_s
            self.phase = "stopped before the first chunk"
            self.log(f"stopped before any chunk was keyed ({n_total} planned)")
        else:
            t_mute, t_end = c.tx_stop, c.rx_stop + self.opts.end_margin_s
            self.phase = f"stopping after chunk {c.index + 1} of {n_total}: echo due {iso_utc(c.rx_stop, 0)[11:19]}Z"
            self.log(f"stopping after chunk {c.index + 1} of {n_total}: echo received until {iso_utc(c.rx_stop, 0)}, "
                     f"{n_total - c.index - 1} chunks not sent")
        if self.tone is not None:
            self.tone.stop_after(t_mute, t_end)
        self._t_end_eff = t_end

    def _keyer(self) -> None:
        """Keyer thread: for each chunk, key up at tx_start - T_lead - settle (the
        sequencer's LNA guard, so the RF still starts T_lead after the transmitter is
        keyed) and down at tx_stop + T_lag, on device time; log the key events and any
        key-line fault. Receive-only sessions never key. A stop() request is honored
        between chunks: the chunk in progress completes, the next is not keyed."""
        if not self.opts.tx_enabled:
            self.phase = "receive only (partner transmits)"
            return
        settle = float(getattr(self.keyer, "settle_s", 0.0))     # the sequencer's LNA guard
        prev = None
        for c in self.sched.chunks:
            t_on = c.tx_start - self.opts.t_lead_s - settle
            t_off = c.tx_stop + self.opts.t_lag_s
            if not self._sleep_until(t_on, wake_on_stop=True):
                if not self._abort.is_set():
                    self._end_after(prev)
                return
            self.keyer.key(True)
            if self.keyer.fault:
                self.log(f"KEY LINE FAULT on chunk {c.index + 1}: {self.keyer.fault}")
            self.phase = f"TX chunk {c.index + 1} of {len(self.sched.chunks)}"
            self.report.key_events.append({"t": self.radio.device_time(), "on": True, "chunk": c.index})
            self.report.chunks_keyed += 1
            # Watchdog: whatever happens to this thread (a stalled host, a serial write that
            # blocks), the transmitter is released no later than t_off + WATCHDOG_MARGIN_S.
            # The earlier "watchdog" argument to _sleep_until could never fire (design 7.2
            # promised one; found in the 2026-09-23 review).
            wd = threading.Timer(max(0.1, t_off - self.radio.device_time() + WATCHDOG_MARGIN_S), self._watchdog_release, args=(c.index,))
            wd.daemon = True
            wd.start()
            ok = self._sleep_until(t_off)
            self.keyer.key(False)
            wd.cancel()
            self.phase = f"listening for chunk {c.index + 1} of {len(self.sched.chunks)}"
            self.report.key_events.append({"t": self.radio.device_time(), "on": False, "chunk": c.index})
            if not ok:
                return
            if self._stop.is_set():
                self._end_after(c)
                return
            prev = c

    def _watchdog_release(self, chunk_index: int) -> None:
        """Timer callback: the keyer thread has not released chunk `chunk_index` in time.
        Release the transmitter (the sequencer does TX first, then the LNA) and abort."""
        if self.keyer.keyed:
            self.log(f"KEYER WATCHDOG: chunk {chunk_index + 1} still keyed {WATCHDOG_MARGIN_S:.0f} s past its end; releasing")
            try:
                self.keyer.key(False)
            except Exception:
                pass
            self.abort(f"keyer watchdog: chunk {chunk_index + 1} overran")

    def _sleep_until(self, t_device: float, wake_on_stop: bool = False) -> bool:
        """Sleep on the radio's device clock until t_device (seconds); True when reached,
        False on abort (or, with wake_on_stop, on a stop() request: the wait before a
        chunk's key-up, never the wait for its key-down)."""
        while not self._abort.is_set() and not (wake_on_stop and self._stop.is_set()):
            now = self.radio.device_time()
            if now >= t_device:
                return True
            time.sleep(min(0.05, max(0.001, t_device - now)))
        return False

    # ---- flowgraph ----------------------------------------------------------------------------
    def _doppler_fn(self):
        if self.model is None:
            return None
        f = self.sched.f_dial_hz
        cache: Dict[int, float] = {}

        def fD(t):
            # 1 s resolution (rate <= 0.5 Hz/s): one model evaluation per distinct second,
            # cached, then a vectorized gather. The per-sample Python loop this replaced cost
            # the Raspberry Pi a third of a core on every Venus run (2026-10-23); the bench,
            # with pre-compensation off, never ran it.
            t = np.atleast_1d(np.asarray(t, dtype=np.float64))
            keys = np.floor(t).astype(np.int64)
            uniq, inv = np.unique(keys, return_inverse=True)
            vals = np.empty(uniq.shape, dtype=np.float64)
            for i, key in enumerate(uniq.tolist()):
                v = cache.get(key)
                if v is None:
                    v = cache[key] = self.model.doppler_hz(float(key), f)
                vals[i] = v
            return vals[inv]
        return fD

    def build(self, t0: float) -> gr.top_block:
        """Assemble the flowgraph for a stream starting at device time t0 (seconds): the
        EveToneSource (comb at f_IF, Doppler pre-compensated from the model when
        tx_precompensate, tx_time-tagged on hardware) into the usrp_sink, and the
        usrp_source through the two-stage decimator (shifting f_IF to DC, 5.3) into the
        EveRxSink, whose per-frame callback runs the frame FFT bank, files the live
        accumulator and calls the listeners. Simulation routes the tone source through
        SimRadio.build_channel instead; receive-only sends the tones into a null sink (to
        keep the frame clock) and starts the receive stream at t0."""
        opts, s, p = self.opts, self.sched, self.p
        tb = gr.top_block(f"eve_session_{s.session_id}")
        fD = self._doppler_fn()
        sim = getattr(self.radio, "sim", False)
        rate = self.radio.tx_rate
        def on_resync(skipped_s: float, now: float) -> None:
            # the transmit stream stalled (underflow) and re-timed itself: say so, with what was lost
            lost = [c.index + 1 for c in s.chunks if c.tx_start < now + gr_blocks.RESYNC_LEAD_S and c.tx_stop > now - skipped_s]
            self.report.tx_resyncs.append({"t": now, "skipped_s": round(float(skipped_s), 3), "chunks": lost})
            self.log(f"TX RESYNC {len(self.report.tx_resyncs)}: the transmit stream was {skipped_s:.1f} s behind the device "
                     f"clock (an underflow); skipped ahead and started a new timed burst at {iso_utc(now + gr_blocks.RESYNC_LEAD_S, 0)}"
                     + (f"; chunk {', '.join(map(str, lost))} partly lost" if lost else "; between chunks, nothing lost"))

        self.tone = gr_blocks.EveToneSource(s, rate, t0, doppler_hz=fD if opts.tx_precompensate else None,
                                            include_if=True, t_end_device=max(c.rx_stop for c in s.chunks) + opts.end_margin_s,
                                            tag_time=not sim, on_resync=on_resync)
        if sim and getattr(self.radio, "resync_test", False):
            self.tone.tag_time = True       # the software bench exercises the resync on the wall clock (tests only)
        windows = [(c.index, c.rx_start, c.rx_stop, c.frame_first) for c in s.chunks]
        f_shift = p.f_if
        self.decim = gr_blocks.make_rx_decimator(p, self.radio.rx_rate, f_shift)
        self.acc = modem.SymbolAccumulator(p, s.frame_map())
        bank = modem.FrameBank(p)
        def on_frame(k, x):
            metric = bank.frame_metric(x)
            if opts.live_decode:
                self.acc.add(k, metric)
            for fn in self.listeners:
                try:
                    fn(k, x, metric)
                except Exception:
                    pass
        meta = {"f_dial_hz": s.f_dial_hz, "f_if_hz": p.f_if, "doppler_applied_on_rx": bool(opts.rx_doppler_removal),
                "tx_precompensated": bool(opts.tx_precompensate), "radio": getattr(self.radio, "status_text", lambda: "B210")()}
        self.sink = gr_blocks.EveRxSink(p, opts.out_dir, s.session_id, windows, self.radio.rx_rate / p.radio_decim,
                                        meta=meta, on_frame=on_frame, t0_if_untagged=t0 if sim else None)
        if sim:
            rtt = self.model.rtt_s(s.t_start) if self.model is not None else 0.0
            rx_in = self.radio.build_channel(tb, self.tone, rtt)
        elif opts.tx_enabled:
            tb.connect(self.tone, self.radio.tx)
            getattr(self.radio, "connect_tx_events", lambda _tb: None)(tb)
            rx_in = self.radio.rx.block
        else:
            tb.connect(self.tone, blocks.null_sink(gr.sizeof_gr_complex))   # keeps the frame clock, sends nothing
            rx_in = self.radio.rx.block
            from gnuradio import uhd
            self.radio.rx.block.set_start_time(uhd.time_spec(t0))
        tb.connect(self.decim.connect(tb, rx_in), self.sink)
        self.tb = tb
        return tb

    def _steer_doppler(self):
        """Receiver-side removal: step the decimator's shift with the model once a second."""
        fD = self._doppler_fn()
        base = self.p.f_if
        while not self._abort.is_set() and not (self.tone is not None and self.tone.done):
            t = self.radio.device_time()
            try:
                self.decim.set_center_freq(base + float(fD(t)[0]))
            except Exception:
                pass
            time.sleep(1.0)

    # ---- run -----------------------------------------------------------------------------------
    def run(self) -> SessionReport:
        """Run the session to its end and return the report. Refuses a failing preflight or
        a schedule whose start is already past on the device clock. Starts the flowgraph
        arm_lead_s before t0 (= t_start - start_margin_s), enters real-time mode when the
        helper exists, keys through the chunk table on the keyer thread, waits for the tone
        source to finish and the last receive window plus end_margin_s to pass, drains the
        sink, then stops. Ctrl-C aborts; abort or a fault still releases the key, closes
        the archive and writes the log (_finish)."""
        probs = self.preflight()
        if probs:
            raise ValueError("preflight failed:\n  " + "\n  ".join(probs))
        s, opts = self.sched, self.opts
        self.report.started_utc = iso_utc(time.time(), 0)
        self.report.radio = getattr(self.radio, "status_text", lambda: "")()
        t0 = s.t_start - opts.start_margin_s
        now = self.radio.device_time()
        if now > s.t_start:
            raise ValueError(f"schedule starts in the past (device time {iso_utc(now, 0)}, start {iso_utc(s.t_start, 0)})")
        self.log(f"session {s.session_id}: {len(s.chunks)} chunks, first at {iso_utc(s.t_start, 0)}, "
                 f"flowgraph at {iso_utc(t0, 0)} (in {t0 - now:.1f} s)")
        if t0 - now > 60.0:          # a timed start hours ahead (the operations schedule): say so on the badge
            self.phase = f"waiting: first chunk at {iso_utc(s.t_start, 0)[11:19]}Z, streams armed {opts.arm_lead_s:.0f} s before"
        tb = self.build(t0)
        rt = None
        if opts.realtime_mode:
            try:
                from ._workbench import import_dses_radio
                rt = import_dses_radio().RealtimeMode()
                rt.begin()
            except Exception:
                rt = None
        keyer = threading.Thread(target=self._keyer, name="eve-keyer", daemon=True)
        steer = threading.Thread(target=self._steer_doppler, name="eve-doppler", daemon=True) if opts.rx_doppler_removal else None
        old_handler = None
        try:
            try:
                old_handler = signal.signal(signal.SIGINT, lambda *_: self.abort("operator interrupt"))
            except Exception:
                pass
            # start the flowgraph before t0 so the timed streams are armed (arm_lead_s)
            self._sleep_until(t0 - opts.arm_lead_s, wake_on_stop=True)
            if self._abort.is_set():
                return self._finish(rt, old_handler)
            if self._stop.is_set():          # stopped while waiting for the start: nothing to send or receive
                self.log("stopped before the streams were armed: nothing transmitted")
                return self._finish(rt, old_handler)
            tb.start()
            self.phase = "armed"
            keyer.start()
            if steer:
                steer.start()
            t_end = max(c.rx_stop for c in s.chunks) + opts.end_margin_s
            while not self._abort.is_set():
                # The simulation is paced by its throttle now, so it waits for the schedule's end
                # like the hardware does; ending on tone.done alone cut the last chunks' keying
                # short (the source runs a buffer's worth ahead of the wall clock). A stop()
                # moves the end to the last keyed chunk's echo (_end_after).
                if self._stop.is_set() and not opts.tx_enabled and self._t_end_eff is None:
                    self._end_after(None)                   # receive only: nothing of ours to wait for
                if self._t_end_eff is not None:
                    t_end = self._t_end_eff
                now = self.radio.device_time()
                if opts.tx_enabled:
                    self.tone.watch(now)        # the transmit stream's lag check (resync after a stall)
                if self.tone.done and now >= t_end:
                    break
                time.sleep(0.2)
            # drain: let the decimator and sink finish the samples already in flight
            last, quiet = -1, 0
            for _ in range(120):
                if self._abort.is_set():
                    break
                n = self.sink.samples_in
                quiet = quiet + 1 if n == last else 0
                last = n
                if quiet >= 3:
                    break
                time.sleep(0.25)
            self.phase = "draining"
            tb.stop()
            tb.wait()
        finally:
            self.phase = "finished" if not self._abort.is_set() else "aborted"
            self.report = self._finish(rt, old_handler)
        return self.report

    def _finish(self, rt, old_handler) -> SessionReport:
        try:
            self.keyer.key(False)
        except Exception:
            pass
        if self.sink is not None:
            self.report.rx = self.sink.close()
        if self.tone is not None:
            self.report.tone_samples_on = int(self.tone.samples_on)
            self.report.frames_sent = len(self.tone.frames_sent)
        ev = dict(getattr(self.radio, "tx_event_counts", lambda: {})() or {})
        if self.tone is not None and getattr(self.tone, "resyncs", 0):      # the generator's source has none
            ev["resync"] = int(self.tone.resyncs)
            ev["resync_skipped_s"] = round(float(self.tone.resync_skipped_s), 1)
        if ev:
            self.report.tx_events = ev
            from .report import tx_events_text
            warn = tx_events_text(ev)
            if warn:
                self.report.notes.append(warn)
                self.log("WARNING: " + warn)
        if self.acc is not None and self.opts.live_decode and self.acc.acc:
            out = self.acc.decode()
            self.report.live_decode = {"ok": out.ok, "text": out.text, "symbols": out.symbols,
                                       "bits_corrected": out.bits_corrected, "frames": self.acc.frames_seen,
                                       "repetitions": self.acc.repetitions}
        if rt is not None:
            rt.end()
        if old_handler is not None:
            try:
                signal.signal(signal.SIGINT, old_handler)
            except Exception:
                pass
        self.report.finished_utc = iso_utc(time.time(), 0)
        self.write_log()
        return self.report

    def release(self) -> None:
        """Drop the flowgraph and every GNU Radio block so the radio's streamers die now,
        not at some later garbage collection. A second uhd.usrp_source for the same
        B210 while the first one is still alive crashed the process (access violation
        in the constructor, 2026-09-12); the application calls this before closing the
        radio and opening it again for the next run."""
        import gc
        tb = self.tb
        if tb is not None:
            try:
                tb.stop()
                tb.wait()
            except Exception:
                pass
            try:
                tb.disconnect_all()
            except Exception:
                pass
        self.tb = None
        self.tone = None
        self.sink = None
        self.decim = None
        self.listeners = []
        gc.collect()

    def write_log(self) -> Path:
        """Write <out_dir>/<session_id>_session.json: the report plus the schedule, the
        options and the software version (8.4). Returns the path."""
        path = Path(self.opts.out_dir) / f"{self.sched.session_id}_session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self.report)
        d["schedule"] = self.sched.to_dict()
        d["options"] = asdict(self.opts)
        d["generated_by"] = f"dses-eve {__version__}"
        path.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        return path
