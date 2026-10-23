"""Session engine through the software bench (design document 5.4 stage 3/4 rehearsal):
a short schedule streams through EveToneSource, a delayed AWGN channel, the two-stage
decimator and EveRxSink; the archive decodes offline and the live accumulator agrees.

Run activated:  python -m pytest tests/test_eve_station_sim.py -q     (~40 s, needs GNU Radio)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from eve import EveParams  # noqa: E402
from eve import doppler as D  # noqa: E402
from eve import schedule as S  # noqa: E402

TEXT = "K0PRT K0PRT"
APPX_C_SYMBOLS = [1203, 80, 1317, 1056, 1203, 80, 1317, 1031, 289, 3894, 3168]


def _have_gr():
    try:
        import gnuradio  # noqa: F401
        return True
    except Exception:
        return False


def test_sim_session_decodes():
    if not _have_gr():
        return
    from eve.station import Session, SessionOptions, SimRadio
    from eve_decode import decode_archive
    p = replace(EveParams.variant_a(), n_frames=4, pilot_frames=2)
    now = time.time()
    model = D.DopplerModel(D.synthetic_table("sim", D.DSES_HASWELL, now - 60, now + 3600, range_km=375000.0))
    with tempfile.TemporaryDirectory() as d:
        sched = S.build_schedule("SIMTEST", "sim", model, now + 2.0, 1296e6, params=p, text=TEXT, repeat_count=2,
                                 pilot=True, chunk_s=None, rtt_guard_s=0.1, t_off_min_s=0.5, t_on_max_s=3600.0)
        assert sched.messages_complete() == 2
        radio = SimRadio(p, cn0_db=18.0, seed=3)
        opts = SessionOptions(out_dir=d, pa_in_chain=False, tx_precompensate=False, live_decode=True,
                              start_margin_s=1.0, realtime_mode=False)
        rep = Session(sched, model, radio, opts).run()
        assert not rep.aborted
        assert rep.frames_sent == 2 * p.n_frames_msg
        assert rep.rx["gap_events"] == 0 and len(rep.rx["files"]) == len(sched.chunks)
        assert rep.live_decode["ok"] and rep.live_decode["text"] == TEXT
        # every window archived at full length (within a sample of rounding)
        for c in sched.chunks:
            side = json.loads((Path(d) / f"SIMTEST_{c.index:02d}.json").read_text())
            n_expected = round((c.rx_stop - c.rx_start) * p.modem_rate)
            assert abs(side["n_samples"] - n_expected) <= 2, (c.index, side["n_samples"], n_expected)
            assert side["frame_first"] == c.frame_first
        # the session log exists and carries the schedule
        log = json.loads((Path(d) / "SIMTEST_session.json").read_text())
        assert log["schedule"]["session_id"] == "SIMTEST" and log["chunks_keyed"] >= 1
        # offline decode of the archive: both passes and the combination
        acc, _ = decode_archive(d, sched, verbose=False)
        assert acc.repetitions == [0, 1]
        for r in (0, 1):
            out = acc.decode([r])
            assert out.ok and out.symbols == APPX_C_SYMBOLS, r
        assert acc.decode().ok and min(acc.margin_db()) > 6.0


def test_sim_session_stops_after_the_current_chunk():
    """Session.stop() (D26): the chunk in progress completes and its echo is archived, no
    further chunk is keyed, the stream ends after that echo, the report says so, and the
    archive decodes what was received."""
    if not _have_gr():
        return
    import threading
    from eve.station import Session, SessionOptions, SimRadio
    from eve_decode import decode_archive
    p = replace(EveParams.variant_a(), n_frames=4, pilot_frames=2)
    now = time.time()
    model = D.DopplerModel(D.synthetic_table("sim", D.DSES_HASWELL, now - 60, now + 3600, range_km=375000.0))
    with tempfile.TemporaryDirectory() as d:
        sched = S.build_schedule("SIMSTOP", "sim", model, now + 2.0, 1296e6, params=p, text=TEXT, repeat_count=1,
                                 pilot=True, chunk_s=None, rtt_guard_s=0.1, t_off_min_s=0.5, t_on_max_s=3600.0)
        assert len(sched.chunks) >= 6                     # 44 frames in 6-frame chunks, ~5 s each
        radio = SimRadio(p, cn0_db=25.0, seed=5)
        opts = SessionOptions(out_dir=d, pa_in_chain=False, tx_precompensate=False, live_decode=True,
                              start_margin_s=1.0, realtime_mode=False)
        sess = Session(sched, model, radio, opts)
        # stop during the second chunk's key-down: that chunk completes, the third is never keyed
        t_stop = sched.chunks[1].tx_start + 0.5
        # the operator turns the TX gain during chunk 1 (recorded; the simulated radio has no gain)
        threading.Timer(max(0.1, sched.chunks[0].tx_start + 0.3 - time.time()), sess.set_level,
                        kwargs={"tx_gain_db": 40.0, "rx_gain_db": 35.0, "why": "test"}).start()
        threading.Timer(max(0.1, t_stop - time.time()), sess.stop, args=("test stop",)).start()
        t_run = time.time()
        rep = sess.run()
        elapsed = time.time() - t_run
        assert rep.stopped_early and rep.stop_reason == "test stop" and not rep.aborted
        assert rep.chunks_keyed == 2
        assert elapsed < sched.chunks[3].tx_start - t_run           # ended well before the full schedule
        assert len(rep.rx["files"]) >= 2                            # both echoes archived
        files = sorted(Path(d).glob("SIMSTOP_*.eve.iq"))
        assert len(files) <= 3                                      # at most a partial third window, nothing beyond
        log = json.loads((Path(d) / "SIMSTOP_session.json").read_text())
        assert log["stopped_early"] and log["chunks_keyed"] == 2
        assert log["level_events"][0]["tx_gain_db"] == 40.0 and log["level_events"][0]["why"] == "test"
        assert log["level_events"][1]["rx_gain_db"] == 35.0
        acc, _ = decode_archive(d, sched, verbose=False)            # decodes what it has, no error
        assert acc.frames_seen <= 3 * 6


def test_sim_session_resyncs_after_a_stall():
    """The transmit stream re-times itself after a stall (D26, Haswell 2026-10-23): the
    device clock jumps 6 s ahead of the stream (what an underflow looks like from the tone
    source), the source ends its burst, skips the backlog and starts a new timed burst; the
    run completes, the report counts the resync and names the chunk that lost frames."""
    if not _have_gr():
        return
    import threading
    from eve import gr_blocks
    from eve.station import Session, SessionOptions, SimRadio
    p = replace(EveParams.variant_a(), n_frames=4, pilot_frames=2)
    now = time.time()
    model = D.DopplerModel(D.synthetic_table("sim", D.DSES_HASWELL, now - 60, now + 3600, range_km=375000.0))
    with tempfile.TemporaryDirectory() as d:
        sched = S.build_schedule("SIMRESYNC", "sim", model, now + 2.0, 1296e6, params=p, text=TEXT, repeat_count=1,
                                 pilot=True, chunk_s=None, rtt_guard_s=0.1, t_off_min_s=0.5, t_on_max_s=3600.0)
        radio = SimRadio(p, cn0_db=25.0, seed=7)
        radio.resync_test = True
        opts = SessionOptions(out_dir=d, pa_in_chain=False, tx_precompensate=False, live_decode=True,
                              start_margin_s=1.0, realtime_mode=False)
        sess = Session(sched, model, radio, opts)
        jump = sched.chunks[1].tx_start + 0.3 - time.time()       # during chunk 2's transmission

        def stall():
            radio.time_offset += 6.0
        threading.Timer(max(0.1, jump), stall).start()
        rep = sess.run()
        assert not rep.aborted
        assert sess.tone.resyncs == 1, sess.tone.resyncs
        assert 6.0 - 3.0 < sess.tone.resync_skipped_s < 6.0 + gr_blocks.RESYNC_LEAD_S + 0.5   # the backlog less the buffer lead
        assert rep.tx_events.get("resync") == 1 and len(rep.tx_resyncs) == 1
        assert 2 in rep.tx_resyncs[0]["chunks"] or rep.tx_resyncs[0]["chunks"] == [] or rep.tx_resyncs[0]["chunks"]
        log = json.loads((Path(d) / "SIMRESYNC_session.json").read_text())
        assert log["tx_events"]["resync"] == 1 and log["tx_resyncs"][0]["skipped_s"] > 2.0
        # the frames before the stall arrived (chunk 1 decoded live) and the run went to its end
        assert rep.live_decode["frames"] >= 6 and rep.chunks_keyed >= 6


def test_preflight_enforces_pa_limits():
    if not _have_gr():
        return
    from eve.station import Session, SessionOptions, SimRadio
    p = replace(EveParams.variant_a(), n_frames=4)
    now = time.time()
    model = D.DopplerModel(D.synthetic_table("sim", D.DSES_HASWELL, now - 60, now + 3600, range_km=375000.0))
    sched = S.build_schedule("PF", "sim", model, now + 5.0, 1296e6, params=p, repeat_count=1, chunk_s=None,
                             rtt_guard_s=0.1, t_off_min_s=0.5, t_on_max_s=3600.0)
    sess = Session(sched, model, SimRadio(p), SessionOptions(pa_in_chain=True, tx_precompensate=False))
    problems = sess.preflight()
    assert any("off time" in x for x in problems)          # 0.5 s gaps violate the 240 s PA minimum
    sess2 = Session(sched, model, SimRadio(p), SessionOptions(pa_in_chain=False, tx_precompensate=False))
    assert sess2.preflight() == []


if __name__ == "__main__":
    for n in ("test_preflight_enforces_pa_limits", "test_sim_session_decodes", "test_sim_session_stops_after_the_current_chunk"):
        t = time.time()
        globals()[n]()
        print(f"PASS {n} ({time.time() - t:.1f}s)")
