"""Operations planning (eve/ops.py, design D26): the elevation windows of an ephemeris
table, the start and stop a run derives from them, and the day-by-day table. Runs on a
fabricated table (a sinusoidal elevation) so it needs no network and no astropy."""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eve import EveParams  # noqa: E402
from eve import doppler as D  # noqa: E402
from eve import ops  # noqa: E402
from eve import schedule as S  # noqa: E402

T0 = D.to_unix("2026-10-24T00:00:00Z")


def _model(days: float = 2.0, step_s: float = 300.0, peak_deg: float = 32.0) -> D.DopplerModel:
    """A table where the target rises to peak_deg once a day: el = peak x sin(2 pi (t - 6 h) / 24 h)
    (above 0 from 06:00 to 18:00 UTC, highest at 12:00), Venus-like range and rate."""
    t = np.arange(T0, T0 + days * 86400.0 + 1, step_s)
    el = peak_deg * np.sin(2 * np.pi * (t - T0 - 6 * 3600.0) / 86400.0)
    rng = 40.8e6 + 2.0 * (t - T0)                     # km, receding 2 km/s
    tab = D.EphemerisTable("venus", D.DSES_HASWELL, t, rng, np.full(t.size, 2.0), np.full(t.size, 180.0), el, "fake")
    return D.DopplerModel(tab)


def test_windows_rise_set_and_peak():
    m = _model()
    ws = ops.windows(m, 10.0)
    assert len(ws) == 2
    w = ws[0]
    # el = 10 at sin = 10/32 -> t - 6h = 24h/(2 pi) asin(0.3125) = 1.2144 h -> rise 07:12:52, set 16:47:08
    assert abs(w.t_rise - (T0 + 6 * 3600 + 1.2144 * 3600)) < 20.0
    assert abs(w.t_set - (T0 + 18 * 3600 - 1.2144 * 3600)) < 20.0
    assert abs(w.t_max - (T0 + 12 * 3600)) < 300.0 and abs(w.el_max - 32.0) < 0.1
    assert 9.4 < w.hours < 9.7
    # the second day's window is open-ended only if the table ends inside it; it does not here
    assert ws[1].t_set is not None
    # rise_time: already up -> the next whole minute; before the window -> its rise; past the last -> None
    t_up = T0 + 12 * 3600 + 17.0
    assert ops.rise_time(m, t_up, 10.0) == 60.0 * np.ceil(t_up / 60.0)
    assert abs(ops.rise_time(m, T0, 10.0) - w.t_rise) <= 60.0
    assert ops.rise_time(m, T0 + 2 * 86400.0 - 3600.0, 10.0) is None
    # stop_time: inside the window -> its set; before it (rising) -> the same set
    assert ops.stop_time(m, t_up, 10.0) == w.t_set
    assert ops.stop_time(m, T0 + 3600.0, 10.0) == w.t_set


def test_resolve_stop_takes_the_earlier():
    m = _model()
    w = ops.windows(m, 10.0)[0]
    t_start = T0 + 9 * 3600
    t, why = ops.resolve_stop(m, t_start, 10.0, None, "venus")
    assert t == w.t_set and "sets below 10 deg" in why
    t, why = ops.resolve_stop(m, t_start, 10.0, "2026-10-24T12:00:00Z", "venus")
    assert t == T0 + 12 * 3600 and "stop time" in why
    t, why = ops.resolve_stop(m, t_start, 10.0, "2026-10-24T23:00:00Z", "venus")
    assert t == w.t_set                                     # the elevation stop comes first
    t, why = ops.resolve_stop(m, t_start, -90.0, None, "venus")
    assert t is None and why == ""                          # never below -90: no stop


def test_schedule_stops_at_the_window_and_plan_text():
    m = _model()
    w = ops.windows(m, 10.0)[0]
    p = EveParams.named("B")
    t_start = ops.rise_time(m, T0, 10.0)
    sched = S.build_schedule("OPS", "venus", m, t_start, 1299.5e6, params=p, repeat_count=20, t_stop=w.t_set)
    assert sched.chunks[-1].rx_stop <= w.t_set
    assert 0 < sched.messages_complete() < 20               # a 9.6 h window cannot hold twenty 34-min passes
    full = S.build_schedule("OPS", "venus", m, t_start, 1299.5e6, params=p, repeat_count=20)
    assert full.t_end > w.t_set and len(full.chunks) > len(sched.chunks)
    txt = ops.day_plan_text("venus", "Haswell", m, t_start, "when venus rises above 10 deg", w.t_set,
                            "venus sets below 10 deg", 10.0, sched)
    assert "above 10 deg 07:1" in txt and "highest 32.0 deg" in txt
    assert f"passes: 20 wanted, {sched.messages_complete()} complete" in txt and "last echo in by" in txt


def test_ops_table_rows_and_text():
    m = _model(days=3.0)
    p = EveParams.named("B")
    rows = ops.ops_table("venus", m, T0, 10.0, p, repeat_count=7)
    assert len(rows) == 3 and [r["date"] for r in rows] == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert all(r["passes"] >= 1 and r["chunks"] > 0 and r["t_last_echo"] <= r["t_set"] for r in rows)
    assert rows[0]["loss_db"] == 0.0 and rows[2]["loss_db"] > rows[1]["loss_db"] > 0.0   # receding
    txt = ops.format_ops_table(rows, "venus", 10.0, 7, tz_name="UTC")
    assert "2026-10-25" in txt and "/7" in txt and "max el" in txt
    # a window the table cuts off is not listed; nothing above 40 deg at all
    assert ops.ops_table("venus", m, T0, 40.0, p) == []
    assert "never above 40 deg" in ops.format_ops_table([], "venus", 40.0, 7)


if __name__ == "__main__":
    for n in [k for k in list(globals()) if k.startswith("test_")]:
        globals()[n]()
        print("PASS", n)
