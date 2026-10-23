"""The two GNU Radio blocks that touch the radio (design document 5.1 to 5.3).

EveToneSource   schedule-driven NCO source at the radio rate. Sample n of the stream is
                device time t_n = t0 + n / fs; the block maps t_n to the schedule's frame
                (through the chunk table), synthesizes the tone for that frame with the
                phase-continuous ToneSynthesizer, and emits zeros outside on-windows. The
                first sample carries a `tx_time` tag = t0, so the usrp_sink holds the
                stream until exactly that device time: the sample clock IS the schedule
                clock. Doppler pre-compensation is a frequency function of device time.

EveRxSink       archive writer at the modem rate (the flowgraph's freq_xlating decimator
                brings the radio stream down by 32 and removes f_IF plus, if the receiver
                is not the pre-compensated one, the bulk Doppler). One file per receive
                window: `<session_id>_<chunk>.eve.iq` complex64 + `.json` sidecar (8.2).
                The FilterbankSink pattern: rx_time tags time-stamp the stream, gaps after
                overflows are measured from the tags and zero-padded so the sample clock
                stays honest, a deep queue and a writer thread keep the GR thread light,
                and close() reports what happened. A live callback per frame feeds the
                operator display.

make_rx_decimator()  the freq_xlating_fir_filter that sits between the radio and the sink.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pmt
from gnuradio import gr, filter as gr_filter
from gnuradio.filter import firdes
from gnuradio.fft import window as fft_window

from .params import EveParams
from .modem import ToneSynthesizer, FrameMap, OFF
from .doppler import iso_utc


# ---------------------------------------------------------------------------------------
# transmit
# ---------------------------------------------------------------------------------------
class ScheduleFrameMap(FrameMap):
    """FrameMap driven by device time: maps device times to the schedule frame transmitted
    then (or OFF), through the chunk table."""

    def __init__(self, schedule):
        super().__init__(schedule.symbols, schedule.params, on_windows=schedule.on_windows(),
                         pilot=schedule.pilot_enabled, repeat_count=schedule.repeat_count)
        self.sched = schedule
        self._starts = np.array([c.tx_start for c in schedule.chunks])
        self._stops = np.array([c.tx_stop for c in schedule.chunks])
        self._first = np.array([c.frame_first for c in schedule.chunks])
        self._last = np.array([c.frame_last for c in schedule.chunks])

    def frame_at_time(self, t: np.ndarray) -> np.ndarray:
        """Frame index for device times t, or OFF where nothing is scheduled."""
        t = np.asarray(t, dtype=np.float64)
        idx = np.searchsorted(self._starts, t, side="right") - 1
        k = np.full(t.shape, OFF, dtype=np.int64)
        ok = idx >= 0
        if ok.any():
            where = np.nonzero(ok)[0]
            i = idx[ok]
            kk = self._first[i] + np.floor((t[ok] - self._starts[i]) * self.p.r_bw + 1e-9).astype(np.int64)
            inside = (t[ok] < self._stops[i] - 1e-9) & (kk <= self._last[i])
            k[where[inside]] = kk[inside]
        return k

    def tones_at_time(self, t: np.ndarray) -> np.ndarray:
        """Tone index (0..M-1, the pilot tone, or OFF) transmitted at each device time t,
        looked up once per distinct frame through FrameMap.tone (ORI's frame-to-symbol
        rule, design document 6.4)."""
        k = self.frame_at_time(t)
        tones = np.full(k.shape, OFF, dtype=np.int64)
        on = k != OFF
        if on.any():
            lut = {int(u): self.tone(int(u)) for u in np.unique(k[on])}
            tones[on] = np.array([lut[int(u)] for u in k[on]], dtype=np.int64)
        return tones


RESYNC_LATE_S = 0.25      # samples this far behind device time = the transmit stream has stalled
RESYNC_LEAD_S = 1.0       # the new burst starts this far ahead of device time
RESYNC_CHECK_BLOCKS = 4   # how often the device clock is read (every 4th block, about 3 times a second)


class EveToneSource(gr.sync_block):
    """Schedule-driven tone source at the radio rate (see module docstring).

    RESYNC AFTER A STALL (2026-10-23, D26). The stream is one timed burst: the first sample
    carries tx_time = t0 and gr-uhd stamps every later packet from the sample count. When
    the host falls behind (an underflow: a CPU stall, a throttled Raspberry Pi), every
    packet after it carries a time already past, the radio drops each one as LATE, and
    nothing we send reaches the air again for the rest of the run (Haswell, 2026-10-20
    and 2026-10-23). With `device_time` given, work() compares the device clock with the
    time of the sample it is about to generate every RESYNC_CHECK_BLOCKS blocks; when the
    stream is RESYNC_LATE_S behind, it ends the burst (tx_eob on a short block, dropped
    like the rest), skips the backlog, and begins a new burst RESYNC_LEAD_S ahead of the
    clock (tx_sob + tx_time on its first sample). The schedule's frames in the skipped
    seconds are lost; the rest of the run goes out on time. `resyncs` and
    `resync_skipped_s` count it; `on_resync(skipped_s, device_now)` is called each time."""

    def __init__(self, schedule, fs: float, t0_device: float,
                 doppler_hz: Optional[Callable[[np.ndarray], np.ndarray]] = None,
                 amplitude: Optional[float] = None, include_if: bool = True,
                 t_end_device: Optional[float] = None, block_len: int = 65536, tag_time: bool = True,
                 device_time: Optional[Callable[[], float]] = None,
                 on_resync: Optional[Callable[[float, float], None]] = None):
        gr.sync_block.__init__(self, name="eve_tone_source", in_sig=None, out_sig=[np.complex64])
        self.sched = schedule
        self.p = schedule.params
        self.fs = float(fs)
        self.t0 = float(t0_device)
        self.t_end = float(t_end_device) if t_end_device is not None else max(c.tx_stop for c in schedule.chunks) + 1.0
        self.map = ScheduleFrameMap(schedule)
        f_extra = None if doppler_hz is None else (lambda t: -np.asarray(doppler_hz(t + self.t0), dtype=np.float64))
        self.synth = ToneSynthesizer(self.map, fs, self.p, f_extra=f_extra, amplitude=amplitude, include_if=include_if)
        self.n_total = int(np.ceil((self.t_end - self.t0) * self.fs))
        self.n_out = 0
        self.samples_on = 0
        self.frames_sent: set = set()
        self.done = False
        self.tag_time = tag_time
        self._block_len = block_len
        self._mute_from: Optional[float] = None
        self.device_time = device_time
        self.on_resync = on_resync
        self.resyncs = 0
        self.resync_skipped_s = 0.0
        self._burst_start = True        # the next block's first sample gets the tx_time tag
        self._resync_pending = False    # the burst was ended: the next block starts a new one
        self._blocks = 0
        self.set_min_output_buffer(1 << 22)     # deep TX edge (self-test lesson 2026-08-05)

    def _lag_s(self) -> float:
        """How far the sample about to be generated lies BEHIND the device clock (seconds;
        negative = the normal state, the stream runs ahead by the buffer depth)."""
        return float(self.device_time()) - (self.t0 + self.n_out / self.fs)

    def _resync(self) -> bool:
        """Begin the new burst after a stall: skip to RESYNC_LEAD_S ahead of the device clock.
        Returns False when that is past the end of the stream (nothing left to send)."""
        now = float(self.device_time())
        n_new = int(np.ceil((now + RESYNC_LEAD_S - self.t0) * self.fs))
        skipped = (n_new - self.n_out) / self.fs
        self.resyncs += 1
        self.resync_skipped_s += max(0.0, skipped)
        self._resync_pending = False
        if n_new >= self.n_total:
            self.n_out = self.n_total
            if self.on_resync:
                self.on_resync(skipped, now)
            return False
        self.n_out = n_new
        self._burst_start = True
        if self.on_resync:
            self.on_resync(skipped, now)
        return True

    def stop_after(self, t_mute_device: float, t_end_device: float) -> None:
        """A clean stop (Session.stop): no tone after device time t_mute_device (the frames
        of the chunks that will not be keyed become silence, so no RF reaches the driver
        while the transmitter is unkeyed) and the stream ends at t_end_device (the last
        kept chunk's echo plus the end margin). Called from the keyer thread while work()
        runs; the counters are read once per block, so the stream ends within one block of
        the new end."""
        self._mute_from = float(t_mute_device)
        self.t_end = float(t_end_device)
        self.n_total = min(self.n_total, int(np.ceil((self.t_end - self.t0) * self.fs)))

    def work(self, input_items, output_items):
        """GNU Radio work: emit up to block_len samples for device times t0 + n / fs, tag
        the very first sample with tx_time = t0 (when tag_time) and the last with tx_eob,
        count samples_on and frames_sent, and return -1 once t_end_device is reached."""
        out = output_items[0]
        if self.done:
            return -1
        if self._resync_pending and not self._resync():
            self.done = True
            return -1
        n = min(len(out), self._block_len, self.n_total - self.n_out)
        if n <= 0:
            self.done = True
            return -1
        self._blocks += 1
        if (self.device_time is not None and self.tag_time and not self._burst_start
                and self._blocks % RESYNC_CHECK_BLOCKS == 0):
            try:
                lag = self._lag_s()
            except Exception:       # noqa: BLE001  (the radio refused the time query: try again next time)
                lag = 0.0
            if lag > RESYNC_LATE_S:
                # the stream is behind: close this burst with a short silent block (it is late
                # and dropped like the rest) and begin a new one on the next call
                n = min(n, 64)
                out[:n] = 0
                self.add_item_tag(0, self.nitems_written(0) + n - 1, pmt.intern("tx_eob"), pmt.PMT_T)
                self.n_out += n
                self._resync_pending = True
                return n
        n_idx = np.arange(self.n_out, self.n_out + n, dtype=np.float64)
        tones = self.map.tones_at_time(self.t0 + n_idx / self.fs)
        if self._mute_from is not None:
            tones[self.t0 + n_idx / self.fs >= self._mute_from] = OFF
        out[:n] = self.synth.generate_with_tones(n_idx, tones)
        if self._burst_start and self.tag_time:
            t_first = self.t0 + self.n_out / self.fs
            secs = int(np.floor(t_first))
            if self.n_out > 0:
                self.add_item_tag(0, self.nitems_written(0), pmt.intern("tx_sob"), pmt.PMT_T)
            self.add_item_tag(0, self.nitems_written(0), pmt.intern("tx_time"),
                              pmt.make_tuple(pmt.from_uint64(secs), pmt.from_double(t_first - secs)))
        self._burst_start = False
        on = tones != OFF
        self.n_out += n
        self.samples_on += int(on.sum())
        if on.any():
            self.frames_sent.update(int(u) for u in np.unique(self.map.frame_at_time(self.t0 + n_idx[on] / self.fs)))
        if self.n_out >= self.n_total:
            self.add_item_tag(0, self.nitems_written(0) + n - 1, pmt.intern("tx_eob"), pmt.PMT_T)
            self.done = True
        return n


# ---------------------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------------------
class RxDecimator:
    """Two-stage radio-to-modem-rate front end (5.3).

    Stage 1: freq_xlating_fir_filter_ccc, decimate by decim/2 with a loose complex
    band-pass around the one-sided comb, shifting by f_shift_hz (f_IF, plus the bulk
    Doppler when this receiver is not the pre-compensated one) so tone 0 lands at DC.
    Stage 2: a sharp half-rate low-pass at the intermediate rate, decimate by 2, passband
    flat to 0.995 x BW. The comb fills the modem-rate Nyquist band exactly (BW = fs/2 by
    construction: M x 2 R_bw = N_fft R_bw), so the last few tones sit in the transition
    band and take alias noise; everything below ~23.4 kHz is flat. set_center_freq()
    steps stage 1 at run time to follow the Doppler model.
    """

    def __init__(self, params: EveParams, radio_rate: float, f_shift_hz: float, decim: Optional[int] = None):
        decim = params.radio_decim if decim is None else decim
        if decim % 2:
            raise ValueError("decimation must be even (two stages)")
        modem_rate = radio_rate / decim
        mid_rate = radio_rate / (decim // 2)
        bw = params.bandwidth
        taps1 = firdes.complex_band_pass(1.0, radio_rate, -0.1 * modem_rate, bw + 0.1 * modem_rate,
                                         0.25 * modem_rate, fft_window.WIN_HAMMING)
        self.stage1 = gr_filter.freq_xlating_fir_filter_ccc(decim // 2, taps1, f_shift_hz, radio_rate)
        pass_edge = 0.995 * bw
        trans = 2.0 * (0.5 * modem_rate - pass_edge)          # transition centered on Nyquist
        taps2 = firdes.low_pass(1.0, mid_rate, pass_edge + 0.5 * trans, trans, fft_window.WIN_HAMMING)
        self.stage2 = gr_filter.fir_filter_ccf(2, taps2)
        self.n_taps = (len(taps1), len(taps2))
        self.group_delay_s = 0.5 * (len(taps1) / radio_rate + len(taps2) / mid_rate)
        self.f_shift_hz = f_shift_hz

    def connect(self, tb, src):
        """Connect src -> stage 1 -> stage 2 in top block tb; returns stage 2, the block
        the sink connects to."""
        tb.connect(src, self.stage1)
        tb.connect(self.stage1, self.stage2)
        return self.stage2

    def set_center_freq(self, f_hz: float):
        """Re-center stage 1 on f_hz (Hz above the radio's dial frequency) at run time:
        f_IF plus the model Doppler when this receiver removes it (design document 6.5)."""
        self.f_shift_hz = f_hz
        self.stage1.set_center_freq(f_hz)


def make_rx_decimator(params: EveParams, radio_rate: float, f_shift_hz: float, decim: Optional[int] = None):
    """Build the RxDecimator for a radio stream at radio_rate S/s, shifting f_shift_hz (Hz)
    to DC and decimating by decim (default params.radio_decim, 32) to the modem rate."""
    return RxDecimator(params, radio_rate, f_shift_hz, decim)


def _parse_rx_time(value) -> Optional[float]:
    """Device time in seconds from an rx_time tag value (a (uint64 seconds, double
    fraction) tuple), or None if the value is not one."""
    try:
        if pmt.is_tuple(value) and pmt.length(value) >= 2:
            return pmt.to_uint64(pmt.tuple_ref(value, 0)) + pmt.to_double(pmt.tuple_ref(value, 1))
    except Exception:
        pass
    return None


class EveRxSink(gr.sync_block):
    """Archive writer at the modem rate; one file per receive window (8.2)."""

    GAP_MIN_SECONDS = 1e-4
    MAX_QUEUE_SAMPLES = 1 << 24          # 16 Mi complex64 = 128 MB, ~6 min at the modem rate
    BACKPRESSURE_WAIT_S = 0.25

    def __init__(self, params: EveParams, out_dir, session_id: str,
                 windows: Sequence[Tuple[int, float, float, int]], fs: float,
                 meta: Optional[Dict] = None, on_frame: Optional[Callable[[int, np.ndarray], None]] = None,
                 t0_if_untagged: Optional[float] = None, comb_center_offset_hz: float = 0.0):
        """windows: (chunk_index, rx_start, rx_stop, frame_first) in device time, per
        receive window; samples outside every window are dropped. meta: extra sidecar
        fields. t0_if_untagged: device time of the first sample when the stream carries no
        rx_time tag (software bench). comb_center_offset_hz: where tone 0 sits in the
        archived stream (the decimator centers the comb; the offline decoder shifts it
        back to baseband) - recorded in the sidecar."""
        gr.sync_block.__init__(self, name="eve_rx_sink", in_sig=[np.complex64], out_sig=None)
        self.p = params
        self.fs = float(fs)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.windows = sorted(windows, key=lambda w: w[1])
        self.meta = dict(meta or {})
        self.on_frame = on_frame
        self.comb_center_offset_hz = float(comb_center_offset_hz)
        self._rx_time_key = pmt.intern("rx_time")
        self._time_ref: Optional[Tuple[int, float]] = None    # (abs input index, device seconds)
        self._t0_untagged = t0_if_untagged
        self.gap_events = 0
        self.gap_samples = 0
        self.time_tags = 0
        self.samples_in = 0
        self.samples_written = 0
        self.first_time: Optional[float] = None
        self._events: List[Dict] = []
        self._q: List = []
        self._q_samples = 0
        self._qlock = threading.Lock()
        self._stopping = threading.Event()
        self._closed = False
        self._t_next: Optional[float] = None
        self._files: Dict[int, Dict] = {}
        self._frame_buf = np.zeros(0, dtype=np.complex64)
        self._frame_k: Optional[int] = None
        self._frame_win: Optional[int] = None
        self._worker = threading.Thread(target=self._worker_loop, name="eve-rx-sink", daemon=True)
        self._worker.start()

    # ---- GR thread ------------------------------------------------------------------------
    def work(self, input_items, output_items):
        """GNU Radio work (GR thread): split the input at its rx_time tags and queue the
        data between them for the writer thread. Each tag's time is compared with the time
        the sample count predicts from the previous tag; a difference above
        GAP_MIN_SECONDS is an overflow and that many zero samples are queued in its place,
        so the archive's sample clock stays honest. Without tags (the software bench) the
        first sample is placed at t0_if_untagged."""
        x = input_items[0]
        n = len(x)
        n0 = self.nitems_read(0)
        if self._time_ref is None and self._t0_untagged is not None and self.samples_in == 0:
            tags0 = self.get_tags_in_window(0, 0, n, self._rx_time_key)
            if not tags0:
                self._time_ref = (int(n0), float(self._t0_untagged))
                self.first_time = float(self._t0_untagged)
                with self._qlock:
                    self._q.append(("time", float(self._t0_untagged)))
        tags = self.get_tags_in_window(0, 0, n, self._rx_time_key)
        pos = 0
        for tag in sorted(tags, key=lambda t: t.offset):
            t = _parse_rx_time(tag.value)
            if t is None:
                continue
            rel = int(tag.offset - n0)
            if rel > pos:
                self._enqueue(x[pos:rel])
            if self._time_ref is not None:
                expected = self._time_ref[1] + (tag.offset - self._time_ref[0]) / self.fs
                gap = t - expected
                if gap > self.GAP_MIN_SECONDS:
                    npad = int(round(gap * self.fs))
                    self.gap_events += 1
                    self.gap_samples += npad
                    self._events.append({"at_device_time": t, "gap_s": gap, "padded_samples": npad})
                    with self._qlock:
                        self._q.append(("pad", npad))
            else:
                self.first_time = t
            self.time_tags += 1
            self._time_ref = (int(tag.offset), t)
            with self._qlock:
                self._q.append(("time", t))
            pos = rel
        if pos < n:
            self._enqueue(x[pos:])
        self.samples_in += n
        return n

    def _enqueue(self, arr):
        """Queue a data block for the writer thread. If the queue stays full for
        BACKPRESSURE_WAIT_S the block is replaced by zeros of the same length and counted
        as a gap, rather than stalling the radio."""
        n = len(arr)
        if n == 0:
            return
        deadline = time.monotonic() + self.BACKPRESSURE_WAIT_S
        while True:
            with self._qlock:
                if self._q_samples + n <= self.MAX_QUEUE_SAMPLES:
                    self._q.append(("data", np.array(arr, dtype=np.complex64)))
                    self._q_samples += n
                    return
            if time.monotonic() >= deadline or self._stopping.is_set():
                break
            time.sleep(0.002)
        with self._qlock:
            self._q.append(("pad", n))
            self.gap_events += 1
            self.gap_samples += n

    # ---- worker ---------------------------------------------------------------------------
    def _worker_loop(self):
        while True:
            item = None
            with self._qlock:
                if self._q:
                    item = self._q.pop(0)
                    if item[0] == "data":
                        self._q_samples -= len(item[1])
            if item is None:
                if self._stopping.is_set():
                    break
                time.sleep(0.005)
                continue
            kind = item[0]
            if kind == "time":
                self._t_next = float(item[1])
            elif kind == "data":
                self._consume(item[1])
            else:
                self._consume(np.zeros(int(item[1]), dtype=np.complex64))

    def _window_for(self, t: float):
        for w in self.windows:
            if w[1] <= t < w[2]:
                return w
        return None

    def _consume(self, arr: np.ndarray):
        """Writer thread: place a block on the device-time axis starting at _t_next, write
        the parts that fall inside a receive window to that window's file, drop the rest,
        and advance _t_next by the block length."""
        if self._t_next is None:
            return                      # no timestamp yet: cannot place samples on the schedule
        n = len(arr)
        i = 0
        while i < n:
            t = self._t_next
            w = self._window_for(t)
            if w is None:
                nxt = [x[1] for x in self.windows if x[1] > t]
                j = n if not nxt else min(n, i + max(1, int(np.ceil((nxt[0] - t) * self.fs))))
            else:
                j = min(n, i + max(1, int(np.ceil((w[2] - t) * self.fs))))
                self._write(w, t, arr[i:j])
            self._t_next = t + (j - i) / self.fs
            i = j

    def _write(self, w, t: float, arr: np.ndarray):
        ci = w[0]
        f = self._files.get(ci)
        if f is None:
            path = self.out_dir / f"{self.session_id}_{ci:02d}.eve.iq"
            f = self._files[ci] = {"fh": open(path, "wb"), "path": path, "n": 0, "t_first": t,
                                   "frame_first": w[3], "rx_start": w[1], "rx_stop": w[2]}
        arr.astype(np.complex64).tofile(f["fh"])
        f["n"] += len(arr)
        self.samples_written += len(arr)
        if self.on_frame is not None:
            self._feed_frames(w, t, arr)

    def _feed_frames(self, w, t: float, arr: np.ndarray):
        nfft = self.p.n_fft
        if self._frame_win != w[0]:
            self._flush_partial_frame()
            self._frame_buf = np.zeros(0, dtype=np.complex64)
            self._frame_k = w[3] + int(np.floor((t - w[1]) * self.p.r_bw + 1e-9))
            self._frame_win = w[0]
        buf = np.concatenate([self._frame_buf, arr])
        nf = buf.size // nfft
        for i in range(nf):
            try:
                self.on_frame(self._frame_k, buf[i * nfft:(i + 1) * nfft])
            except Exception:
                pass
            self._frame_k += 1
        self._frame_buf = buf[nf * nfft:]

    def _flush_partial_frame(self):
        """A window ends on a device-time boundary and can leave its last frame a few
        samples short; give the live view that frame (zero-padded) rather than drop it."""
        nfft = self.p.n_fft
        buf = getattr(self, "_frame_buf", None)
        if buf is not None and buf.size >= 0.9 * nfft and self.on_frame is not None:
            frame = np.concatenate([buf, np.zeros(nfft - buf.size, dtype=np.complex64)])
            try:
                self.on_frame(self._frame_k, frame)
            except Exception:
                pass
            self._frame_k += 1
        self._frame_buf = np.zeros(0, dtype=np.complex64)

    # ---- shutdown -------------------------------------------------------------------------
    def stop(self):
        """GNU Radio stop hook: closes the archive (files and sidecars) when the flowgraph stops."""
        self.close()
        return True

    def close(self) -> Dict:
        """Stop the writer thread, flush the last partial frame to the live view, close
        every window file and write its JSON sidecar (schema dses-eve-archive/1, design
        document 8.2: first-sample time as device time and UTC, the receive window, the
        first frame number and the first sample's offset into it in frames, the gap list,
        the waveform parameters and the meta fields). Returns report(). Safe to call twice."""
        if self._closed:
            return self.report()
        self._closed = True
        self._stopping.set()
        self._worker.join(timeout=60.0)
        try:
            self._flush_partial_frame()
        except Exception:
            pass
        for ci, f in self._files.items():
            f["fh"].close()
            side = {
                "schema": "dses-eve-archive/1", "session_id": self.session_id, "chunk": ci,
                "path": f["path"].name, "sample_rate": self.fs, "datatype": "cf32_le",
                "n_samples": f["n"], "start_device_time": f["t_first"], "start_utc": iso_utc(f["t_first"]),
                "rx_window_utc": [iso_utc(f["rx_start"]), iso_utc(f["rx_stop"])],
                "frame_first": f["frame_first"],
                "first_sample_frame_offset": (f["t_first"] - f["rx_start"]) * self.p.r_bw,
                "comb_center_offset_hz": self.comb_center_offset_hz,
                "gap_events": self.gap_events, "gap_samples": self.gap_samples, "gaps": self._events[:1000],
                "waveform": self.p.to_schedule_dict(), **self.meta,
            }
            Path(str(f["path"])[:-len(".eve.iq")] + ".json").write_text(json.dumps(side, indent=2), encoding="utf-8")
        return self.report()

    def report(self) -> Dict:
        """Counts for the session log: samples in and written, gap events and padded
        samples, rx_time tags seen, the file per chunk index, and the first timestamp."""
        return {"samples_in": self.samples_in, "samples_written": self.samples_written,
                "gap_events": self.gap_events, "gap_samples": self.gap_samples,
                "time_tags": self.time_tags, "files": {ci: str(f["path"]) for ci, f in self._files.items()},
                "first_time": self.first_time}


class TxEventCounter(gr.basic_block):
    """Counts the usrp_sink's asynchronous transmit events. gr-uhd publishes them on the
    sink's "async_msgs" message port as a pair (metadata dict, value dict) whose value
    carries "event_code": a list of symbols among burst_ack, underflow, seq_error,
    time_error, underflow_in_packet, seq_error_in_burst. A time_error is a LATE PACKET:
    timed samples that reached the radio after their slot and were dropped (UHD prints
    'L' for each). Counted here: underflow (both kinds), late (time_error), seq_error
    (both kinds), other (anything unparsable); burst_ack is ignored. The counts feed the
    operator warning (report.tx_events_text) on the Run tab, in the log and in the report.

    Why: on the Haswell Raspberry Pi 5 (2026-10-20) Variant A's 1.5 MS/s comb could not be
    generated in time; symbol 0 decoded from the pre-buffered first chunk and symbols 1-10
    were never on the air, with nothing in the GUI to say so."""

    def __init__(self):
        gr.basic_block.__init__(self, name="eve_tx_events", in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self._on_msg)
        self._lock = threading.Lock()
        self.counts: Dict[str, int] = {"underflow": 0, "late": 0, "seq_error": 0, "other": 0}
        self.last_t: Optional[float] = None

    def _on_msg(self, msg) -> None:
        codes: List[str] = []
        try:
            d = pmt.cdr(msg) if pmt.is_pair(msg) else msg
            if pmt.is_dict(d):
                v = pmt.dict_ref(d, pmt.intern("event_code"), pmt.PMT_NIL)
                if pmt.is_symbol(v):
                    codes = [pmt.symbol_to_string(v)]
                while pmt.is_pair(v):                 # a pmt list: walk car/cdr (to_python
                    x = pmt.car(v)                    # would hand back nested tuples)
                    if pmt.is_symbol(x):
                        codes.append(pmt.symbol_to_string(x))
                    v = pmt.cdr(v)
        except Exception:        # noqa: BLE001
            codes = []
        with self._lock:
            if not codes:
                self.counts["other"] += 1
            for c in codes:
                if c in ("underflow", "underflow_in_packet"):
                    self.counts["underflow"] += 1
                elif c == "time_error":
                    self.counts["late"] += 1
                elif c in ("seq_error", "seq_error_in_burst"):
                    self.counts["seq_error"] += 1
                elif c != "burst_ack":
                    self.counts["other"] += 1
            self.last_t = time.time()

    def snapshot(self) -> Dict[str, int]:
        """A copy of the counts (thread-safe)."""
        with self._lock:
            return dict(self.counts)
