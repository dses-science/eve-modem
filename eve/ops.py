"""Operations planning: when the target is usable, when a run should start and stop, and
the day-by-day operating schedule (design document 4.2, D26).

A sky run is bounded three ways. The chunk table is laid from the start time until the
repeat count is satisfied (schedule.plan_chunks), but never past a STOP TIME: the earlier
of an operator-set UTC stop and the moment the target drops below the MINIMUM ELEVATION
(the end of its window). The start can likewise be "when the target rises" above that
elevation, so an operator can press START hours ahead and the modem waits. The window
arithmetic lives here, on the ephemeris table the session already fetches, so the Setup
tab's preview, the worker, and the command-line planner (tools/eve_ops_schedule.py) agree.

The Venus operating schedule of 2026-10-20 (seven passes a day, 14:20-22:27 UTC above
10 degrees at Haswell on conjunction day) is what ops_table() reproduces from the
ephemeris for any span of days.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .doppler import DopplerModel, iso_utc, to_unix
from .params import EveParams
from . import schedule as S

MIN_EL_DEG = 10.0       # the operating schedule's floor (the dish and the Tsys both argue for it)


@dataclass
class Window:
    """One interval during which the target is at or above the minimum elevation at every
    site of the model: rise and set (Unix seconds; t_set None when the table ends with
    the target still up), the time and value of the highest elevation inside it."""
    t_rise: float
    t_set: Optional[float]
    t_max: float
    el_max: float

    @property
    def hours(self) -> Optional[float]:
        """Length in hours, None when open-ended."""
        return None if self.t_set is None else (self.t_set - self.t_rise) / 3600.0


def _refine(t_lo: float, t_hi: float, f, want_up: bool, steps: int = 20) -> float:
    """Bisect the crossing of f(t) >= 0 between t_lo (state not want_up) and t_hi (state
    want_up) to the second or so."""
    for _ in range(steps):
        t_mid = 0.5 * (t_lo + t_hi)
        if (f(t_mid) >= 0.0) == want_up:
            t_hi = t_mid
        else:
            t_lo = t_mid
    return t_hi


def windows(model: DopplerModel, min_el_deg: float = MIN_EL_DEG) -> List[Window]:
    """Every window of the model's table where the target is at or above min_el_deg (at
    both sites when the model is bistatic), with the crossings refined between table rows."""
    t = np.asarray(model.tx.t_unix, dtype=np.float64)
    el = np.asarray(model.tx.el_deg, dtype=np.float64)
    if not model.monostatic:
        el = np.minimum(el, model.rx.el_at(t))

    def margin(tt: float) -> float:
        m = model.elevation_deg(tt) - min_el_deg
        if not model.monostatic:
            m = min(m, model.elevation_deg(tt, "rx") - min_el_deg)
        return float(m)

    up = el >= min_el_deg
    out: List[Window] = []
    i = 0
    n = up.size
    while i < n:
        if not up[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and up[j + 1]:
            j += 1
        # crossings refined by bisection: the rise between the last row down and the first row
        # up, the set between the last row up and the first row down (bisected backwards)
        t_rise = float(t[i]) if i == 0 else _refine(float(t[i - 1]), float(t[i]), margin, True)
        t_set = None if j == n - 1 else _refine(float(t[j + 1]), float(t[j]), margin, True)
        k = i + int(np.argmax(el[i:j + 1]))
        out.append(Window(t_rise, t_set, float(t[k]), float(el[k])))
        i = j + 1
    return out


def window_at(model: DopplerModel, t: float, min_el_deg: float = MIN_EL_DEG) -> Optional[Window]:
    """The window containing time t, else the first one that opens after t, else None
    (the target stays below min_el_deg for the rest of the table)."""
    for w in windows(model, min_el_deg):
        if w.t_set is None or w.t_set > t:
            return w
    return None


def rise_time(model: DopplerModel, t_from: float, min_el_deg: float = MIN_EL_DEG) -> Optional[float]:
    """The first whole minute at or after t_from when the target is at or above
    min_el_deg: t_from itself (rounded up) when it already is, the next window's rise
    otherwise, None when it never rises inside the table."""
    w = window_at(model, t_from, min_el_deg)
    if w is None:
        return None
    return 60.0 * math.ceil(max(t_from, w.t_rise) / 60.0)


def stop_time(model: DopplerModel, t_start: float, min_el_deg: float = MIN_EL_DEG) -> Optional[float]:
    """When a run starting at t_start must end on elevation: the set of the window
    containing t_start (or of the next window, when the target is still rising toward
    min_el_deg at t_start); None when the table ends with the target up."""
    w = window_at(model, t_start, min_el_deg)
    return None if w is None else w.t_set


def resolve_stop(model: DopplerModel, t_start: float, min_el_deg: float, t_stop_utc=None,
                 target: str = "target") -> Tuple[Optional[float], str]:
    """The run's stop time and the reason, in words, for the preview and the log: the
    earlier of the operator's UTC stop (t_stop_utc, any TimeLike, or None) and the
    elevation stop; (None, '') when neither applies."""
    cands: List[Tuple[float, str]] = []
    if t_stop_utc is not None and t_stop_utc != "":
        cands.append((to_unix(t_stop_utc), "the stop time set on the Setup tab"))
    t_el = stop_time(model, t_start, min_el_deg)
    if t_el is not None:
        cands.append((t_el, f"{target} sets below {min_el_deg:.0f} deg"))
    if not cands:
        return None, ""
    return min(cands, key=lambda c: c[0])


def day_plan_text(target: str, site_name: str, model: DopplerModel, t_start: float, start_why: str,
                  t_stop: Optional[float], stop_why: str, min_el_deg: float, sched: Optional[S.Schedule]) -> str:
    """The operations header of a sky preview: the day's window at the minimum elevation,
    the highest point, the start and stop with their reasons, and how many passes fit."""
    w = window_at(model, t_start, min_el_deg)
    day = iso_utc(t_start, 0)[:10]
    lines = []
    if w is None:
        lines.append(f"OPERATIONS  {target} at {site_name} on {day}: not above {min_el_deg:.0f} deg within the ephemeris span")
    else:
        set_txt = "still up at the end of the ephemeris" if w.t_set is None else iso_utc(w.t_set, 0)[11:16] + "Z"
        hours = "" if w.hours is None else f" ({w.hours:.1f} h)"
        # a window that was already open when the table begins has no rise in it
        rise_txt = ("since before " if w.t_rise <= model.tx.t_start + 1.0 else "") + iso_utc(w.t_rise, 0)[11:16] + "Z"
        lines.append(f"OPERATIONS  {target} at {site_name} on {day}: above {min_el_deg:.0f} deg "
                     f"{rise_txt} - {set_txt}{hours}, highest {w.el_max:.1f} deg at {iso_utc(w.t_max, 0)[11:16]}Z")
    stop_txt = "after the passes" if t_stop is None else f"{iso_utc(t_stop, 0)[11:19]}Z ({stop_why})"
    lines.append(f"  start {iso_utc(t_start, 0)[11:19]}Z ({start_why}); stop {stop_txt}")
    if sched is not None:
        done = sched.messages_complete()
        last = sched.chunks[-1]
        partial = "" if sched.n_frames_total == done * sched.params.n_frames_msg else " and part of one more"
        lines.append(f"  passes: {sched.repeat_count} wanted, {done} complete{partial} in {len(sched.chunks)} chunks; "
                     f"last echo in by {iso_utc(last.rx_stop, 0)[11:19]}Z"
                     + (f", {(t_stop - last.rx_stop) / 60:.0f} min before the stop" if t_stop is not None else ""))
    return "\n".join(lines)


def ops_table(target: str, model: DopplerModel, t_from: float, min_el_deg: float, params: EveParams,
              text: str = "K0PRT K0PRT", repeat_count: int = 7, mode: str = "monostatic",
              t_on_max_s: float = 300.0, t_off_min_s: float = 240.0, chunk_s: Optional[float] = 240.0,
              rtt_guard_s: float = 30.0, f_dial_hz: float = 1299.5e6) -> List[Dict]:
    """One row per window of the model's table from t_from on: the day, rise and set at
    min_el_deg, hours, highest elevation, the chunks and passes that fit between rise and
    set with the schedule's own planner, the last echo, the range at the highest point,
    the one-way loss relative to the closest day in the span (40 log d / d_min, the
    radar equation's fourth power), and the Doppler at rise. No Sun separation: the table
    carries no solar ephemeris. The model is fetched by the caller (doppler.make_model at
    a 5-minute step covers weeks in one request)."""
    rows: List[Dict] = []
    for w in windows(model, min_el_deg):
        if w.t_set is None or w.t_set <= t_from:
            continue
        t_rise = 60.0 * math.ceil(max(t_from, w.t_rise) / 60.0)
        n_wanted = repeat_count * params.n_frames_msg
        try:
            chunks = S.plan_chunks(model, params, t_rise, n_wanted, mode, t_on_max_s, t_off_min_s,
                                   chunk_s, rtt_guard_s, w.t_set)
        except ValueError:
            chunks = []
        n_frames = chunks[-1].frame_last + 1 if chunks else 0
        rows.append({
            "date": iso_utc(w.t_rise, 0)[:10], "t_rise": t_rise, "t_set": w.t_set, "hours": (w.t_set - t_rise) / 3600.0,
            "t_max": w.t_max, "el_max": w.el_max, "chunks": len(chunks),
            "passes": n_frames // params.n_frames_msg, "frames": n_frames,
            "t_last_echo": chunks[-1].rx_stop if chunks else None,
            "range_km": float(np.atleast_1d(model.tx.range_at(w.t_max))[0]),
            "rtt_s": model.rtt_s(w.t_max), "doppler_hz": model.doppler_hz(t_rise, f_dial_hz),
        })
    if rows:
        d_min = min(r["range_km"] for r in rows)
        for r in rows:
            r["loss_db"] = 40.0 * math.log10(r["range_km"] / d_min)
    return rows


def _local(t: float, tz_name: str) -> str:
    """Local wall-clock HH:MM for a Unix time in the named zone, or '' when zoneinfo has no
    data for it (no tzdata on this machine)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(t, ZoneInfo(tz_name)).strftime("%H:%M %Z")
    except Exception:
        return ""


def format_ops_table(rows: Sequence[Dict], target: str, min_el_deg: float, repeat_count: int,
                     tz_name: str = "America/Denver") -> str:
    """The operating schedule as fixed-width text: one line per day with the rise and set
    (UTC and local), hours, highest elevation, passes that fit of the repeat count, the last
    echo, range and loss versus the best day, and the Doppler at rise."""
    if not rows:
        return f"{target}: never above {min_el_deg:.0f} deg in the span"
    local = any(_local(r["t_rise"], tz_name) for r in rows)
    head = (f"{'date':10}  {'rise':>6} {'set':>6}  " + (f"{'rise local':>11} {'set local':>11}  " if local else "")
            + f"{'hours':>5}  {'max el':>6}  {'passes':>6}  {'chunks':>6}  {'last echo':>9}  {'Mkm':>6}  {'loss dB':>7}  {'Doppler':>8}")
    lines = [f"{target} above {min_el_deg:.0f} deg; up to {repeat_count} passes a day; loss = one-way vs the closest day (40 log d/d0)",
             head]
    for r in rows:
        loc = (f"{_local(r['t_rise'], tz_name):>11} {_local(r['t_set'], tz_name):>11}  " if local else "")
        echo = "" if r["t_last_echo"] is None else iso_utc(r["t_last_echo"], 0)[11:16] + "Z"
        lines.append(f"{r['date']:10}  {iso_utc(r['t_rise'], 0)[11:16]}Z {iso_utc(r['t_set'], 0)[11:16]}Z  {loc}"
                     f"{r['hours']:5.1f}  {r['el_max']:6.1f}  {r['passes']:3d}/{repeat_count:<2d}  {r['chunks']:6d}  {echo:>9}  "
                     f"{r['range_km'] / 1e6:6.2f}  {r.get('loss_db', 0.0):7.1f}  {r['doppler_hz']:+8.0f}")
    return "\n".join(lines)
