"""DSES Earth-Venus-Earth modem.

Implements the ORI EVE waveform (Pete Wyckoff KA3WCA, "Venus Bounce Transmitter
Spiral #2", GPL-3.0) as specified in the DSES Design Description and ICD
(docs/DSES_EVE_Modem_Design_and_ICD.md). Pure NumPy/SciPy core; GNU Radio only in
gr_blocks.py and radio.py.

Supported by a grant from Amateur Radio Digital Communications. (ARDC's 2026 grant to the Deep
Space Exploration Society for EVE-26; Section 10 of the grant agreement.)
"""
__version__ = "1.0.8"

from .params import EveParams  # noqa: F401


def log_dir():
    """Where the program's own logs go (app.log from the launcher, fault.log from the
    fault handler): Windows %LOCALAPPDATA%\\DSES\\EVE_Modem, macOS ~/Library/Logs/DSES_EVE_Modem,
    Linux $XDG_STATE_HOME/dses-eve-modem (default ~/.local/state). The shell launchers use
    the same folders."""
    import os
    import sys
    from pathlib import Path as _P
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(_P.home())
        d = _P(base) / "DSES" / "EVE_Modem"
    elif sys.platform == "darwin":
        d = _P.home() / "Library" / "Logs" / "DSES_EVE_Modem"
    else:
        d = _P(os.environ.get("XDG_STATE_HOME") or (_P.home() / ".local" / "state")) / "dses-eve-modem"
    d.mkdir(parents=True, exist_ok=True)
    return d
