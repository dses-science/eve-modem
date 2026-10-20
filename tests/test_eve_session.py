"""Tests for doppler, schedule, sync, and sigmf_io (design document 4.2 to 4.5, 6.4, 8).

Run:  conda activate ./.conda ; python -m pytest tests -q
(astropy needs the env's Library/bin on PATH, which activation provides.)
The Horizons fetch is exercised only when the network answers; otherwise skipped.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eve import EveParams, modem, channel, sync, sigmf_io  # noqa: E402
from eve import doppler as D  # noqa: E402
from eve import schedule as S  # noqa: E402

TEXT = "K0PRT K0PRT"
APPX_C_SYMBOLS = [1203, 80, 1317, 1056, 1203, 80, 1317, 1031, 289, 3894, 3168]
_CACHE = {}


def venus_model() -> D.DopplerModel:
    if "venus" not in _CACHE:
        tab = D.compute_astropy("venus", D.DSES_HASWELL, "2026-10-24T13:00:00Z", "2026-10-24T23:30:00Z", 60.0)
        _CACHE["venus"] = D.DopplerModel(tab)
    return _CACHE["venus"]


def moon_model() -> D.DopplerModel:
    if "moon" not in _CACHE:
        tab = D.compute_astropy("moon", D.DSES_HASWELL, "2026-10-24T00:00:00Z", "2026-10-24T12:00:00Z", 300.0)
        _CACHE["moon"] = D.DopplerModel(tab)
    return _CACHE["moon"]


# ---- doppler -------------------------------------------------------------------------------
def test_time_helpers():
    t = D.to_unix("2026-10-24T15:30:00Z")
    assert D.iso_utc(t, 0) == "2026-10-24T15:30:00Z"
    assert D.to_unix("2026-10-24 15:30:00") == t
    assert abs(D.to_unix(D.to_datetime(t)) - t) < 1e-6


def test_iso_utc_rounds_with_carry():
    """A fraction that rounds up to the next second must carry into the seconds field (the
    2026-10-20 EME schedule bug: 53.9996 s was written as 53.000, one second early, and the
    GUI refused chunk 866 of its own schedule)."""
    t = D.to_unix("2026-10-20T01:49:53Z")
    assert D.iso_utc(t + 0.9994) == "2026-10-20T01:49:53.999Z"
    assert D.iso_utc(t + 0.9996) == "2026-10-20T01:49:54.000Z"
    assert D.iso_utc(t + 0.99999) == "2026-10-20T01:49:54.000Z"
    assert D.iso_utc(t + 6.9997, 0) == "2026-10-20T01:50:00Z"            # carry across a minute
    assert D.iso_utc(D.to_unix("2026-12-31T23:59:59.9995Z")) == "2027-01-01T00:00:00.000Z"
    assert D.iso_utc(t + 0.1234567, 6) == "2026-10-20T01:49:53.123457Z"
    # text -> seconds -> text stays within half a unit for every millisecond of a second
    for ms in range(0, 1000):
        x = t + ms / 1000.0 + 0.0004
        assert abs(D.to_unix(D.iso_utc(x)) - x) <= 0.0005 + 1e-9, ms


def test_venus_geometry_matches_document():
    """Section 2.1 / Figure 1: RTT 272 s, two-way Doppler +9.5 to -4.5 kHz at 2304 MHz,
    rate to -0.48 Hz/s, window above 20 deg about 15:26 to 21:22 UTC."""
    m = venus_model()
    t18 = D.to_unix("2026-10-24T18:00:00Z")
    assert abs(m.rtt_s(t18) - 272.25) < 0.3
    assert abs(m.doppler_hz(t18, 1299.5e6) - m.doppler_hz(t18, 2304e6) * 1299.5 / 2304) < 1.0
    ts = np.arange(m.tx.t_start, m.tx.t_stop, 300.0)
    dop = m.doppler_series(ts, 2304e6)
    assert 9300 < dop.max() < 9600 and -4600 < dop.min() < -4300
    rates = [m.rate_hz_s(t, 2304e6) for t in ts]
    assert -0.50 < min(rates) < -0.46
    (a, b), = m.window(20.0)
    assert D.iso_utc(a, 0)[11:16] in ("15:25", "15:26", "15:27") and D.iso_utc(b, 0)[11:16] in ("21:21", "21:22", "21:23")
    assert 33 < m.elevation_deg(t18) < 35


def test_moon_geometry():
    m = moon_model()
    t = D.to_unix("2026-10-24T06:00:00Z")
    assert 2.3 < m.rtt_s(t) < 2.7
    assert abs(m.doppler_hz(t, 1296e6)) < 4000


def test_table_csv_roundtrip():
    m = venus_model()
    with tempfile.TemporaryDirectory() as d:
        pth = m.tx.save(Path(d) / "venus.csv")
        t2 = D.EphemerisTable.load(pth)
    assert t2.target == "venus" and t2.site.name == "DSES Haswell"
    assert np.allclose(t2.range_km, m.tx.range_km, atol=1e-5)
    assert np.allclose(t2.t_unix, m.tx.t_unix)


def test_horizons_parse_and_fetch():
    sample = ("junk\n$$SOE\n"
              " 2026-Oct-24 15:00:00,*, , 129.385938,    16.289981,  40800000.123456, -0.5017066,\n"
              " 2026-Oct-24 15:01,*, , 129.578406,    16.442088,  0.27281507181110, -0.5003916,\n"
              "$$EOE\n")
    tab = D.parse_horizons(sample, "venus", D.DSES_HASWELL)
    assert tab.t_unix.size == 2 and abs(tab.range_km[0] - 40800000.123456) < 1e-6
    assert abs(tab.range_km[1] - 0.27281507181110 * D.AU_KM) < 1.0     # AU fallback
    assert tab.step_s == 60.0
    try:
        th = D.fetch_horizons("venus", D.DSES_HASWELL, "2026-10-24T17:58:00Z", "2026-10-24T18:02:00Z", "1 m")
    except Exception:
        return   # no network: skipped
    m = venus_model()
    t18 = D.to_unix("2026-10-24T18:00:00Z")
    assert abs(float(th.range_at(t18)[0]) - float(m.tx.range_at(t18)[0])) < 2000.0   # km, builtin vs JPL
    assert abs(float(th.rate_at(t18)[0]) - float(m.tx.rate_at(t18)[0])) < 0.005     # km/s
    assert abs(float(th.el_at(t18)[0]) - m.elevation_deg(t18)) < 0.05


# ---- schedule -------------------------------------------------------------------------------
def test_monostatic_schedule_conjunction_day():
    m = venus_model()
    sch = S.build_schedule("DSES-EVE-20261024-A", "venus", m, "2026-10-24T15:30:00Z", 1299.5e6,
                           repeat_count=5, t_stop="2026-10-24T21:20:00Z")
    p = sch.params
    assert sch.symbols == APPX_C_SYMBOLS
    assert sch.messages_complete() == 5                       # five passes fit the window (4.2)
    assert all(c.n_frames <= 688 for c in sch.chunks)         # 4-minute chunks on the frame grid
    assert all(c.tx_stop - c.tx_start <= 240.0 + 1e-6 for c in sch.chunks)
    for a, b in zip(sch.chunks, sch.chunks[1:]):
        assert b.tx_start >= a.rx_stop - 1e-6                 # silent while the echo arrives
        assert b.tx_start - a.tx_stop >= 240.0 - 1e-6         # amplifier off time
        assert b.frame_first == a.frame_last + 1
    assert 5.2 * 3600 < sch.t_end - sch.t_start < 5.6 * 3600
    assert abs(sch.frame_tx_time(688) - sch.chunks[1].tx_start) < 1e-6
    assert abs(sch.frame_tx_time(5) - (sch.epoch + 5 * p.t_frame)) < 1e-6
    fm = sch.frame_map()
    assert fm.tone(0) == p.pilot_tone and fm.tone(40) == APPX_C_SYMBOLS[0]
    assert fm.tone(688) == p.pilot_tone                       # every chunk starts with the pilot
    assert fm.tone(688 + 40) == APPX_C_SYMBOLS[1]             # frame 728 -> symbol 1 (473 <= 728 < 946)
    one = S.build_schedule("X", "venus", m, "2026-10-24T15:30:00Z", 1299.5e6, repeat_count=1)
    assert len(one.chunks) == 8 and 60 * 60 < one.t_end - one.t_start < 70 * 60


def test_schedule_json_contract_roundtrip():
    m = venus_model()
    sch = S.build_schedule("DSES-EVE-20261024-A", "venus", m, "2026-10-24T15:30:00Z", 1299.5e6,
                           repeat_count=2, doppler_table="horizons_venus_haswell_20261024.csv")
    js = sch.to_json()
    d = json.loads(js)
    for key in ("schema", "session_id", "target", "epoch_utc", "transmitter", "receiver", "rf", "waveform",
                "message", "repeat_count", "pilot", "chunks", "doppler", "limits", "generated_by", "generated_utc"):
        assert key in d
    assert d["schema"] == "dses-eve-schedule/1" and d["rf"]["f_if_hz"] == 25000.0
    assert d["message"]["symbols"] == APPX_C_SYMBOLS and len(d["message"]["codeword_bits"]) == 127
    back = S.Schedule.from_json(js)
    assert back.params == sch.params and back.symbols == sch.symbols
    assert [c.to_dict() for c in back.chunks] == [c.to_dict() for c in sch.chunks]
    assert abs(back.epoch - sch.epoch) < 1e-3
    # a tampered schedule is refused
    bad = json.loads(js)
    bad["message"]["symbols"][0] ^= 1
    try:
        S.Schedule.from_dict(bad)
        assert False, "tampered symbols accepted"
    except ValueError:
        pass


def test_bistatic_and_eme_schedules():
    m = venus_model()
    bi = S.build_schedule("Y", "venus", m, "2026-10-24T15:30:00Z", 1299.5e6, repeat_count=1, mode="bistatic_tx")
    assert len(bi.chunks) == 8 and bi.chunks[1].tx_start - bi.chunks[0].tx_stop >= 240 - 1e-6
    assert bi.t_end - bi.t_start < 65 * 60                    # no echo wait: shorter than monostatic
    pe = replace(EveParams.variant_a(), n_frames=6, pilot_frames=2)
    eme = S.build_schedule("EME", "moon", moon_model(), "2026-10-24T05:00:00Z", 1296e6, params=pe,
                           repeat_count=2, chunk_s=None, rtt_guard_s=0.1, t_off_min_s=0.0)
    assert all(c.tx_stop - c.tx_start <= 2.5 for c in eme.chunks)       # <= Moon round trip (4.6)
    assert eme.messages_complete() == 2
    for a, b in zip(eme.chunks, eme.chunks[1:]):
        assert b.tx_start >= a.rx_stop - 1e-6


# ---- sync ---------------------------------------------------------------------------------------
def _pilot_scene(cn0, n_frames, pilot_frames, delay, f_off, seed=5):
    p = replace(EveParams.variant_a(), n_frames=n_frames, pilot_frames=pilot_frames)
    _, _, syms = modem.encode_message(TEXT, p)
    fm = modem.FrameMap(syms, p, on_windows=[(0, p.n_frames_msg - 1)], pilot=True)
    x = modem.ToneSynthesizer(fm, p.modem_rate, p, include_if=False).generate(p.n_frames_msg * p.n_fft)
    x = np.concatenate([x, np.zeros(3 * p.n_fft, dtype=x.dtype)])
    y = channel.StreamChannel(p, cn0, f_offset_hz=f_off, delay_samples=delay, seed=seed).apply(x)
    return p, syms, fm, y


def test_mix_is_phase_continuous_and_exact():
    p = EveParams.variant_a()
    x = np.ones(3 * p.n_fft, dtype=np.complex128)
    y1, ph = sync.mix(x[:p.n_fft], p.modem_rate, lambda t: np.full(t.size, 100.0))
    y2, _ = sync.mix(x[p.n_fft:], p.modem_rate, lambda t: np.full(t.size, 100.0), t0=p.t_frame, phase0=ph)
    y = np.concatenate([y1, y2])
    f = np.angle(y[1:] * np.conj(y[:-1])) / (2 * np.pi) * p.modem_rate
    assert np.allclose(f, -100.0, atol=1e-6)                  # sign -1 removes +100 Hz
    tone = np.exp(2j * np.pi * 100.0 * np.arange(p.n_fft) / p.modem_rate)
    z = sync.shift_hz(tone, p.modem_rate, -100.0)              # remove a +100 Hz offset
    assert np.allclose(np.angle(z[1:] * np.conj(z[:-1])), 0.0, atol=1e-6)


def test_pilot_frequency_presence_and_epoch():
    # EME strength: frequency to R_bw/8, presence loud, whole-frame epoch error found
    n = 16384
    p, syms, fm, y = _pilot_scene(14.0, 12, 8, 2 * n, 1.3)
    det = sync.PilotDetector(p, max_frames=3)
    est = det.search(y, 0, first_symbol=syms[0])
    assert est.detected and est.contrast > 8
    assert est.frame_shift == 2 and est.sample_offset == 2 * n
    assert abs(est.f_offset_hz - 1.3) <= p.r_bw / 8 + 1e-9
    # moderate strength, 40-frame pilot, no epoch error: frequency still good
    p, syms, fm, y = _pilot_scene(4.0, 60, 40, 0, -2.2)
    est = sync.PilotDetector(p).search(y, 0, first_symbol=syms[0])
    # frequency and presence are solid; the whole-frame epoch check is only a ~1 sigma
    # decision between adjacent shifts at this strength (timing lives in the tone edges)
    assert est.detected and abs(est.frame_shift) <= 1 and abs(est.f_offset_hz + 2.2) <= p.r_bw / 4
    # no signal at all: not detected
    p2 = replace(EveParams.variant_a(), n_frames=12, pilot_frames=8)
    noise = channel.StreamChannel(p2, 0.0, seed=1).apply(np.zeros(14 * p2.n_fft, dtype=np.complex64))
    assert not sync.PilotDetector(p2).search(noise, 0).detected


def test_window_receiver_with_pilot_correction():
    n = 16384
    p, syms, fm, y = _pilot_scene(13.0, 12, 8, n, 2.0)       # one frame late, 2 Hz high (0.7 bin)
    acc = modem.SymbolAccumulator(p, fm)
    r = sync.WindowReceiver(p, fm, acc).process(y, 0)
    assert r.sync is not None and r.sync.detected and r.sync.frame_shift == 1
    assert acc.decode().ok and acc.decode().text == TEXT
    # without correction the 0.7-bin offset would put every tone on a guard bin
    acc2 = modem.SymbolAccumulator(p, fm)
    sync.WindowReceiver(p, fm, acc2, use_pilot=False, track=False).process(y[n:], 0)
    assert not acc2.decode().ok


def test_one_frame_grid_error_is_harmless():
    """Section 4.3: the non-coherent receiver only needs the frame grid to about a frame."""
    p = replace(EveParams.variant_a(), n_frames=30)
    _, _, syms = modem.encode_message(TEXT, p)
    x = modem.synthesize_message(syms, p)
    y = channel.StreamChannel(p, 10.0, seed=3).apply(np.concatenate([x, np.zeros(2 * p.n_fft, dtype=x.dtype)]))
    fm = modem.FrameMap(syms, p)
    for off in (0, p.n_fft // 2, p.n_fft):                   # exact, half a frame, one frame late
        acc = modem.SymbolAccumulator(p, fm)
        acc.add_block(0, modem.FrameBank(p).frames_metric(y[off:]))
        assert acc.decode().ok, off


def test_grid_search_and_tracker():
    p = replace(EveParams.variant_a(), n_frames=12)
    _, _, syms = modem.encode_message(TEXT, p)
    x = modem.synthesize_message(syms, p)
    y = channel.StreamChannel(p, 12.0, delay_samples=2 * p.n_fft, seed=1).apply(
        np.concatenate([x, np.zeros(4 * p.n_fft, dtype=x.dtype)]))
    offs = [k * p.n_fft for k in range(0, 4)]
    best, scores = sync.grid_search(y, p, 0, offs, known_symbols=syms)
    assert best == 2 * p.n_fft
    best_b, _ = sync.grid_search(y, p, 0, offs)
    assert best_b == 2 * p.n_fft
    y3 = channel.StreamChannel(p, 10.0, f_offset_hz=0.7, seed=2).apply(x)
    fm = modem.FrameMap(syms, p)
    acc = modem.SymbolAccumulator(p, fm)
    r = sync.WindowReceiver(p, fm, acc, use_pilot=False, track=True, tracker_block=12).process(y3, 0)
    assert abs(r.f_residual_hz - 0.7) < 0.25 and acc.decode().ok


def test_doppler_precompensation_and_receiver_removal():
    p = replace(EveParams.variant_a(), n_frames=12)
    _, _, syms = modem.encode_message(TEXT, p)
    fD = lambda t: 200.0 + 0.48 * t                            # a conjunction-day ramp
    n_tot = p.n_frames_msg * p.n_fft
    # transmitter pre-compensates (6.5), the channel applies the physical Doppler, the receiver does nothing
    syn = modem.ToneSynthesizer(modem.FrameMap(syms, p), p.modem_rate, p, include_if=False, f_extra=lambda t: -fD(t))
    xt = syn.generate(n_tot)
    yt = channel.StreamChannel(p, 12.0, f_offset_hz=200.0, f_rate_hz_s=0.48, seed=9).apply(xt)
    assert modem.demodulate(yt, p).ok
    # receiver without the transmitter's schedule removes the full Doppler itself
    x = modem.synthesize_message(syms, p)
    yr = channel.StreamChannel(p, 12.0, f_offset_hz=200.0, f_rate_hz_s=0.48, seed=9).apply(x)
    fm = modem.FrameMap(syms, p)
    acc = modem.SymbolAccumulator(p, fm)
    sync.WindowReceiver(p, fm, acc, use_pilot=False).process(yr, 0, doppler_hz=fD)
    assert acc.decode().ok
    assert not modem.demodulate(yr, p).ok                     # and it does not decode uncorrected


def test_repeat_and_combine_across_passes():
    p = replace(EveParams.variant_a(), n_frames=40)
    _, _, syms = modem.encode_message(TEXT, p)
    x = modem.synthesize_message(syms, p)
    accs = []
    for seed in range(4):
        a = modem.SymbolAccumulator(p)
        a.add_block(0, modem.FrameBank(p).frames_metric(channel.StreamChannel(p, 3.0, seed=seed).apply(x)))
        accs.append(a)
    merged = sync.merge_accumulators(accs, p)
    assert merged.repetitions == [0, 1, 2, 3]
    assert merged.decode().ok
    assert sum(a.decode().ok for a in accs) < 4               # single passes at +3 dB-Hz with 40 frames fail


# ---- sigmf ------------------------------------------------------------------------------------
def test_sigmf_roundtrip_and_ori_design_block():
    p = replace(EveParams.variant_a(), n_frames=3)
    _, _, syms = modem.encode_message(TEXT, p)
    fs = 250000.0
    x = modem.ToneSynthesizer(modem.FrameMap(syms, p), fs, p, include_if=True).generate(int(round(p.t_msg * fs)))
    try:
        sigmf_io.write_sigmf(x[:1000], p.modem_rate, Path(tempfile.gettempdir()) / "eve_alias", p, syms, 1e9)
        assert False, "aliasing comb accepted"
    except ValueError:
        pass
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "dses_test"
        meta = sigmf_io.write_sigmf(x, fs, base, p, syms, 1299.5e6, TEXT, schedule={"session_id": "T"})
        assert meta["global"]["ori:design"]["symbols_d"] == APPX_C_SYMBOLS
        assert len(meta["annotations"]) == 11
        rec = sigmf_io.read_sigmf(base)
    assert rec.data_valid and np.array_equal(rec.iq, x) and rec.params == p
    assert rec.symbols == APPX_C_SYMBOLS and rec.schedule == {"session_id": "T"} and rec.text == TEXT
    tones = sigmf_io.symbol_tones_in_file(rec)
    assert all(abs(t - p.tone_freq_if(d)) <= p.r_bw for t, d in zip(tones, syms))
    # an ORI-style block as eve_tx_sigmf.py writes it (no DSES keys)
    ori = {"M": 4096, "bits_per_symbol": 12, "n_symbols": 11, "R_bw_hz": 2.87, "tone_spacing_hz": 5.74,
           "t_sym_s": 2.0, "t_sym_design_s": 164.794, "payload_text": TEXT, "symbols_d": APPX_C_SYMBOLS,
           "freq_offset_hz": 25000.0}
    q = sigmf_io.params_from_ori_design(ori)
    assert q.n_frames == 6 and q.f_if == 25000.0 and q.r_bw == 2.87 and q.tone_bin_step == 2
    full = dict(ori, t_sym_s=164.794)
    assert sigmf_io.params_from_ori_design(full).n_frames == 473


if __name__ == "__main__":
    import time
    names = [n for n in dir() if n.startswith("test_")]
    failed = 0
    for n in names:
        t = time.time()
        try:
            globals()[n]()
            print(f"PASS {n} ({time.time() - t:.1f}s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {n}: {e!r}")
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
