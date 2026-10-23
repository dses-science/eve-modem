#!/usr/bin/env python3
"""The operating schedule for the Moon or Venus at Haswell: one line per day with the window
above the minimum elevation, the highest elevation, the passes that fit with the modem's
chunking, the last echo, the range and its loss against the best day, and the Doppler at
rise (eve/ops.py, design D26). The same table the application's 'Operations schedule'
button shows; this one is for the field log and for planning emails.

    python tools/eve_ops_schedule.py --target venus --from 2026-10-22 --days 14 --min-el 10 --repeat 7

One Horizons request at a 5-minute step covers the whole span (astropy fallback offline).
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eve import EveParams  # noqa: E402
from eve import doppler as D  # noqa: E402
from eve import ops  # noqa: E402
from eve.doppler import iso_utc  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default="venus", help="venus | moon")
    ap.add_argument("--from", dest="t_from", default=None, help="first day, ISO UTC (default: now)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-el", type=float, default=ops.MIN_EL_DEG, help="the window's elevation floor, degrees")
    ap.add_argument("--f-dial", type=float, default=1299.5e6)
    ap.add_argument("--variant", default="B")
    ap.add_argument("--n-frames", type=int, default=None, help="frames per symbol (default: the variant's full length)")
    ap.add_argument("--message", default="K0PRT K0PRT")
    ap.add_argument("--repeat", type=int, default=7, help="passes wanted per day")
    ap.add_argument("--mode", default="monostatic")
    ap.add_argument("--chunk", type=float, default=240.0)
    ap.add_argument("--rtt-guard", type=float, default=30.0)
    ap.add_argument("--t-on-max", type=float, default=300.0)
    ap.add_argument("--t-off-min", type=float, default=240.0)
    ap.add_argument("--source", default="auto", help="horizons | astropy | auto")
    ap.add_argument("--tz", default="America/Denver", help="local zone for the rise/set columns ('' for none)")
    a = ap.parse_args(argv)
    t0 = D.to_unix(a.t_from) if a.t_from else time.time()
    days = max(1, min(60, a.days))
    p = EveParams.named(a.variant)
    if a.n_frames:
        p = replace(p, n_frames=a.n_frames)
    model = D.make_model(a.target, D.DSES_HASWELL, t0 - 1800, t0 + days * 86400.0, source=a.source, table_path=None, step="5 m")
    rows = ops.ops_table(a.target, model, t0, a.min_el, p, text=a.message, repeat_count=a.repeat, mode=a.mode,
                         t_on_max_s=a.t_on_max, t_off_min_s=a.t_off_min, chunk_s=a.chunk, rtt_guard_s=a.rtt_guard,
                         f_dial_hz=a.f_dial)
    print(f"OPERATIONS SCHEDULE  {a.target} at Haswell, {days} days from {iso_utc(t0, 0)[:16]}Z ({model.tx.source}); "
          f"Variant {p.variant}, {p.n_frames} frames per symbol, chunks of {a.chunk:.0f} s")
    print(ops.format_ops_table(rows, a.target, a.min_el, a.repeat, tz_name=a.tz or "UTC"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
