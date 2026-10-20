"""The session schedule: the block-slot timeline and the JSON contract with partner
stations (design document 4.1, 4.2, 6.4, 8.1).

Frame numbering. Frame k is the k-th TRANSMITTED frame of the session: it carries symbol
floor(k / N_frames) mod N_sym of repetition floor(k / (N_frames x N_sym)). The chunk
table maps frame ranges to UTC: chunk i transmits frames [frame_first, frame_last] from
tx_start_utc, so frame k of chunk i starts at tx_start_i + (k - frame_first_i) x T_frame.
A continuous (bistatic) session is a single chunk. The echo of chunk i is expected at
rx_start_utc = tx_start + RTT(tx_start) .. rx_stop_utc = tx_stop + RTT(tx_stop).

Monostatic chunking (4.2): T_on <= min(RTT - guard, t_on_max), and the next chunk starts
only after the echo is in and the amplifier's off time has elapsed:
tx_start_{i+1} = max(rx_stop_i, tx_stop_i + t_off_min).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from . import __version__
from .params import EveParams
from .doppler import DopplerModel, Site, DSES_HASWELL, to_unix, iso_utc
from . import modem, message

SCHEMA = "dses-eve-schedule/1"


@dataclass
class Chunk:
    """One row of the chunk table (design document 6.4, 8.1, D18): transmitted frames
    frame_first..frame_last inclusive, sent during [tx_start, tx_stop), whose echo is
    expected during [rx_start, rx_stop). Times are Unix seconds (UTC)."""
    index: int
    frame_first: int
    frame_last: int
    tx_start: float          # unix seconds
    tx_stop: float
    rx_start: float
    rx_stop: float

    @property
    def n_frames(self) -> int:
        """Frames in the chunk (the range is inclusive)."""
        return self.frame_last - self.frame_first + 1

    def to_dict(self) -> Dict:
        """The schedule-file row, times as ISO UTC text."""
        return {"index": self.index, "frame_first": self.frame_first, "frame_last": self.frame_last,
                "tx_start_utc": iso_utc(self.tx_start), "tx_stop_utc": iso_utc(self.tx_stop),
                "rx_start_utc": iso_utc(self.rx_start), "rx_stop_utc": iso_utc(self.rx_stop)}

    @classmethod
    def from_dict(cls, d: Dict) -> "Chunk":
        """A Chunk from a schedule-file row."""
        return cls(int(d["index"]), int(d["frame_first"]), int(d["frame_last"]),
                   to_unix(d["tx_start_utc"]), to_unix(d["tx_stop_utc"]),
                   to_unix(d["rx_start_utc"]), to_unix(d["rx_stop_utc"]))


@dataclass
class Schedule:
    """The session contract of design document 8.1 in memory: who transmits and who
    receives, the waveform (EveParams), the message text and its symbols (packed and
    BCH-encoded by modem.encode_message under ORI's conventions), the repeat count and
    pilot flag, the chunk table, the Doppler source and convention (6.5), and the
    amplifier limits in seconds. `epoch` is frame 0's transmit start in Unix seconds.
    to_dict / from_dict are the JSON contract with a partner station; validate() the
    consistency checks. Frame numbering follows the module docstring (D18)."""
    session_id: str
    target: str
    epoch: float                              # unix seconds, frame 0 transmit start
    transmitter: Site
    receiver_site: Site
    mode: str                                 # monostatic | bistatic_tx | bistatic_rx
    f_dial_hz: float
    params: EveParams
    text: str
    symbols: List[int]
    payload_bits: str
    codeword_bits: str
    repeat_count: int
    pilot_enabled: bool
    chunks: List[Chunk]
    doppler_model_name: str = "horizons"
    doppler_table: str = ""
    doppler_convention: str = "tx_precompensated_for_receiver"
    t_on_max_s: float = 300.0
    t_off_min_s: float = 240.0
    generated_utc: str = ""
    generated_by: str = f"dses-eve {__version__}"
    notes: str = ""

    # ---- frame arithmetic ------------------------------------------------------------
    @property
    def n_frames_total(self) -> int:
        """Frames the chunk table covers (last frame number + 1)."""
        return self.chunks[-1].frame_last + 1 if self.chunks else 0

    @property
    def n_frames_wanted(self) -> int:
        """Frames for repeat_count whole messages (repeat_count x N_frames x N_sym)."""
        return self.repeat_count * self.params.n_frames_msg

    def chunk_of_frame(self, k: int) -> Optional[Chunk]:
        """The chunk carrying frame k, or None."""
        for c in self.chunks:
            if c.frame_first <= k <= c.frame_last:
                return c
        return None

    def frame_tx_time(self, k: int) -> float:
        """Transmit start of frame k in Unix seconds: its chunk's tx_start + (k -
        frame_first) x T_frame (6.4). ValueError if no chunk carries k."""
        c = self.chunk_of_frame(k)
        if c is None:
            raise ValueError(f"frame {k} is not in any chunk")
        return c.tx_start + (k - c.frame_first) * self.params.t_frame

    def on_windows(self) -> List[Tuple[int, int]]:
        """(frame_first, frame_last) per chunk: the FrameMap's on-window list."""
        return [(c.frame_first, c.frame_last) for c in self.chunks]

    def frame_map(self) -> modem.FrameMap:
        """The modem.FrameMap for this schedule (symbols, on-windows, pilot, repeat count):
        frame number to tone, including the pilot frames at each chunk start."""
        return modem.FrameMap(self.symbols, self.params, on_windows=self.on_windows(),
                              pilot=self.pilot_enabled, repeat_count=self.repeat_count)

    @property
    def t_start(self) -> float:
        """The first chunk's transmit start, Unix seconds."""
        return self.chunks[0].tx_start

    @property
    def t_end(self) -> float:
        """End of the last chunk's receive window (or of its transmission, if later),
        Unix seconds."""
        return max(self.chunks[-1].rx_stop, self.chunks[-1].tx_stop)

    def messages_complete(self) -> int:
        """How many whole message repetitions the chunk table covers."""
        return self.n_frames_total // self.params.n_frames_msg

    # ---- JSON ------------------------------------------------------------------------
    def to_dict(self) -> Dict:
        """The schedule-file dictionary (schema dses-eve-schedule/1, design document 8.1)."""
        return {
            "schema": SCHEMA,
            "session_id": self.session_id,
            "target": self.target,
            "epoch_utc": iso_utc(self.epoch),
            "transmitter": self.transmitter.to_dict(),
            "receiver": {**self.receiver_site.to_dict(), "mode": self.mode},
            "rf": {"f_dial_hz": self.f_dial_hz, "f_if_hz": self.params.f_if},
            "waveform": self.params.to_schedule_dict(),
            "message": {"text": self.text, "payload_bits": self.payload_bits,
                        "codeword_bits": self.codeword_bits, "symbols": list(self.symbols)},
            "repeat_count": self.repeat_count,
            "pilot": {"enabled": self.pilot_enabled, "n_frames": self.params.pilot_frames,
                      "tone": self.params.pilot_tone},
            "chunks": [c.to_dict() for c in self.chunks],
            "doppler": {"model": self.doppler_model_name, "table": self.doppler_table,
                        "convention": self.doppler_convention},
            "limits": {"t_on_max_s": self.t_on_max_s, "t_off_min_s": self.t_off_min_s},
            "generated_by": self.generated_by,
            "generated_utc": self.generated_utc or iso_utc(datetime.now(timezone.utc), 0),
            "notes": self.notes,
        }

    def to_json(self, path: Optional[Union[str, Path]] = None) -> str:
        """JSON text of to_dict(); also written to `path` when given."""
        s = json.dumps(self.to_dict(), indent=2)
        if path:
            Path(path).write_text(s + "\n", encoding="utf-8")
        return s

    @classmethod
    def from_dict(cls, d: Dict) -> "Schedule":
        """Rebuild a Schedule from a schedule-file dictionary and validate it. The waveform
        block's f_if_hz falls back to rf.f_if_hz; the pilot block overrides the waveform's
        pilot frames and tone; a receiver entry without coordinates means the transmitter's
        site. ValueError on another schema or a failed validation."""
        if d.get("schema") != SCHEMA:
            raise ValueError(f"unsupported schedule schema {d.get('schema')!r}")
        wf = dict(d["waveform"])
        wf.setdefault("f_if_hz", d.get("rf", {}).get("f_if_hz", 25000.0))
        p = EveParams.from_schedule_dict(wf)
        pilot = d.get("pilot", {})
        if pilot:
            from dataclasses import replace
            p = replace(p, pilot_frames=int(pilot.get("n_frames", p.pilot_frames)),
                        pilot_tone=int(pilot.get("tone", p.pilot_tone)))
        msg = d["message"]
        rx = dict(d["receiver"])
        mode = rx.pop("mode", "monostatic")
        tx_site = Site.from_dict(d["transmitter"])
        rx_site = Site.from_dict(rx) if "lat" in rx else tx_site
        sched = cls(
            session_id=d["session_id"], target=d["target"], epoch=to_unix(d["epoch_utc"]),
            transmitter=tx_site, receiver_site=rx_site, mode=mode,
            f_dial_hz=float(d["rf"]["f_dial_hz"]), params=p, text=msg["text"],
            symbols=[int(s) for s in msg["symbols"]], payload_bits=msg.get("payload_bits", ""),
            codeword_bits=msg.get("codeword_bits", ""), repeat_count=int(d.get("repeat_count", 1)),
            pilot_enabled=bool(pilot.get("enabled", False)),
            chunks=[Chunk.from_dict(c) for c in d["chunks"]],
            doppler_model_name=d.get("doppler", {}).get("model", ""),
            doppler_table=d.get("doppler", {}).get("table", ""),
            doppler_convention=d.get("doppler", {}).get("convention", "tx_precompensated_for_receiver"),
            t_on_max_s=float(d.get("limits", {}).get("t_on_max_s", 300)),
            t_off_min_s=float(d.get("limits", {}).get("t_off_min_s", 240)),
            generated_utc=d.get("generated_utc", ""), generated_by=d.get("generated_by", ""),
            notes=d.get("notes", ""))
        sched.validate()
        return sched

    @classmethod
    def from_json(cls, src: Union[str, Path]) -> "Schedule":
        """From a JSON file path, or from the JSON text itself when no such file exists."""
        s = str(src)
        if s.lstrip().startswith("{"):
            return cls.from_dict(json.loads(s))     # the JSON text itself
        try:
            is_file = Path(s).exists()
        except (OSError, ValueError):               # macOS/Linux: a long text is "File name too long"
            is_file = False
        txt = Path(s).read_text(encoding="utf-8") if is_file else s
        return cls.from_dict(json.loads(txt))

    # ---- checks ------------------------------------------------------------------------
    def validate(self) -> None:
        """Raise ValueError if the schedule contradicts itself or the limits (7.2)."""
        p = self.params
        _, cw, syms = modem.encode_message(self.text, p)
        if syms != list(self.symbols):
            raise ValueError("schedule symbols do not match the message text under the waveform parameters")
        k_next = 0
        prev: Optional[Chunk] = None
        for c in self.chunks:
            if c.frame_first != k_next:
                raise ValueError(f"chunk {c.index}: frames must be contiguous (expected first {k_next})")
            k_next = c.frame_last + 1
            t_on = c.tx_stop - c.tx_start
            if abs(t_on - c.n_frames * p.t_frame) > 0.5 * p.t_frame:
                raise ValueError(f"chunk {c.index}: tx window {t_on:.3f} s does not match {c.n_frames} frames")
            if t_on > self.t_on_max_s + 1e-6:
                raise ValueError(f"chunk {c.index}: {t_on:.1f} s on exceeds t_on_max {self.t_on_max_s}")
            if self.mode == "monostatic":
                if c.rx_start < c.tx_stop - 1e-6:
                    raise ValueError(f"chunk {c.index}: echo returns before transmission ends")
                if prev is not None:
                    if c.tx_start < prev.rx_stop - 1e-6:
                        raise ValueError(f"chunk {c.index}: transmits while the previous echo is arriving")
                    if c.tx_start - prev.tx_stop < self.t_off_min_s - 1e-6:
                        raise ValueError(f"chunk {c.index}: off time shorter than t_off_min")
            prev = c


def plan_chunks(model: DopplerModel, params: EveParams, t_start: float, n_frames_wanted: int,
                mode: str = "monostatic", t_on_max_s: float = 300.0, t_off_min_s: float = 240.0,
                chunk_s: Optional[float] = 240.0, rtt_guard_s: float = 30.0,
                t_stop: Optional[float] = None) -> List[Chunk]:
    """Lay chunks on frame boundaries from t_start until n_frames_wanted frames are
    covered (or t_stop is reached). Monostatic: chunk <= min(chunk_s, t_on_max,
    RTT - guard); next chunk after the echo and the amplifier's off time. Bistatic:
    chunks of chunk_s / t_on_max separated by t_off_min (the partner listens
    throughout, so no echo wait; a schedule with t_on_max = inf is one chunk)."""
    chunks: List[Chunk] = []
    k = 0
    t = float(t_start)
    i = 0
    if mode != "monostatic":
        # the partner listens throughout; only the amplifier duty limits chunk the transmission
        while k < n_frames_wanted:
            t_on = t_on_max_s if chunk_s is None else min(chunk_s, t_on_max_s)
            n = min(int(math.floor(t_on * params.r_bw + 1e-9)), n_frames_wanted - k)
            if n <= 0:
                raise ValueError("chunk would hold no frames")
            tx_stop = t + n * params.t_frame
            rx_stop = tx_stop + model.rtt_s(tx_stop)
            if t_stop is not None and rx_stop > t_stop:
                break
            chunks.append(Chunk(i, k, k + n - 1, t, tx_stop, t + model.rtt_s(t), rx_stop))
            k += n
            i += 1
            t = tx_stop + t_off_min_s
        return chunks
    while k < n_frames_wanted:
        rtt = model.rtt_s(t)
        t_on = min(t_on_max_s, rtt - rtt_guard_s)
        if chunk_s is not None:
            t_on = min(t_on, chunk_s)
        n = int(math.floor(t_on * params.r_bw + 1e-9))
        n = min(n, n_frames_wanted - k)
        if n <= 0:
            raise ValueError("chunk would hold no frames: RTT, guard, or limits too tight")
        tx_stop = t + n * params.t_frame
        rx_start = t + rtt
        rx_stop = tx_stop + model.rtt_s(tx_stop)
        if t_stop is not None and rx_stop > t_stop:
            break
        chunks.append(Chunk(i, k, k + n - 1, t, tx_stop, rx_start, rx_stop))
        k += n
        i += 1
        t = max(rx_stop, tx_stop + t_off_min_s)
    return chunks


def build_schedule(session_id: str, target: str, model: DopplerModel, t_start, f_dial_hz: float,
                   params: Optional[EveParams] = None, text: str = "K0PRT K0PRT",
                   repeat_count: int = 5, mode: str = "monostatic", pilot: bool = True,
                   transmitter: Site = DSES_HASWELL, receiver_site: Optional[Site] = None,
                   t_on_max_s: float = 300.0, t_off_min_s: float = 240.0, chunk_s: Optional[float] = 240.0,
                   rtt_guard_s: float = 30.0, t_stop=None, doppler_table: str = "",
                   notes: str = "") -> Schedule:
    """Plan and validate a complete Schedule: encode `text` under `params` (default
    Variant A) into its symbols, lay repeat_count messages' worth of frames into chunks
    from t_start with plan_chunks() (mode monostatic, bistatic_tx or bistatic_rx), and fill
    in the sites, the dial frequency (Hz), the limits and chunk length (seconds), the
    Doppler source name taken from the model's table, and the notes. ValueError when no
    chunk fits before t_stop or the result fails validate()."""
    p = params or EveParams.variant_a()
    payload, cw, syms = modem.encode_message(text, p)
    t0 = to_unix(t_start)
    chunks = plan_chunks(model, p, t0, repeat_count * p.n_frames_msg, mode, t_on_max_s, t_off_min_s,
                         chunk_s, rtt_guard_s, None if t_stop is None else to_unix(t_stop))
    if not chunks:
        raise ValueError("no chunk fits before t_stop")
    sched = Schedule(
        session_id=session_id, target=target.lower(), epoch=t0, transmitter=transmitter,
        receiver_site=receiver_site or transmitter, mode=mode, f_dial_hz=float(f_dial_hz), params=p,
        text=text, symbols=syms, payload_bits=message.bits_str(payload, None),
        codeword_bits=message.bits_str(cw, None), repeat_count=repeat_count, pilot_enabled=pilot,
        chunks=chunks, doppler_model_name=model.tx.source.split(":")[0] or "table",
        doppler_table=doppler_table, t_on_max_s=t_on_max_s, t_off_min_s=t_off_min_s,
        generated_utc=iso_utc(datetime.now(timezone.utc), 0), notes=notes)
    sched.validate()
    return sched


def describe(sched: Schedule, model: Optional[DopplerModel] = None) -> str:
    """Human-readable summary: two header lines, then one line per chunk (frames, transmit
    and receive windows, round trip) with elevation, Doppler and Doppler rate at the chunk
    start when a model is given."""
    p = sched.params
    lines = [f"{sched.session_id}: {sched.target}, {sched.mode}, f_dial {sched.f_dial_hz / 1e6:.4f} MHz, "
             f"variant {p.variant}, message '{sched.text}', repeat {sched.repeat_count}, pilot {'on' if sched.pilot_enabled else 'off'}",
             f"  frames {sched.n_frames_total} of {sched.n_frames_wanted} wanted = "
             f"{sched.messages_complete()} complete messages; {len(sched.chunks)} chunks; "
             f"{iso_utc(sched.t_start, 0)} .. {iso_utc(sched.t_end, 0)} "
             f"({(sched.t_end - sched.t_start) / 60:.1f} min)"]
    for c in sched.chunks:
        extra = ""
        if model is not None:
            extra = (f"  el {model.elevation_deg(c.tx_start):.1f}  dop {model.doppler_hz(c.tx_start, sched.f_dial_hz):+.0f} Hz"
                     f"  rate {model.rate_hz_s(c.tx_start, sched.f_dial_hz):+.3f} Hz/s")
        lines.append(f"  chunk {c.index:2d}: frames {c.frame_first:5d}-{c.frame_last:5d} ({c.n_frames:3d})  "
                     f"tx {iso_utc(c.tx_start, 0)[11:19]}-{iso_utc(c.tx_stop, 0)[11:19]}  "
                     f"rx {iso_utc(c.rx_start, 0)[11:19]}-{iso_utc(c.rx_stop, 0)[11:19]}  "
                     f"rtt {c.rx_start - c.tx_start:.1f} s{extra}")
    return "\n".join(lines)
