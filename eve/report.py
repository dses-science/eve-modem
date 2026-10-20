"""Session report PDF (design document 8.4): what happened, what was decoded, with what
margin, and the timeline, in one document the operator can read in the application when
a run finishes. Built with matplotlib alone (no pyplot: safe inside the Qt application).

    from eve.report import write_report
    pdf = write_report(schedule, session_report_dict, decode_summary, window_results, out_pdf,
                       extra={"GPS clock": "...", "Radio": "..."})
"""
from __future__ import annotations

import textwrap
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure

from . import __version__
from .doppler import iso_utc
from .schedule import Schedule

TEAL, NAVY, GOLD, GREEN, RED, GREY = "#156082", "#0A2F40", "#B86A18", "#1e7d3a", "#c0392b", "#7f8c8d"
PAGE = (8.5, 11.0)


def _page(title: str, subtitle: str) -> Figure:
    """A new letter-size Figure with the report banner: series line, `title`, `subtitle`, a
    teal rule, and the version and generation time in the footer."""
    fig = Figure(figsize=PAGE, dpi=100)
    fig.text(0.06, 0.965, "DSES Earth-Venus-Earth modem — session report", fontsize=9, color=GREY)
    fig.text(0.06, 0.935, title, fontsize=15, fontweight="bold", color=NAVY)
    fig.text(0.06, 0.912, subtitle, fontsize=9, color=GREY)
    fig.add_artist(__import__("matplotlib.lines", fromlist=["Line2D"]).Line2D([0.06, 0.94], [0.902, 0.902], color=TEAL, lw=1.5))
    fig.text(0.94, 0.03, f"dses-eve {__version__}  ·  generated {iso_utc(time.time(), 0)}", fontsize=7, color=GREY, ha="right")
    return fig


def tx_events_text(counts: Dict) -> str:
    """The operator warning for the transmit event counts (gr_blocks.TxEventCounter: underflows,
    late packets, sequence errors), or '' when there were none. One text for the Run tab, the
    log, the notice box and the report. Found 2026-10-20 on the Haswell Raspberry Pi 5: Variant
    A's 1.5 MS/s underran the transmitter, 36,469 late packets, symbols 1-10 never sent."""
    u = int(counts.get("underflow", 0))
    late = int(counts.get("late", 0))
    seq = int(counts.get("seq_error", 0))
    if u + late + seq == 0:
        return ""
    return (f"TRANSMITTER UNDERRAN: {u} underflows, {late} late packets, {seq} sequence errors. The host could not "
            "deliver the transmit samples in time, so chunks went out late or empty and the receiver saw noise. "
            "Variant A streams 1.5 MS/s, Variant B 786 kS/s: the Raspberry Pi 5 manages B, not A. On any PC close "
            "other programs and use a direct USB 3 port.")


def _mono(fig: Figure, x: float, y: float, lines: List[str], size: float = 8.0, dy: float = 0.0148) -> float:
    """Write `lines` in monospace from (x, y) downward (figure fractions), `dy` per line;
    returns the y below the last line."""
    for ln in lines:
        fig.text(x, y, ln, fontsize=size, family="monospace", va="top")
        y -= dy
    return y


def _fmt_rx(rx: Dict) -> List[str]:
    """One line summarizing the session's receive statistics dict (samples, windows filed,
    gaps)."""
    if not rx:
        return ["(no receive summary)"]
    n_files = len(rx.get("files", {}) or {})
    return [f"samples in {rx.get('samples_in', 0):,}  written {rx.get('samples_written', 0):,}  "
            f"windows filed {n_files}  gap events {rx.get('gap_events', 0)}  gap samples {rx.get('gap_samples', 0):,}"]


def write_report(sched: Schedule, rep: Dict, summary: Optional[Dict], windows: List[Dict], out_pdf,
                 extra: Optional[Dict[str, str]] = None) -> Path:
    """rep: the session JSON as a dict (SessionReport fields + schedule + options).
    summary: eve.decode.summarize() output, or None if the offline decode did not run.
    windows: eve.decode.decode_archive() per-window results (may be empty).
    Writes `out_pdf` (folders created) and returns its Path. Page 1: the verdict, the
    session lines, the extra and radio lines, the options, and the per-symbol decisions
    table; page 2: the margin per symbol, the chunk timeline with key events, and the
    per-window synchronization; then the window lines and key events, 52 per page."""
    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    p = sched.params
    comb = (summary or {}).get("combined")
    verdict = ("DECODED" if comb and comb["ok"] else ("NOT DECODED" if comb else "NO OFFLINE DECODE"))
    if rep.get("aborted"):
        verdict += "  (session aborted: " + str(rep.get("abort_reason", "")) + ")"
    vcolor = GREEN if comb and comb["ok"] and not rep.get("aborted") else RED
    t_sym = float(p.t_sym)
    t_msg = t_sym * p.n_sym

    with PdfPages(str(out_pdf)) as pdf:
        # ---------------- page 1: summary
        fig = _page(f"{sched.session_id}", f"{sched.target} · {sched.mode} · {sched.f_dial_hz / 1e6:.4f} MHz · "
                                            f"Variant {p.variant} · message '{sched.text}' · repeat {sched.repeat_count}")
        fig.text(0.06, 0.865, verdict, fontsize=20, fontweight="bold", color=vcolor)
        if comb:
            fig.text(0.06, 0.838, f"offline decode (decision of record): text '{comb['text']}'   BCH corrected {comb['bits_corrected']} bits   "
                                  f"passes combined {len((summary or {}).get('passes', []))}   frames filed {(summary or {}).get('frames_seen', 0)}",
                     fontsize=9, family="monospace")
        y = 0.805
        lines = [
            f"started  {rep.get('started_utc', '')}    finished {rep.get('finished_utc', '')}",
            f"schedule {iso_utc(sched.t_start, 0)} .. {iso_utc(sched.t_end, 0)}   chunks {len(sched.chunks)}   "
            f"frames wanted {sched.n_frames_wanted}   keyed {rep.get('chunks_keyed', 0)}   frames sent {rep.get('frames_sent', 0)}",
            f"symbol {p.n_frames} frames = {t_sym:.1f} s ({'full length' if p.n_frames >= 400 else 'TEST length'}); "
            f"message {t_msg / 60:.1f} min per pass; bins {p.r_bw:.2f} Hz; pilot {'on' if sched.pilot_enabled else 'off'}",
        ]
        y = _mono(fig, 0.06, y, lines)
        y = _mono(fig, 0.06, y, _fmt_rx(rep.get("rx", {})))
        live = rep.get("live_decode") or {}
        if live:
            y = _mono(fig, 0.06, y, [f"live decode (operator view): {'ok' if live.get('ok') else 'fail'} '{live.get('text', '')}' "
                                     f"frames {live.get('frames', 0)} passes {live.get('repetitions', [])}"])
        y -= 0.006
        for k, v in (extra or {}).items():
            for i, ln in enumerate(textwrap.wrap(f"{k}: {v}", 118) or [f"{k}:"]):
                y = _mono(fig, 0.06, y, [ln if i == 0 else "    " + ln])
        if rep.get("radio") and "Radio" not in (extra or {}):
            for i, ln in enumerate(textwrap.wrap(f"Radio: {rep['radio']}", 118)):
                y = _mono(fig, 0.06, y, [ln if i == 0 else "    " + ln])
        warn = tx_events_text(rep.get("tx_events") or {})
        if warn:
            for i, ln in enumerate(textwrap.wrap(warn, 118)):
                fig.text(0.06, y, ln if i == 0 else "    " + ln, fontsize=8, family="monospace", va="top",
                         color=RED, fontweight="bold")
                y -= 0.0148
        opts = rep.get("options", {})
        if opts:
            y = _mono(fig, 0.06, y, [f"options: PA limits {'on' if opts.get('pa_in_chain') else 'off'}, TX pre-compensation "
                                     f"{'on' if opts.get('tx_precompensate') else 'off'}, RX Doppler removal {'on' if opts.get('rx_doppler_removal') else 'off'}, "
                                     f"archive {opts.get('out_dir', '')}"])
        # decisions table
        if comb:
            y -= 0.01
            fig.text(0.06, y, "Symbols (combined over passes)", fontsize=10, fontweight="bold", color=NAVY, va="top")
            y -= 0.02
            ax = fig.add_axes([0.06, max(0.08, y - 0.30), 0.88, 0.30])
            ax.axis("off")
            rows = []
            colors = []
            for m in range(p.n_sym):
                exp, dec = comb["expected"][m], comb["symbols"][m]
                good = exp == dec
                per_pass = "  ".join(f"{pp['symbols'][m]:4d}" for pp in (summary or {}).get("passes", []))
                rows.append([str(m), str(exp), str(dec), f"{comb['margins_db'][m]:.1f}", per_pass, "ok" if good else "WRONG"])
                colors.append(["#D6F0DD" if good else "#F7D9D3"] * 6)
            tbl = ax.table(cellText=rows, colLabels=["symbol", "expected", "decided", "margin dB", "per pass", "state"],
                           cellColours=colors, loc="upper center", cellLoc="center",
                           colWidths=[0.08, 0.1, 0.1, 0.12, 0.45, 0.1])
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(8)
            tbl.scale(1, 1.15)
        pdf.savefig(fig)

        # ---------------- page 2: figures
        fig = _page("Margins, timeline, and windows", sched.session_id)
        ax1 = fig.add_axes([0.09, 0.66, 0.85, 0.20])
        if comb:
            mg = np.asarray(comb["margins_db"])
            good = np.asarray(comb["expected"]) == np.asarray(comb["symbols"])
            ax1.bar(np.arange(p.n_sym), mg, color=[GREEN if g else RED for g in good])
            ax1.axhline(0, color=GREY, lw=0.8)
            ax1.set_xlabel("symbol")
            ax1.set_ylabel("margin dB")
            ax1.set_title("decision margin per symbol (combined; green = matches the expected tone)", fontsize=9)
            for m in range(p.n_sym):
                ax1.text(m, mg[m] + 0.3, f"{comb['symbols'][m]}", ha="center", fontsize=7)
        else:
            ax1.text(0.5, 0.5, "no offline decode", ha="center", transform=ax1.transAxes)
            ax1.axis("off")
        # timeline
        ax2 = fig.add_axes([0.09, 0.38, 0.85, 0.20])
        t0 = sched.t_start
        for c in sched.chunks:
            ax2.broken_barh([(c.tx_start - t0, c.tx_stop - c.tx_start)], (0.6, 0.35), color=GOLD)
            ax2.broken_barh([(c.rx_start - t0, c.rx_stop - c.rx_start)], (0.1, 0.35), color=TEAL)
        for e in rep.get("key_events", []):
            if "chunk" in e:
                ax2.axvline(e["t"] - t0, color=RED if e["on"] else GREY, lw=0.5, alpha=0.6)
        ax2.set_yticks([0.275, 0.775])
        ax2.set_yticklabels(["receive", "transmit"])
        ax2.set_xlabel(f"seconds after {iso_utc(t0, 0)}")
        ax2.set_title(f"chunk timeline ({len(sched.chunks)} chunks; thin lines = key events)", fontsize=9)
        ax2.set_ylim(0, 1.05)
        # windows
        ax3 = fig.add_axes([0.09, 0.08, 0.85, 0.22])
        if windows:
            idx = np.arange(len(windows))
            fo = np.array([w["f_offset_hz"] for w in windows])
            det = np.array([w["pilot_detected"] for w in windows])
            ax3.plot(idx, fo, "o-", color=TEAL, ms=4, label="pilot frequency offset (Hz)")
            ax3.plot(idx[~det], fo[~det], "x", color=RED, ms=7, label="pilot not detected")
            ax3.plot(idx, [w["f_residual_hz"] for w in windows], "s--", color=GOLD, ms=3, label="tracker residual (Hz)")
            gaps = [i for i, w in enumerate(windows) if w["gap_events"]]
            for i in gaps:
                ax3.axvline(i, color=RED, lw=0.6, alpha=0.5)
            ax3.set_xlabel("receive window")
            ax3.set_ylabel("Hz")
            ax3.legend(fontsize=7, loc="upper right")
            ax3.set_title(f"per-window synchronization ({sum(det)}/{len(windows)} pilots detected; red lines = windows with sample gaps)", fontsize=9)
        else:
            ax3.text(0.5, 0.5, "no receive windows", ha="center", transform=ax3.transAxes)
            ax3.axis("off")
        pdf.savefig(fig)

        # ---------------- page 3+: window lines and key events
        lines = [w["line"] for w in windows]
        ev = rep.get("key_events", [])
        ev_lines = [f"{iso_utc(e['t'], 3)}  {'KEY DOWN' if e.get('on') else 'key up  '}  chunk {e.get('chunk', '')} {e.get('why', '')}" for e in ev]
        per_page = 52
        chunks = [("Receive windows (offline decode)", lines[i:i + per_page]) for i in range(0, max(len(lines), 1), per_page)]
        chunks += [("Key events", ev_lines[i:i + per_page]) for i in range(0, max(len(ev_lines), 1), per_page)]
        for title, block in chunks:
            fig = _page(title, sched.session_id)
            _mono(fig, 0.06, 0.875, [textwrap.shorten(s, 125) for s in block] or ["(none)"], size=7.0, dy=0.0148)
            pdf.savefig(fig)
    return out_pdf
