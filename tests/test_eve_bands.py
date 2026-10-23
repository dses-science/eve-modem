"""The on-air band limit (eve/bands.py, D27): the whole comb must lie inside a US amateur
band for a Moon or Venus run; the session preflight refuses otherwise."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import replace  # noqa: E402

from eve import EveParams, bands  # noqa: E402
from eve import doppler as D  # noqa: E402
from eve import schedule as S  # noqa: E402


def test_comb_inside_a_band():
    a, b = EveParams.variant_a(), EveParams.named("B")
    assert bands.band_of(1299.5e6, b) == "23 cm" and bands.band_of(1296e6, a) == "23 cm"
    assert bands.band_of(2304e6, a) == "13 cm (lower)" and bands.band_of(2400e6, a) == "13 cm"
    assert bands.band_of(1240e6, a) == "23 cm"
    # the comb reaches 48.5 kHz above the dial: 1299.96 puts its top past 1300
    assert bands.band_of(1299.96e6, a) is None and bands.band_of(1299.95e6, a) == "23 cm"
    assert bands.check_on_air(1299.95e6, a) is None
    msg = bands.check_on_air(1310e6, a)
    assert msg and "outside the US amateur bands" in msg and "1240-1300 MHz (23 cm)" in msg
    assert bands.check_on_air(1239.99e6, a)             # the dial itself below the band edge
    assert bands.check_on_air(2320e6, a)                # between the two 13 cm segments
    lim = {name: (lo, hi) for lo, hi, name in bands.dial_limits_mhz(a)}
    assert lim["23 cm"] == (1240.0, 1299.9515)


def test_preflight_refuses_out_of_band_on_the_air():
    from eve.station import Session, SessionOptions, SimRadio
    p = replace(EveParams.named("B"), n_frames=4)        # test length: 15 Moon chunks, not 906
    now = time.time()
    model = D.DopplerModel(D.synthetic_table("moon", D.DSES_HASWELL, now - 60, now + 3600, range_km=375000.0))
    for f, ok in ((1296e6, True), (1310e6, False), (1299.99e6, False)):
        sched = S.build_schedule("BAND", "moon", model, now + 5.0, f, params=p, repeat_count=1,
                                 chunk_s=2.4, rtt_guard_s=0.1, t_off_min_s=0.5, t_on_max_s=300.0)
        probs = Session(sched, model, SimRadio(p), SessionOptions(pa_in_chain=False, tx_precompensate=False)).preflight()
        assert (not any("on the air" in x for x in probs)) == ok, (f, probs)
    # receive only (a partner transmits): no band check on our side
    sched = S.build_schedule("BAND", "moon", model, now + 5.0, 1310e6, params=p, repeat_count=1,
                             chunk_s=2.4, rtt_guard_s=0.1, t_off_min_s=0.5, t_on_max_s=300.0, mode="bistatic_rx")
    probs = Session(sched, model, SimRadio(p), SessionOptions(pa_in_chain=False, tx_precompensate=False, tx_enabled=False)).preflight()
    assert not any("on the air" in x for x in probs)


if __name__ == "__main__":
    test_comb_inside_a_band()
    test_preflight_refuses_out_of_band_on_the_air()
    print("PASS")
