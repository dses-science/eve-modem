"""The amateur bands an on-air run may use (Rick, 2026-10-23: the Moon and Venus modes must
not accept a dial frequency outside the legal ham bands).

US amateur allocations within the B210's 70 MHz to 6 GHz tuning range, in MHz. The comb
occupies f_IF to f_IF + M x spacing above the dial (25 to 48.5 kHz for Variant A, 25 to
37.3 kHz for Variant B), so the whole comb, not only the dial, must lie inside a band.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .params import EveParams

# (low, high) in MHz; 47 CFR 97.301 for the bands a B210 reaches
US_AMATEUR_BANDS_MHZ: List[Tuple[float, float, str]] = [
    (144.0, 148.0, "2 m"),
    (222.0, 225.0, "1.25 m"),
    (420.0, 450.0, "70 cm"),
    (902.0, 928.0, "33 cm"),
    (1240.0, 1300.0, "23 cm"),
    (2300.0, 2310.0, "13 cm (lower)"),
    (2390.0, 2450.0, "13 cm"),
    (3300.0, 3500.0, "9 cm"),
    (5650.0, 5925.0, "5 cm"),
]


def comb_span_hz(f_dial_hz: float, params: EveParams) -> Tuple[float, float]:
    """The comb's lowest and highest tone frequency on the air (Hz) for a dial frequency."""
    lo = float(f_dial_hz) + params.f_if
    return lo, lo + params.bandwidth


def band_of(f_dial_hz: float, params: EveParams) -> Optional[str]:
    """The band name when the dial and the whole comb above it lie inside one US amateur
    band, else None. The dial itself counts too: the B210 parks its LO 300 kHz below it and
    nothing of ours belongs below the band edge."""
    lo, hi = comb_span_hz(f_dial_hz, params)
    for b_lo, b_hi, name in US_AMATEUR_BANDS_MHZ:
        if b_lo * 1e6 <= float(f_dial_hz) and hi <= b_hi * 1e6:
            return name
    return None


def check_on_air(f_dial_hz: float, params: EveParams) -> Optional[str]:
    """None when the comb is inside a US amateur band; otherwise the refusal text with the
    comb's span and the bands near the dial."""
    name = band_of(f_dial_hz, params)
    if name is not None:
        return None
    lo, hi = comb_span_hz(f_dial_hz, params)
    near = [f"{b_lo:.0f}-{b_hi:.0f} MHz ({n})" for b_lo, b_hi, n in US_AMATEUR_BANDS_MHZ
            if abs(0.5 * (b_lo + b_hi) * 1e6 - f_dial_hz) < 400e6] or \
           [f"{b_lo:.0f}-{b_hi:.0f} MHz ({n})" for b_lo, b_hi, n in US_AMATEUR_BANDS_MHZ]
    return (f"dial {f_dial_hz / 1e6:.4f} MHz puts the comb at {lo / 1e6:.4f}-{hi / 1e6:.4f} MHz, outside the US amateur "
            f"bands; the dial and the whole comb must lie inside one: {', '.join(near)}. The dial must be at least "
            f"{(params.f_if + params.bandwidth) / 1e3:.1f} kHz below the band's top edge")


def dial_limits_mhz(params: EveParams) -> List[Tuple[float, float, str]]:
    """Per band, the dial frequencies (MHz) that keep the whole comb inside it."""
    top = (params.f_if + params.bandwidth) / 1e6
    return [(b_lo, round(b_hi - top, 4), name) for b_lo, b_hi, name in US_AMATEUR_BANDS_MHZ]
