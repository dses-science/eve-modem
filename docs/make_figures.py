#!/usr/bin/env python3
"""Figures for the DSES EVE Modem Design and ICD document.

Run with the system Python 3.12 (astropy + matplotlib present):
    python make_figures.py
Writes PNGs into ./figures/ at 300 dpi, ~6.6 in wide (DSES house rule).
"""
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

OUT = Path(__file__).resolve().parent / "figures"
OUT.mkdir(exist_ok=True)
BLUE, VERM, TEAL = "#0072B2", "#D55E00", "#009E73"   # validated data trio
GREY = "#555555"
plt.rcParams.update({"font.family": "sans-serif",
                     "font.sans-serif": ["Myriad Pro", "Segoe UI", "Arial"],
                     "font.size": 9, "axes.grid": True, "grid.color": "#DDDDDD",
                     "grid.linewidth": 0.6, "axes.edgecolor": GREY,
                     "axes.labelcolor": "#1A1A1A"})

# --- waveform constants (Python_Implementation conventions) ------------------
R_BW = 2.87            # Hz, FFT bin = frame rate
N_FRAMES = 473         # frames per symbol
N_SYM = 11
T_FRAME = 1 / R_BW
T_SYM = N_FRAMES * T_FRAME          # 164.808 s
T_MSG = N_SYM * T_SYM               # 1812.9 s
RTT = 272.0                         # s, Earth-Venus round trip 2026-10-24 (0.2727 AU)
PA_ON_MAX, PA_OFF_MIN = 300.0, 240.0


def fig_timeline():
    """Monostatic chunked timeline: TX chunks, echo return windows, frame grid."""
    t_on = 240.0
    cycle = t_on + max(RTT, PA_OFF_MIN)
    fig, ax = plt.subplots(figsize=(6.6, 2.6))
    n = 4
    for i in range(n):
        t0 = i * cycle
        ax.add_patch(Rectangle((t0 / 60, 1.05), t_on / 60, 0.8, color=VERM, alpha=0.85))
        ax.add_patch(Rectangle(((t0 + RTT) / 60, 0.05), t_on / 60, 0.8, color=BLUE, alpha=0.85))
        ax.text((t0 + t_on / 2) / 60, 1.45, f"TX chunk {i}", ha="center", va="center",
                color="white", fontsize=8, fontweight="bold")
        ax.text((t0 + RTT + t_on / 2) / 60, 0.45, f"echo {i}", ha="center", va="center",
                color="white", fontsize=8, fontweight="bold")
    # symbol boundaries on the frame grid (continuous symbol time, not wall time)
    ax.annotate("", xy=((RTT) / 60, 1.0), xytext=(0, 1.0),
                arrowprops=dict(arrowstyle="<->", color=GREY, lw=0.8))
    ax.text(RTT / 120, 0.93, f"round trip {RTT:.0f} s", ha="center", va="top", color=GREY, fontsize=8)
    ax.set_yticks([0.45, 1.45]); ax.set_yticklabels(["receive", "transmit"])
    ax.set_ylim(0, 2.0); ax.set_xlim(0, (n * cycle) / 60)
    ax.set_xlabel("wall-clock minutes from schedule epoch")
    ax.set_title(f"Monostatic schedule: {t_on/60:.0f} min chunks, listen for the echo while the PA cools "
                 f"(cycle {cycle/60:.1f} min, duty {100*t_on/cycle:.0f} %)", fontsize=9)
    ax.grid(axis="y", visible=False)
    fig.tight_layout(); fig.savefig(OUT / "fig_timeline.png", dpi=300); plt.close(fig)


def fig_chunk_tradeoff():
    """Message wall-clock time and duty cycle vs chunk length."""
    t_on = np.arange(30, 301, 5.0)
    t_off = np.maximum(RTT, PA_OFF_MIN)
    n_chunks = np.ceil(T_MSG / t_on)
    total = n_chunks * (t_on + t_off) - t_off
    duty = t_on / (t_on + t_off)
    fig, ax = plt.subplots(figsize=(6.6, 3.0))
    ax.plot(t_on / 60, total / 60, color=BLUE, lw=2, label="wall-clock minutes per 30.2 min message")
    ax2 = ax.twinx()
    ax2.plot(t_on / 60, 100 * duty, color=VERM, lw=2, ls="--", label="transmit duty cycle (%)")
    ax2.set_ylabel("duty cycle (%)", color=VERM); ax2.grid(False)
    ax.axvline(RTT / 60, color=GREY, lw=0.8, ls=":")
    ax.text(RTT / 60 + 0.05, total.max() / 60 * 0.95, "chunk = round trip\n(own echo would\noverlap TX)",
            fontsize=7.5, color=GREY, va="top")
    ax.axvspan(PA_ON_MAX / 60, 5.5, color="#EEEEEE", zorder=0)
    ax.text(5.05, total.max() / 60 * 0.6, "PA limit\n5 min on", fontsize=7.5, color=GREY)
    for tv in (60, 120, 180, 240, 270):
        i = np.argmin(abs(t_on - tv))
        ax.annotate(f"{total[i]/60:.0f} min", (t_on[i] / 60, total[i] / 60),
                    textcoords="offset points", xytext=(6, 6), fontsize=7.5, color=BLUE)
    ax.set_xlabel("transmit chunk length (minutes)")
    ax.set_ylabel("minutes to send one message", color=BLUE)
    ax.set_xlim(0.5, 5.5)
    ax.set_title("Chunk length trade: one 1812.9 s message, 272 s round trip, PA 5 on / 4 off", fontsize=9)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper center", fontsize=7.5, frameon=False)
    fig.tight_layout(); fig.savefig(OUT / "fig_chunk_tradeoff.png", dpi=300); plt.close(fig)


def fig_doppler():
    """Venus elevation and two-way Doppler at Haswell on 2026-10-24 (astropy builtin ephemeris)."""
    from astropy.time import Time
    from astropy.coordinates import get_body, EarthLocation, AltAz, solar_system_ephemeris
    import astropy.units as u
    site = EarthLocation(lat=38.380833 * u.deg, lon=-103.156111 * u.deg, height=1311 * u.m)
    c = 299792.458; f0 = 2304e6
    hours = np.arange(13.0, 23.51, 0.25)
    alt, dop, rate = [], [], []
    with solar_system_ephemeris.set("builtin"):
        t0 = Time("2026-10-24T00:00:00")
        def rr(t):
            dt = 1.0 * u.s
            return (get_body("venus", t + dt, site).distance.to(u.km).value
                    - get_body("venus", t - dt, site).distance.to(u.km).value) / 2.0
        for h in hours:
            t = t0 + h * u.hour
            alt.append(get_body("venus", t, site).transform_to(AltAz(obstime=t, location=site)).alt.deg)
            r = rr(t); d = -2 * f0 * r / c
            d2 = -2 * f0 * rr(t + 60 * u.s) / c
            dop.append(d); rate.append((d2 - d) / 60.0)
    alt, dop, rate = map(np.array, (alt, dop, rate))
    fig, ax = plt.subplots(figsize=(6.6, 3.0))
    ax.plot(hours, dop / 1e3, color=BLUE, lw=2, label="two-way Doppler, 2304 MHz (kHz)")
    ax.plot(hours, dop / 1e3 * (1299.5 / 2304), color=BLUE, lw=1.4, ls=":", label="two-way Doppler, 1299.5 MHz (kHz)")
    ax.set_ylabel("two-way Doppler (kHz)", color=BLUE)
    ax2 = ax.twinx(); ax2.grid(False)
    ax2.plot(hours, rate, color=VERM, lw=2, ls="--", label="Doppler rate, 2304 MHz (Hz/s)")
    ax2.plot(hours, alt / 100, color=TEAL, lw=1.5, ls="-.", label="elevation (deg / 100)")
    ax2.set_ylabel("rate (Hz/s)   |   elevation/100", color=GREY)
    up = alt > 20
    ax.axvspan(hours[up].min(), hours[up].max(), color="#EEF5EE", zorder=0)
    ax.text(hours[up].min() + 0.1, (dop / 1e3).max() * 0.9, "Venus above 20°", fontsize=7.5, color=GREY)
    ax.set_xlabel("UTC hour, 2026-10-24 (Haswell)")
    ax.set_title("Two-way Doppler at Haswell, 2026-10-24: rate up to 0.48 Hz/s at 2304 MHz, "
                 "0.27 Hz/s at 1299.5 MHz", fontsize=9)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="lower left", fontsize=7.5, frameon=False)
    fig.tight_layout(); fig.savefig(OUT / "fig_doppler.png", dpi=300); plt.close(fig)
    return hours, alt, dop, rate


if __name__ == "__main__":
    fig_timeline()
    fig_chunk_tradeoff()
    h, a, d, r = fig_doppler()
    up = a > 20
    print(f"Venus >20 deg at Haswell 2026-10-24: {h[up].min():.2f}-{h[up].max():.2f} UTC; "
          f"max elev {a.max():.1f}; Doppler {d.min():.0f}..{d.max():.0f} Hz; "
          f"rate extreme {r.min():.3f} Hz/s")
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))


def fig_blocks():
    """System block diagram: schedule-driven transmit and receive chains on one B210."""
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
    fig, ax = plt.subplots(figsize=(6.6, 4.2))
    ax.set_xlim(0, 100); ax.set_ylim(0, 64); ax.axis("off")

    def box(x, y, w, h, text, fc="#FFFFFF", ec=BLUE, fs=7.2, bold=False):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.3,rounding_size=1.2",
                                    fc=fc, ec=ec, lw=1.0))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
                fontweight="bold" if bold else "normal", color="#1A1A1A")

    def arrow(x0, y0, x1, y1, color=GREY, ls="-"):
        ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=9,
                                     color=color, lw=0.9, ls=ls, shrinkA=1, shrinkB=1))

    def seg(x0, y0, x1, y1, color=GREY):
        ax.plot([x0, x1], [y0, y1], color=color, lw=0.9, solid_capstyle="round")

    # top row: inputs
    box(2, 55, 22, 7, "message text\n+ CRC-16 -> BCH(127,106)\n-> 11 symbols", fs=6.8)
    box(28, 55, 26, 7, "schedule.json\nepoch, chunks, symbols,\nrepeat, pilot, Doppler model",
        fc="#E8F0F4", bold=True)
    box(58, 55, 18, 7, "ephemeris\nHorizons CSV /\nastropy DE440s", fs=6.8)
    box(80, 55, 18, 7, "GPS clock 10 MHz + 1 PPS\n(Leo Bodnar, USB)", fs=6.8)
    arrow(24, 58.5, 28, 58.5); arrow(58, 58.5, 54, 58.5)
    # middle row: radio and transmit chain
    box(2, 40, 22, 8, "TX NCO source\nradio rate, phase-continuous,\nIF offset, chunk gating,\n"
                      "-f_D(t) pre-compensation", fs=6.5)
    box(30, 40, 16, 8, "USRP B210\nTX/RX A  ->\nRX2 A  <-\nGPIO keys unused", fc="#FFF4E8",
        ec=VERM, bold=True, fs=6.8)
    box(51, 40, 10, 8, "20 W\ndriver", fs=6.8)
    box(65, 40, 19, 8, "feed, 1299.5 MHz\n1200 W SSPA\nmounted at the feed", fs=6.8)
    box(88, 42, 10, 4, "Venus", fc="#EEF5EE", ec=TEAL, bold=True)
    arrow(30, 55, 13, 48)                       # schedule -> TX NCO source
    arrow(24, 45, 30, 45)                       # TX samples -> B210
    arrow(46, 45.5, 51, 45.5)                   # RF drive -> driver
    arrow(61, 44, 65, 44); arrow(84, 44, 88, 44)
    seg(89, 55, 89, 51); seg(89, 51, 38, 51); arrow(38, 51, 38, 48)   # GPS -> B210 REF/PPS
    ax.text(63, 51.8, "REF IN / PPS IN", fontsize=6, color=GREY, ha="center")
    # receive chain and keying: the USB relay board drives both key lines, timed by the modem
    # (the B210 GPIO carries the same keys in parallel, unused at DSES; Rick, 2026-09-28)
    box(72, 28, 12, 6, "LNA", fs=6.8)
    arrow(92, 42, 80, 34)                       # Venus echo -> LNA
    seg(78, 28, 78, 24); seg(78, 24, 44, 24); arrow(44, 24, 44, 40)   # LNA -> B210 RX2
    box(46, 27, 16, 7, "USB dual-relay board\nTX key, LNA key\n(timed by the modem)",
        fc="#F7F7F7", fs=6.2)
    arrow(54, 34, 54, 40, ls="--")              # TX key -> amplifier chain
    ax.text(55.2, 36.6, "TX key", fontsize=6, color=GREY, ha="left")
    arrow(62, 31, 72, 31, ls="--")              # LNA key -> LNA
    ax.text(67, 32.3, "LNA key", fontsize=6, color=GREY, ha="center")
    box(26, 26, 14, 8, "EveRxSink\nmix down IF, RX NCO\n(+f_D residual),\ndecimate /32", fs=6.2)
    arrow(34, 40, 34, 34)
    box(2, 26, 20, 8, "raw IQ archive\n.eve.iq + JSON sidecar\n(47,022.08 S/s)", fc="#F7F7F7", fs=6.5)
    arrow(26, 30, 22, 30)
    box(2, 12, 22, 8, "frame FFT bank\n16,384 pt, 2.87 Hz bins\nper-symbol accumulators", fs=6.5)
    arrow(12, 26, 12, 20)
    box(30, 12, 16, 8, "sync (from schedule)\npilot, frame-grid search,\nresidual tracker,\n"
                       "repeat-and-combine", fs=6.0)
    arrow(24, 16, 30, 16)
    box(52, 12, 14, 8, "decide\nargmax of 4096,\nunpack MSB-first", fs=6.5)
    arrow(46, 16, 52, 16)
    box(72, 12, 12, 8, "BCH decode\n+ CRC check", fs=6.8)
    arrow(66, 16, 72, 16)
    box(88, 12, 10, 8, "session\nreport", fc="#E8F0F4", bold=True, fs=6.8)
    arrow(84, 16, 88, 16)
    ax.text(3, 6.0, "One B210, one schedule, one Doppler model shared by transmit and receive. "
                    "The schedule file is the contract with any partner station.\n"
                    "The sequencer is the modem: it times the TX and LNA key lines through the "
                    "USB dual-relay board. The B210 GPIO pins carry the same keys in\n"
                    "parallel, for a station with an external sequencer at the feed as ORI has, "
                    "but DSES does not use them.",
            fontsize=6.3, color=GREY, va="top", linespacing=1.35)
    fig.tight_layout(); fig.savefig(OUT / "fig_blocks.png", dpi=300); plt.close(fig)


fig_blocks()


def fig_variants():
    """Frame error rate vs C/N0: ORI waveform (2.87 Hz bins) vs the DSES 23 cm
    matched-bin variant (1.5 Hz), AWGN chi-square model as in ORI's link check."""
    rng = np.random.default_rng(0)
    M, NSYM = 4096, 11

    def ser(cn0_db, rbw, nfr, trials=1500):
        g = 10 ** (cn0_db / 10) / rbw
        df = 2 * nfr
        zt = 0.5 * rng.noncentral_chisquare(df, 2 * nfr * g, size=trials)
        errs = 0
        for i in range(0, trials, 300):
            n = min(300, trials - i)
            zf = 0.5 * rng.chisquare(df, size=(n, M - 1)).max(axis=1)
            errs += int((zf >= zt[i:i + n]).sum())
        return errs / trials

    cn0 = np.arange(-5.0, 1.51, 0.5)
    configs = [("A: ORI, 2.87 Hz bins, 473 frames (165 s symbol)", 2.87, 473, BLUE, "-"),
               ("B: DSES 23 cm, 1.5 Hz bins, 247 frames (165 s symbol)", 1.5, 247, VERM, "--"),
               ("B-long: 1.5 Hz bins, 473 frames (315 s symbol)", 1.5, 473, TEAL, "-.")]
    fig, ax = plt.subplots(figsize=(6.6, 3.2))
    for label, r, n, col, ls in configs:
        fer = [1 - (1 - ser(c, r, n)) ** NSYM for c in cn0]
        ax.plot(cn0, fer, color=col, ls=ls, lw=2, label=label)
    ax.axhline(0.1, color=GREY, lw=0.8, ls=":")
    ax.text(-4.9, 0.12, "10 % frame error rate", fontsize=7.5, color=GREY)
    ax.axvspan(-5.4, -4.2, color="#F3E6DE", zorder=0)   # 1200 W: date-resolved to static albedo
    ax.text(-4.8, 0.62, "DSES 23 cm\nmonostatic\n1200 W", fontsize=7.5, color=VERM, ha="center")
    ax.set_xlabel("C/N0 (dB-Hz)"); ax.set_ylabel("frame error rate (11 symbols)")
    ax.set_ylim(0, 1.02); ax.set_xlim(cn0.min(), cn0.max())
    ax.set_title("Matched 1.5 Hz bins buy about 1.1 dB at the same symbol length (AWGN model)", fontsize=9)
    ax.legend(loc="upper right", fontsize=7.5, frameon=False)
    fig.tight_layout(); fig.savefig(OUT / "fig_variants.png", dpi=300); plt.close(fig)


fig_variants()
