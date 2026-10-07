"""The DSES EVE modem application: one window for every kind of run, with remembered
settings, the live operator display, and the session report.

Tabs:
  Setup   - the run mode (software simulation, B210 loopback bench, EME, Venus) and its
            settings; a schedule preview; Start / Abort. Settings persist in an INI file
            (QSettings, %APPDATA%\\DSES\\EVE_Modem.ini on Windows) so the desktop icon comes
            up ready for the last test.
  Run     - the operator display (eve.display.OperatorPanel), re-bound for every run; the
            program does not need restarting between runs.
  Report  - the session report PDF of the run just finished (or any earlier one in the
            archive folder), rendered in the window; open it externally or re-decode.

The command-line tools keep working; this window drives the same Session, EveRadio,
SimRadio, gpsdo preflight, decode, and report code they do.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

import json
import math
import sys
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__, log_dir
from .params import EveParams
from . import doppler as D
from . import schedule as S
from .doppler import iso_utc
import numpy as np

from . import modem
from .display import OperatorPanel, NAVY, TEAL, mono, ABORT_STYLE, wrap_tooltips

MODES = [("sim", "Software simulation (no radio)"),
         ("bench", "Bench loopback (one B210, no antenna)"),
         ("siggen", "Signal generator (bench RF package test)"),
         ("interop", "Interop with a partner station (ORI): transmit only or receive only"),
         ("eme", "EME — Earth-Moon-Earth"),
         ("eve", "EVE — Earth-Venus-Earth")]
PREFIX = {"sim": "SIM", "bench": "BENCH", "siggen": "SIGGEN", "interop": "INTEROP", "eme": "EME", "eve": "EVE"}
TARGET = {"sim": "sim", "bench": "bench", "siggen": "siggen", "interop": "partner", "eme": "moon", "eve": "venus"}
FULL_FRAMES = {"A": 473, "B": 247, "MATLAB": 540}
KEYER_KINDS = {"none": "none", "TX key + LNA (USB relay board + B210 GPIO)": "sequencer"}
_LEGACY_KEYER_KINDS = {"B210 GPIO": "TX key + LNA (USB relay board + B210 GPIO)", "USB relay board": "TX key + LNA (USB relay board + B210 GPIO)"}   # settings from before 2026-09-23

# "Set defaults" for a run mode: every setting the mode cares about goes to the design
# value (the design document's numbers; bench values from the 2026-09-11/12 loopback).
COMMON_DEFAULTS = {"variant": "A", "message": "K0PRT K0PRT", "pilot": True, "n_frames_test": 6, "pilot_frames_test": 2,
                   "clock": "external", "time_host": False, "gpsdo": True, "lo_offset_khz": -300.0, "serial": ""}
MODE_DEFAULTS = {
    "sim": {"f_dial_mhz": 1296.0, "repeat": 1, "full_symbol": False, "sim_cn0": 20.0, "sim_range_km": 375000.0,
            "sim_seed": 1, "bench_chunk_s": 2.4, "bench_t_off_min": 0.5, "lead_s": 12.0},
    "bench": {"f_dial_mhz": 1296.0, "repeat": 2, "full_symbol": False, "tx_gain": 0.0, "rx_gain": 30.0,
              "bench_range_km": 0.15, "bench_chunk_s": 2.4, "bench_t_off_min": 0.5, "lead_s": 15.0},
    "siggen": {"f_dial_mhz": 1296.0, "full_symbol": False, "tx_gain": 0.0, "siggen_signal": "CW", "siggen_offset_khz": 0.0,
               "siggen_spacing_khz": 10.0, "siggen_level_db": -3.0, "siggen_pa": False,
               "siggen_sim": False, "siggen_sweep": False, "siggen_sweep_start_db": 0.0, "siggen_sweep_stop_db": 60.0, "siggen_sweep_step_db": 5.0,
               "siggen_sweep_hold_s": 10.0, "lead_s": 15.0},
    "interop": {"f_dial_mhz": 1296.0, "repeat": 1, "full_symbol": True, "tx_gain": 30.0, "rx_gain": 30.0,
                "interop_chunk_s": 300.0, "interop_search_frames": 30, "sky_start_now": True, "lead_s": 30.0},
    "eme": {"f_dial_mhz": 1296.0, "repeat": 2, "full_symbol": True, "tx_gain": 0.0, "rx_gain": 40.0,
            "eme_chunk_s": 2.4, "eme_rtt_guard": 0.1, "eme_t_off_min": 0.5, "eme_t_on_max": 300.0, "eme_pa": False,
            "sky_mode": "monostatic", "sky_source": "auto", "sky_precomp": True, "sky_rx_doppler": False,
            "sky_start_now": True, "lead_s": 30.0},
    "eve": {"f_dial_mhz": 1299.5, "repeat": 5, "full_symbol": True, "tx_gain": 0.0, "rx_gain": 45.0,
            "sky_chunk_s": 240.0, "sky_rtt_guard": 30.0, "sky_t_on_max": 300.0, "sky_t_off_min": 240.0, "sky_pa": True,
            "sky_mode": "monostatic", "sky_source": "auto", "sky_precomp": True, "sky_rx_doppler": False,
            "sky_start_now": True, "lead_s": 60.0},
}



# ------------------------------------------------------------------------------------------
# operator help: one tooltip per control (shown on hover; also on the row labels)
# ------------------------------------------------------------------------------------------
TIPS: Dict[str, str] = {
    "mode:sim": "No radio. The tone source feeds a software channel (delay + noise) straight into the receiver. "
                "Use it to learn the display, to check a schedule, or to prove the decoder at a chosen C/N0.",
    "mode:bench": "One B210, nothing on the antenna ports. Transmits at low gain and receives its own internal "
                  "TX-to-RX leakage on RX2; proves the radio, the reference, the archive, and the decoder before "
                  "anything goes on the air. Chunks follow the bench settings, not a round trip.",
    "mode:eme": "Moon bounce with the station. Ephemeris from JPL Horizons (astropy fallback), Doppler "
                "pre-compensated, 2.5 s round trip: transmit a chunk, listen for its echo, repeat. The proof test "
                "before Venus (design 4.6). Keying and the amplifier limits apply if a PA is in the chain.",
    "mode:eve": "Venus bounce. 272 s round trip on conjunction day: 240 s transmit chunks, then listen. A full "
                "message is 30.2 minutes per pass; five passes are planned (design 4.1, 4.2). The amplifier limits "
                "are enforced regardless of the schedule.",
    "variant": "A: the ORI air interface (2.87 Hz bins, 473 frames per symbol) - use it whenever another station "
               "must decode us. B: DSES 23 cm monostatic variant (1.5 Hz bins, 247 frames), +1.1 dB, DSES-only.",
    "f_dial_mhz": "Dial frequency in MHz. The 4096-tone comb sits 25 to 48.5 kHz ABOVE it. Venus 2026: 1299.5 "
                  "(23 cm package). Bench: 1296. 13 cm: 2304 or 2400. The LO parks 300 kHz below (LO offset).",
    "message": "Up to 11 ASCII characters (90 bits + CRC-16 -> BCH(127,106) -> 11 symbols of 12 bits). The Venus "
               "message is K0PRT K0PRT, the club call, never a personal call.",
    "repeat": "How many times the message is sent. The receiver adds the passes before deciding, so every extra pass "
              "buys margin (about +3 dB per doubling). Venus plan: 5 passes; bench: 2.",
    "pilot": "Send pilot frames before each pass. The receiver uses them to measure the frequency offset and to "
             "confirm the epoch to a whole frame. Leave on unless a partner's file format forbids it.",
    "full_symbol": "Full length = the air interface: 473 frames per symbol (Variant A), 164.8 s per symbol, "
                   "30.2 min per message pass. Untick for a short TEST symbol that runs in seconds; a test-length "
                   "signal has no on-air meaning and a partner could not decode it.",
    "n_frames_test": "Frames per symbol for a TEST run. Each frame is 0.348 s (Variant A). 6 frames = 2.1 s per symbol, "
                     "23 s per message pass; the bench default.",
    "pilot_frames_test": "Pilot frames per pass for a TEST run (the full-length pilot is 40 frames).",
    "serial": "B210 serial number, or blank for the first B2xx found on USB. The Haswell radio and the bench clone "
              "have different serials; set it when both are plugged in.",
    "tx_gain": "B210 transmit gain, 0 to 89.75 dB. 0 dB for the loopback bench (the leakage is enough). On the air the "
               "driver amplifier's input level sets it; +8 dBm out of the B210 at maximum. Never exceed what the "
               "driver's input pad expects (design 7.1, O4).",
    "rx_gain": "B210 receive gain. Set it so the receiver noise sits 10 to 15 dB above the B210's own floor: watch the "
               "tone strip's background on the Run tab. 30 dB on the bench; 40 to 50 dB behind the LNA.",
    "clock": "external: 10 MHz on REF IN and 1 PPS on PPS IN from the station GPS clock (the standard). gpsdo: an "
             "Ettus GPSDO board inside the B210. internal: the B210's own TCXO, bench only, no lock check, +/-2 ppm.",
    "time_host": "Tick when there is NO 1 PPS into the B210: the epoch then comes from this PC's NTP clock (tens of "
                 "milliseconds, inside the one-frame tolerance). With the Leo Bodnar clock's OUT2 on PPS IN leave "
                 "this unticked so the time is set on a real PPS edge.",
    "gpsdo": "Before opening the radio, program the Leo Bodnar GPS reference clock over USB to the station setting "
             "(OUT1 = 10 MHz at level 1 for REF IN; OUT2 disabled = 1 PPS for PPS IN) and wait for satellite and PLL "
             "lock. The run refuses to start unlocked. Untick only if that clock is not connected to this PC.",
    "lo_offset_khz": "Where the B210's local oscillator parks relative to the dial frequency. -300 kHz keeps its "
                     "leakage far outside the comb in both directions. Leave it unless the radio says the offset "
                     "could not be applied.",
    "archive": "Folder for everything a run produces: the schedule JSON, the ephemeris CSV, the archived receive "
               "windows (.eve.iq + sidecar), the session log JSON, and the report PDF. One folder per campaign.",
    "sim_cn0": "Carrier-to-noise density of the simulated echo in dB-Hz. The Venus design point is about 0 dB-Hz, "
               "which the waveform reaches only at FULL symbol length (473 frames). A short TEST symbol needs far more: "
               "the 6-frame bench length decodes above about +12 dB-Hz (the blue line under Symbol length says what "
               "the current length needs). 20 dB-Hz is an easy check of the machinery.",
    "sim_range_km": "One-way range of the synthetic target. 375,000 km = the Moon's 2.5 s round trip (monostatic "
                    "chunking, transmit then listen). Under 100 km the run is chunked like the bench.",
    "sim_seed": "Random seed of the simulated noise; the same seed repeats a run exactly.",
    "bench_range_km": "Synthetic range for the bench schedule. The B210's leakage path has no delay, so keep this "
                      "tiny (0.15 km) and the receive window lands on the transmission itself.",
    "bench_chunk_s": "Length of each transmit chunk on the bench. 2.4 s mimics the Moon cadence.",
    "bench_t_off_min": "Minimum silence between bench chunks.",
    "eme_chunk_s": "Monostatic Moon: transmit at most the round trip (2.5 s) minus the guard, then listen for the "
                   "echo. 2.4 s. The station's transmit/receive switching must follow this cadence (7.2, O6); if it "
                   "cannot, use bistatic_tx with a partner receiver.",
    "eme_rtt_guard": "Time kept between the end of a chunk and the arrival of its echo (switching time).",
    "eme_t_off_min": "Minimum silence between Moon chunks.",
    "eme_t_on_max": "Longest chunk the schedule may plan for the Moon.",
    "eme_pa": "Tick when the amplifier is in the chain: the 300 s on / 240 s off limits of design 7.2 are then "
              "enforced whatever the schedule says. Untick for the bare-B210 EME test (design 4.6).",
    "sky_chunk_s": "Venus transmit chunk. 240 s against the 272 s round trip on conjunction day; the amplifier's "
                   "thermal limit is the other bound (design 4.2, O2).",
    "sky_rtt_guard": "Seconds kept between the end of a chunk and the earliest echo (30 s covers the switching and "
                     "the round-trip change during a session).",
    "sky_t_on_max": "Longest chunk the schedule may plan (the amplifier's on-limit, 300 s).",
    "sky_t_off_min": "Shortest silence between chunks (the amplifier's cooling limit, 240 s).",
    "sky_pa": "Tick when the amplifier is in the chain (it is, for Venus): the 7.2 limits are enforced regardless "
              "of the schedule and a schedule violating them is refused.",
    "sky_mode": "monostatic: DSES transmits and receives its own echo in the gaps. bistatic_tx: DSES transmits the "
                "whole message (chunked only by the amplifier limits) and a partner station receives.",
    "sky_start_now": "Start the first chunk this many seconds after START (the radio needs about 8 s to arm). "
                     "Untick to start at the UTC time on the right (for a planned session window).",
    "lead_s": "Seconds from START to the first chunk. At least 12; 15 is comfortable. Also used by the bench.",
    "sky_start_utc": "UTC start of the first chunk when 'start now' is unticked. The run refuses a time in the past.",
    "sky_source": "auto: JPL Horizons over the internet, astropy with the local DE440s if that fails. The two differ "
                  "by 10 Hz at 13 cm, so Horizons is primary (design 4.3). The table used is saved with the run.",
    "sky_precomp": "Monostatic: shift the transmit tones by minus the predicted two-way Doppler so our own echo "
                   "lands on the nominal comb. Untick only when transmitting for a partner that does its own removal.",
    "set_defaults": "Put every setting the selected run mode cares about back to the design value (frequency, "
                    "repeat count, symbol length, gains, chunking, timing, Doppler). Settings the mode does not use are "
                    "left alone. The status line lists what changed.",
    "gpsdo_sub": "Tick when the external 10 MHz and PPS come from the Leo Bodnar GPS clock on this PC's USB: the "
                 "program sets it to the station setting (OUT1 10 MHz at level 1, OUT2 disabled = 1 PPS) and waits "
                 "for lock before opening the radio. Untick for any other reference (the HP5065A rubidium and a PPS "
                 "source, or a GPSDO the program cannot talk to); the B210's own lock check still runs.",
    "keyer_kind": "The station's transmit/receive switching, done by the modem (design 7.2, D24). none: nothing is "
                  "switched (loopback bench, simulation, receive only). Otherwise two signals go out on two outputs in "
                  "parallel: relay 1 of the USB relay board and B210 GPIO_0 = TX key (energized / high = transmit); relay 2 "
                  "and GPIO_1 = LNA (energized / high = LNA off). Both released = receive, so an unplugged board or a dead "
                  "PC leaves the feed in receive. Order: LNA off, guard, TX on ... TX off, release, LNA on.",
    "keyer_port": "The USB relay board's COM port. 'auto' finds it (the board answers a status query; it may sit on a "
                  "different port each day). If no board answers, the run continues on the GPIO lines alone and says so. "
                  "The GPIO pins need an external circuit; GPIO_0 alone can drive a DB6NT-style sequencer (GPIO_1 is "
                  "then ignored).",
    "keyer_lna_guard_ms": "Time between switching the LNA off and keying the transmitter. The RF still starts T_lead "
                          "(200 ms) after the key; this guard is added in front of it.",
    "keyer_lna_release_ms": "Time between unkeying the transmitter and switching the LNA back on.",
    "mode:siggen": "The B210 as a bench source for the RF package: CW, two tones, or the EVE waveform, with the "
                   "sequencer working exactly as in a session. Runs until you press STOP: key and unkey from the Run "
                   "tab as often as you like, and change the signal, the TX gain (Radio group) and the digital scale "
                   "here while it runs; everything applies at once and is time-stamped in the log and the report. For "
                   "power-out and compression measurements on the bench instruments; the meter is the truth.",
    "siggen_signal": "CW: one tone at the dial frequency plus the offset. Two-tone: two equal tones spaced as set, "
                     "around the offset (intermodulation). EVE waveform: the message's tone hops at the design frame "
                     "rate, the real comb the amplifier will see.",
    "siggen_offset_khz": "Where the tone (or the two-tone pair's center) sits relative to the dial frequency. 0 is the "
                         "dial frequency itself (the LO is parked 300 kHz away, so 0 is clean).",
    "siggen_spacing_khz": "Two-tone: the spacing between the two tones.",
    "siggen_level_db": "Digital scale below full scale: the fine level control, applied instantly without touching the "
                       "radio. Combine with TX gain (coarse, 0 to 89.75 dB). Both can be changed while the generator runs.",
    "siggen_pa": "Enforce the amplifier's 7.2 duty limits per key-down: the key is released after 300 s and refused "
                 "until 240 s have passed. Tick whenever an amplifier is in the chain, even into a load.",
    "siggen_sim": "Run the generator without a B210: the simulated radio takes the samples, while the sequencer (USB relay "
                  "board and simulated GPIO), the live level controls, the sweep and the report all work as they would on "
                  "the air. Use it to check the station wiring to the relays before the first real transmission.",
    "siggen_sweep": "Step the TX gain from start to stop by the step, holding each level for the hold time: automatically "
                    "on the first key-down, and again whenever you press Sweep on the Run tab. Every step is time-stamped "
                    "in the log and the report so meter readings line up with steps. Unkeying stops the sweep; the gain "
                    "stays at the last level reached.",
    "mode:interop": "Compatibility test with ORI's own hardware and software, on the bench (cable and attenuator) "
                    "or across the room. Transmit only: we send the agreed message at the agreed UTC time and the "
                    "partner's receiver decodes it. Receive only: the partner transmits (their generator has no pilot: "
                    "untick Pilot) and we archive and decode, searching for their start within the search range. No "
                    "Doppler, no round trip, no amplifier limits.",
    "interop_dir": "Which end we are. Transmit only keys nothing unless a sequencer is wired; receive only sends no RF at all.",
    "interop_chunk_s": "Length of each contiguous transmit/receive block. Blocks follow each other with no gap, so the "
                       "signal is continuous; the block only sets the archive file size (about 24 MB per 300 s).",
    "interop_search_frames": "Receive only: how far (in frames of 0.35 s) the partner's actual start may differ from "
                             "the agreed time. The decoder searches this range; 30 frames = +/-10 s. Larger costs time.",
    "sky_rx_doppler": "Remove the model Doppler on receive. Needed when this receiver is NOT the one the transmit was "
                      "pre-compensated for (receiving a partner's transmission).",
}


# ------------------------------------------------------------------------------------------
# settings
# ------------------------------------------------------------------------------------------
class Settings:
    """Typed access to the INI file. Keys are the form fields; defaults live here."""
    DEFAULTS: Dict[str, object] = {
        "mode": "bench", "variant": "A", "f_dial_mhz": 1296.0, "message": "K0PRT K0PRT", "repeat": 2,
        "pilot": True, "full_symbol": False, "n_frames_test": 6, "pilot_frames_test": 2,
        "serial": "", "tx_gain": 0.0, "rx_gain": 30.0, "clock": "external", "time_host": False,
        "gpsdo": True, "lo_offset_khz": -300.0, "archive": "archive_app",
        "tx_port": "A", "rx_port": "A : RX2",
        "bench_range_km": 0.15, "bench_chunk_s": 2.4, "bench_t_off_min": 0.5, "lead_s": 15.0,
        "sim_cn0": 20.0, "sim_range_km": 375000.0, "sim_seed": 1,
        "sky_mode": "monostatic", "sky_start_now": True, "sky_start_utc": "", "sky_chunk_s": 240.0,
        "sky_pa": True, "sky_t_on_max": 300.0, "sky_t_off_min": 240.0, "sky_rtt_guard": 30.0,
        "sky_source": "auto", "sky_rx_doppler": False, "sky_precomp": True,
        "eme_chunk_s": 2.4, "eme_pa": False, "eme_t_off_min": 0.5, "eme_rtt_guard": 0.1, "eme_t_on_max": 300.0,
        "interop_dir": "Transmit only (the partner receives)", "interop_chunk_s": 300.0, "interop_search_frames": 30,
        "report_zoom": 100,
        "keyer_kind": "none", "keyer_port": "auto", "keyer_lna_guard_ms": 50, "keyer_lna_release_ms": 50,
        "siggen_signal": "CW", "siggen_offset_khz": 0.0, "siggen_spacing_khz": 10.0, "siggen_level_db": -3.0,
        "siggen_pa": False, "siggen_sim": False, "siggen_sweep": False, "siggen_sweep_start_db": 0.0,
        "siggen_sweep_stop_db": 60.0, "siggen_sweep_step_db": 5.0, "siggen_sweep_hold_s": 10.0,
        # updates (the Workbench way): a manifest on gpstime, checked at start-up at most once a day
        "manifest_url": "https://gpstime.com/sw_distribution/eve-modem/manifest.json", "auto_check": True,
        "check_interval_hours": 24, "last_check_iso": "", "dismissed_version": "",
    }

    def __init__(self):
        self.q = QtCore.QSettings(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, "DSES", "EVE_Modem")

    @property
    def path(self) -> str:
        """The INI file's path (QSettings user scope, organization DSES, application
        EVE_Modem); shown in the status bar and the About box."""
        return self.q.fileName()

    def get(self, key: str):
        """Read `key` from the INI file, coerced to the type of its DEFAULTS entry (bool,
        int, float, or str); a missing or unparsable value gives the default. KeyError for a
        key that is not in DEFAULTS."""
        d = self.DEFAULTS[key]
        v = self.q.value(key, d)
        if isinstance(d, bool):
            return v in (True, "true", "True", 1, "1")
        if isinstance(d, int):
            try:
                return int(v)
            except Exception:
                return d
        if isinstance(d, float):
            try:
                return float(v)
            except Exception:
                return d
        return str(v) if v is not None else d

    def set(self, key: str, value) -> None:
        """Store `value` under `key` (written to disk on sync() or when Qt flushes)."""
        self.q.setValue(key, value)

    def sync(self) -> None:
        """Flush pending values to the INI file."""
        self.q.sync()


# ------------------------------------------------------------------------------------------
# run controller: one worker PROCESS per run (eve/worker.py); the GUI holds a proxy
# ------------------------------------------------------------------------------------------
class _RemoteRadio:
    """What the operator panel asks of a radio, answered from the worker's status messages."""

    def __init__(self, t_offset: float, text: str, sim: bool):
        self.t_offset = t_offset
        self.text = text
        self.keyed = False
        self.sim = sim

    def device_time(self) -> float:
        """The worker's radio time now: the PC clock plus the offset the worker reported with
        the session message (B210 device time minus time.time())."""
        return time.time() + self.t_offset

    def status_text(self) -> str:
        """The radio status block from the latest worker status message (the panel's radio
        pane)."""
        return self.text


class RemoteSession:
    """The panel's view of a session running in the worker process: the schedule, an
    accumulator fed by the worker's frame metrics, phase and key state, status texts."""

    def __init__(self, payload: Dict, abort_fn):
        self.sched = S.Schedule.from_dict(payload["schedule"])
        self.p = self.sched.params
        self.model = None
        self.radio = _RemoteRadio(float(payload.get("t_offset", 0.0)), payload.get("radio", ""), bool(payload.get("sim")))
        self.gps_text = payload.get("gps", "")
        self.eph_text = payload.get("generator", "") or ""
        self.sched_text = ""
        self.acc = modem.SymbolAccumulator(self.p, self.sched.frame_map())
        self.phase = "preparing"
        self.phase_kind = "busy"
        self.listeners = []
        self.log = lambda s: None
        self._abort = abort_fn

    def abort(self, reason: str) -> None:
        """Ask the worker to abort the run (the panel's ABORT button); `reason` goes to the
        session log."""
        self._abort(reason)

    def frame(self, k: int, metric: np.ndarray) -> None:
        """One ("frame", k, metric) message from the worker: frame index `k` and its metric
        over the candidate tones are added to the accumulator and handed to the panel's
        listeners as (k, None, metric)."""
        self.acc.add(k, metric)
        for fn in self.listeners:
            try:
                fn(k, None, metric)
            except Exception:
                pass

    def status(self, d: Dict) -> None:
        """Apply a ("status", dict) message from the worker: the phase text and kind (a phase
        of 'idle' is shown as arming), the key state, and the radio, GPS clock, and ephemeris
        texts the panel shows."""
        ph = d.get("phase", self.phase)
        self.phase = "starting the streams (arming)" if ph == "idle" else ph
        if "kind" in d:
            self.phase_kind = d["kind"]
        elif "phase" in d:
            self.phase_kind = "run"
        self.radio.keyed = bool(d.get("keyed", False))
        if "radio" in d:
            self.radio.text = d["radio"]
        if "gps" in d:
            self.gps_text = d["gps"]
        if "eph" in d:
            self.eph_text = d["eph"]
        if "generator" in d:
            self.eph_text = d["generator"]
        if "sched_text" in d:
            self.sched_text = d["sched_text"]


FULL_THRESHOLD_DB = {"A": -0.6, "B": -1.7}     # design 2.4: 10 % message error, one pass, AWGN model


def cn0_threshold_db(variant: str, n_frames: int) -> float:
    """C/N0 (dB-Hz) at which one pass decodes with a 10 % message error rate, for a symbol of
    `n_frames` frames. The design's chi-square model (4096 tones, noncoherent sum of the
    frames, 11 symbols) gives -0.6 dB-Hz at 473 frames for Variant A and rises 6.5 dB per
    decade of fewer frames (2 frames +15.5, 6 +11.7, 47 +5.4, 118 +3.0, 236 +1.2; the fit is
    within 0.5 dB of the Monte Carlo at every point). A short TEST symbol therefore needs a
    strong signal: the 6-frame bench length wants about +12 dB-Hz, not the 0 dB-Hz the
    waveform is sized for. The sensitivity is bought with the 164.8 s symbol."""
    v = "B" if str(variant).upper().startswith("B") else "A"
    n_full = FULL_FRAMES.get(v, 473)
    return FULL_THRESHOLD_DB[v] + 6.5 * math.log10(n_full / max(1, int(n_frames)))


def open_pdf(path) -> None:
    """Open a PDF in a real viewer. On Linux the desktop's default handler for PDFs is
    often LibreOffice Draw once LibreOffice is installed (Ubuntu 22.04 with the default
    suite): Draw IMPORTS the file for editing with substitute fonts, so text overflows its
    boxes and the document looks broken. Prefer a viewer that renders the embedded fonts;
    fall back to the desktop default."""
    path = str(Path(path).resolve())
    if sys.platform.startswith("linux"):
        import shutil
        import subprocess
        for viewer in ("evince", "okular", "xreader", "atril", "qpdfview", "zathura", "firefox"):
            exe = shutil.which(viewer)
            if exe:
                try:
                    subprocess.Popen([exe, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    continue
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))


class RunController(QtCore.QObject):
    """Runs one job at a time in a worker process (eve.worker.run_job) and turns the worker's
    pipe messages into Qt signals for the window: log(text), state(preparing | running |
    decoding | idle), preview_ready(schedule text), notice(a warning shown without stopping
    the run), session_ready(RemoteSession to bind the panel to), session_done(), and
    finished(result dict). A worker that dies while opening the radio is relaunched up to
    three times before the operator hears of it."""
    log = QtCore.Signal(str)
    session_ready = QtCore.Signal(object)          # RemoteSession, before frames arrive (GUI binds the panel)
    preview_ready = QtCore.Signal(str)
    notice = QtCore.Signal(str)
    finished = QtCore.Signal(dict)                 # {"ok", "pdf", "session_id", "summary", "error"}
    state = QtCore.Signal(str)                     # idle | preparing | running | decoding
    session_done = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session: Optional[RemoteSession] = None
        self.proc = None
        self.conn = None
        self._reader: Optional[threading.Thread] = None
        self.busy = False

    # ---- public --------------------------------------------------------------------------
    def start(self, cfg: Dict, preview_only: bool = False) -> bool:
        """Launch a run (or, with preview_only, only the schedule description) from a
        settings dict as EveApp._values() builds it. Returns False when a job is already
        running."""
        return self._launch(dict(cfg), preview_only, None)

    def redecode(self, session_json: Path) -> bool:
        """Launch a worker that re-runs the offline decode and the report of an archived
        session from its schedule JSON. Returns False when busy."""
        return self._launch({}, False, str(session_json))

    def abort(self, reason: str = "operator abort") -> None:
        """Send ("abort", reason) to the worker; a no-op when none is running."""
        if self.conn is not None:
            try:
                self.conn.send(("abort", reason))
            except Exception:
                pass

    def panel_bound(self) -> None:
        """Hook called when the window binds the operator panel; nothing to do with a worker
        process (kept for the in-process API)."""
        pass

    def panel_released(self) -> None:
        """Hook called when the window unbinds the operator panel; nothing to do here."""
        pass

    def close_radio(self) -> None:
        """Nothing to hold: the radio lives and dies with the worker process."""
        if self.proc is not None and self.proc.is_alive():
            self.abort("window closed")
            self.proc.join(3.0)
            if self.proc.is_alive():
                self.proc.terminate()

    # ---- process management ----------------------------------------------------------------
    def _launch(self, cfg: Dict, preview_only: bool, redecode: Optional[str], retry: int = 3) -> bool:
        """Spawn the worker process with a duplex pipe and start the reader thread. A preview
        worker still winding down is terminated first (its text is already on screen) rather
        than refusing the Start. `retry` is how many relaunches a crash during the radio open
        may still get."""
        if self.busy and self._job is not None and self._job[1] and not self._saw_session:
            # A preview worker is still winding down (its text is already on screen; a
            # process with UHD and GNU Radio loaded can take many seconds to exit). Nothing
            # is lost by ending it, so do that rather than refuse the Start after a Preview.
            proc, reader = self.proc, self._reader
            cfg0, po0, rd0, _ = self._job
            self._job = (cfg0, po0, rd0, 0)          # no retries: the exit is deliberate, not a fault
            if proc is not None and proc.is_alive():
                proc.terminate()
            if reader is not None:
                reader.join(10.0)
        if self.busy:
            return False
        self._job = (cfg, preview_only, redecode, retry)
        self._saw_session = False
        import multiprocessing as mp
        from . import worker
        self.busy = True
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        self.conn = parent
        self.proc = ctx.Process(target=worker.run_job, args=(cfg, preview_only, child, redecode), name="eve-worker", daemon=True)
        self.proc.start()
        child.close()
        self._reader = threading.Thread(target=self._pump, args=(parent, self.proc), name="eve-worker-reader", daemon=True)
        self._reader.start()
        return True

    def _pump(self, conn, proc) -> None:
        """The reader thread: forwards pipe messages to _handle until the worker exits, then
        relaunches a worker that crashed before the session existed (while retries remain) or
        synthesizes a failure result, and emits session_done, state('idle'), and finished
        (previews emit no finished)."""
        finished = None
        try:
            while True:
                try:
                    if not conn.poll(0.2):
                        if not proc.is_alive():
                            # one more look for messages that arrived with the exit
                            while conn.poll():
                                finished = self._handle(conn.recv()) or finished
                            break
                        continue
                    msg = conn.recv()
                except (EOFError, OSError, BrokenPipeError):     # the worker is gone (pipe ended)
                    break
                finished = self._handle(msg) or finished
        finally:
            proc.join(5.0)
            if finished is None and not self._saw_session and self._job[3] > 0 and proc.exitcode not in (0, None):
                # the worker died while opening the radio: UHD's usrp_source constructor
                # faults intermittently on this platform (about 1 open in 7 on the bench,
                # 2026-09-12, nothing else on USB). Retry a few times before bothering
                # the operator; the crashed worker leaves nothing behind.
                cfg, preview_only, redecode, left = self._job
                self.log.emit(f"the run worker died while opening the radio (exit code {proc.exitcode}); "
                              f"retrying in 3 s ({left - 1} more {'try' if left - 1 == 1 else 'tries'} after this)")
                self.busy = False
                self.conn = None
                time.sleep(3.0)
                if self._launch(cfg, preview_only, redecode, retry=left - 1):
                    return
            if finished is None:
                code = proc.exitcode
                finished = {"ok": False, "pdf": None,
                            "error": f"the run worker stopped without a result (exit code {code}); see fault.log and app.log "
                                     f"in {log_dir()}. The window is unaffected: fix the cause and Start again."}
                self.log.emit("ERROR: " + finished["error"])
            if self.session is not None:
                self.session_done.emit()
                self.session = None
            self.busy = False
            self.conn = None
            self.state.emit("idle")
            if not finished.get("preview"):         # a preview has nothing to report
                self.finished.emit(finished)

    def _handle(self, msg) -> Optional[Dict]:
        """Dispatch one pipe message to the matching signal or the RemoteSession; returns the
        result dict of a 'finished' message, else None."""
        kind = msg[0]
        if kind == "log":
            self.log.emit(msg[1])
        elif kind == "state":
            if msg[1] != "idle":
                self.state.emit(msg[1])
        elif kind == "preview":
            self.preview_ready.emit(msg[1])
        elif kind == "notice":
            self.notice.emit(msg[1])
        elif kind == "session":
            self._saw_session = True
            self.session = RemoteSession(msg[1], self.abort)
            self.session_ready.emit(self.session)
        elif kind == "frame":
            if self.session is not None:
                self.session.frame(int(msg[1]), np.frombuffer(msg[2], dtype=np.float32))
        elif kind == "status":
            if self.session is not None:
                self.session.status(msg[1])
        elif kind == "finished":
            return msg[1]
        return None


# ------------------------------------------------------------------------------------------
# report viewer
# ------------------------------------------------------------------------------------------
class ReportPane(QtWidgets.QWidget):
    """One rendered report: a header naming it, the pages in a scroll area."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current: Optional[Path] = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.header = QtWidgets.QLabel("no report selected")
        self.header.setStyleSheet(f"font-weight: bold; color: white; background: {TEAL}; padding: 4px 8px;")
        lay.addWidget(self.header)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.pages = QtWidgets.QWidget()
        self.pages_lay = QtWidgets.QVBoxLayout(self.pages)
        self.pages_lay.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop)
        self.scroll.setWidget(self.pages)
        lay.addWidget(self.scroll, 1)

    def clear(self) -> None:
        """Remove every rendered page widget from the pane."""
        while self.pages_lay.count():
            w = self.pages_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)        # stop painting now; deleteLater alone leaves ghosts until the loop turns
                w.deleteLater()

    def render(self, pdf: Optional[Path], zoom: int) -> None:
        """Show `pdf` (a Path, or None for 'no report selected') at `zoom` percent: every
        page is rasterized with pymupdf at 96 dpi times zoom/100 into a QLabel. A rendering
        failure shows the error and points to 'Open in PDF viewer'."""
        self.clear()
        self.current = pdf
        if pdf is None or not pdf.exists():
            self.header.setText("no report selected")
            self.pages_lay.addWidget(QtWidgets.QLabel("no report selected"))
            return
        self.header.setText(f"{pdf.name[:-len('_report.pdf')]}    written {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pdf.stat().st_mtime))}")
        try:
            import pymupdf
            doc = pymupdf.open(str(pdf))
            for page in doc:
                pix = page.get_pixmap(dpi=int(96 * zoom / 100), alpha=False)
                img = QtGui.QImage(pix.samples, pix.width, pix.height, pix.stride, QtGui.QImage.Format_RGB888).copy()
                lbl = QtWidgets.QLabel()
                lbl.setPixmap(QtGui.QPixmap.fromImage(img))
                lbl.setFrameShape(QtWidgets.QFrame.Box)
                self.pages_lay.addWidget(lbl)
            doc.close()
        except Exception as e:      # noqa: BLE001
            self.pages_lay.addWidget(QtWidgets.QLabel(f"cannot render {pdf.name}: {e}\nUse 'Open in PDF viewer'."))
        self.scroll.verticalScrollBar().setValue(0)


class ReportView(QtWidgets.QWidget):
    """The report list, one rendered report, and an optional second pane for comparing."""
    redecode_requested = QtCore.Signal(object)     # Path of the schedule JSON

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.archive = Path(settings.get("archive"))
        lay = QtWidgets.QHBoxLayout(self)
        left_w = QtWidgets.QWidget()
        left = QtWidgets.QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(QtWidgets.QLabel("Session reports in the archive folder (newest first). Click one to show it."))
        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(200)
        self.list.currentItemChanged.connect(self._pick)
        self.list.setToolTip("One entry per session report; click to show it in the main pane. With Compare ticked, "
                             "the second pane keeps its own choice so two runs sit side by side.")
        left.addWidget(self.list, 1)
        row = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_refresh.setToolTip("Re-read the archive folder for reports.")
        self.btn_open = QtWidgets.QPushButton("Open in PDF viewer")
        self.btn_open.clicked.connect(self._open_external)
        self.btn_open.setToolTip("Open the selected report in the system PDF viewer (for printing or sending).")
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_open)
        left.addLayout(row)
        row2 = QtWidgets.QHBoxLayout()
        self.btn_folder = QtWidgets.QPushButton("Open archive folder")
        self.btn_folder.setToolTip("Open the archive folder in Explorer: schedules, ephemeris tables, receive windows, logs, reports.")
        self.btn_folder.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.archive.resolve()))))
        self.btn_redecode = QtWidgets.QPushButton("Re-decode this session")
        self.btn_redecode.setToolTip("run the offline decode again on the archived IQ and rebuild the report")
        self.btn_redecode.clicked.connect(self._redecode)
        row2.addWidget(self.btn_folder)
        row2.addWidget(self.btn_redecode)
        left.addLayout(row2)
        zrow = QtWidgets.QHBoxLayout()
        zrow.addWidget(QtWidgets.QLabel("Zoom"))
        self.zoom = QtWidgets.QComboBox()
        for z in (50, 75, 100, 125, 150, 200):
            self.zoom.addItem(f"{z} %", z)
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(int(settings.get("report_zoom")))))
        self.zoom.currentIndexChanged.connect(lambda _i: self._rerender())
        self.zoom.setToolTip("Page rendering size (50 % fits two reports side by side on a laptop screen).")
        zrow.addWidget(self.zoom)
        self.compare = QtWidgets.QCheckBox("Compare")
        self.compare.setToolTip("Show a second pane on the right with its own report chooser, to compare two runs.")
        self.compare.toggled.connect(self._toggle_compare)
        zrow.addWidget(self.compare)
        zrow.addStretch(1)
        left.addLayout(zrow)

        self.pane = ReportPane()
        self.pane2 = ReportPane()
        self.pane2_pick = QtWidgets.QComboBox()
        self.pane2_pick.setToolTip("The report shown in the compare pane.")
        self.pane2_pick.currentIndexChanged.connect(self._pane2_changed)
        pane2_w = QtWidgets.QWidget()
        p2 = QtWidgets.QVBoxLayout(pane2_w)
        p2.setContentsMargins(0, 0, 0, 0)
        p2.addWidget(self.pane2_pick)
        p2.addWidget(self.pane2, 1)
        self.pane2_w = pane2_w
        self.pane2_w.hide()
        self.panes = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.panes.addWidget(self.pane)
        self.panes.addWidget(self.pane2_w)
        self.split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.split.addWidget(left_w)
        self.split.addWidget(self.panes)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 4)
        self.split.setSizes([330, 960])
        lay.addWidget(self.split)
        self.refresh()

    # ---- compatibility with the earlier single-pane API
    @property
    def current(self) -> Optional[Path]:
        """The report in the main pane (Path), or None."""
        return self.pane.current

    @property
    def pages_lay(self):
        """The main pane's page layout (the earlier single-pane API)."""
        return self.pane.pages_lay

    def set_archive(self, folder: str) -> None:
        """Point the view at another archive folder and re-read its reports (the Setup tab's
        archive field, on Browse and Start)."""
        self.archive = Path(folder)
        self.refresh()

    def _files(self):
        return sorted(self.archive.glob("*_report.pdf"), key=lambda f: f.stat().st_mtime, reverse=True) if self.archive.exists() else []

    def refresh(self) -> None:
        """Re-read the archive folder for *_report.pdf files (newest first) into the list and
        the compare chooser, keeping the current choices; with nothing chosen the newest
        report is shown."""
        files = self._files()
        keep = self.pane.current
        self.list.blockSignals(True)
        self.list.clear()
        for f in files:
            it = QtWidgets.QListWidgetItem(f"{f.name[:-len('_report.pdf')]}   {time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))}")
            it.setData(QtCore.Qt.UserRole, str(f))
            self.list.addItem(it)
        self.list.blockSignals(False)
        keep2 = self.pane2.current
        self.pane2_pick.blockSignals(True)
        self.pane2_pick.clear()
        for f in files:
            self.pane2_pick.addItem(f.name[:-len("_report.pdf")], str(f))
        if keep2 is not None:
            i = self.pane2_pick.findData(str(keep2))
            if i >= 0:
                self.pane2_pick.setCurrentIndex(i)
        self.pane2_pick.blockSignals(False)
        if files and (keep is None or not keep.exists()):
            self.list.setCurrentRow(0)
        elif keep is not None:
            self.select(keep)

    def select(self, pdf: Path) -> None:
        """Select `pdf` in the list (which renders it) or, when it is not listed, render it
        in the main pane directly."""
        for i in range(self.list.count()):
            if self.list.item(i).data(QtCore.Qt.UserRole) == str(pdf):
                if self.list.currentRow() == i:
                    self.pane.render(pdf, int(self.zoom.currentData()))
                else:
                    self.list.setCurrentRow(i)
                return
        self.pane.render(pdf, int(self.zoom.currentData()))

    def _pick(self, cur, _prev) -> None:
        """List selection changed: render the chosen report in the main pane."""
        if cur is None:
            return
        self.pane.render(Path(cur.data(QtCore.Qt.UserRole)), int(self.zoom.currentData()))

    def _pane2_changed(self, i: int) -> None:
        """Compare chooser changed: render its report in the second pane while Compare is on."""
        if i >= 0 and self.compare.isChecked():
            self.pane2.render(Path(self.pane2_pick.itemData(i)), int(self.zoom.currentData()))

    def _toggle_compare(self, on: bool) -> None:
        """Compare box toggled: show or hide the second pane; on first use it defaults to the
        run before the one in the main pane and the two panes are split evenly (guide 6)."""
        self.pane2_w.setVisible(on)
        if on:
            if self.pane2_pick.count() and self.pane2.current is None:
                # default: the run before the one in the main pane
                i = 0
                for j in range(self.pane2_pick.count()):
                    if self.pane.current is not None and self.pane2_pick.itemData(j) == str(self.pane.current):
                        i = min(j + 1, self.pane2_pick.count() - 1)
                        break
                self.pane2_pick.setCurrentIndex(i)
            self._pane2_changed(self.pane2_pick.currentIndex())
            self.panes.setSizes([1, 1])

    def _rerender(self) -> None:
        """Zoom changed: remember it in the report_zoom setting and re-render both panes."""
        z = int(self.zoom.currentData())
        self.settings.set("report_zoom", z)
        self.pane.render(self.pane.current, z)
        if self.compare.isChecked():
            self.pane2.render(self.pane2.current, z)

    def _open_external(self) -> None:
        """'Open in PDF viewer' button: open the main pane's report with open_pdf()."""
        if self.pane.current is not None and self.pane.current.exists():
            open_pdf(self.pane.current)

    def _redecode(self) -> None:
        """'Re-decode this session' button: emit redecode_requested with the session's
        schedule JSON (<session_id>.json beside the report) when it exists."""
        if self.pane.current is None:
            return
        sid = self.pane.current.name[:-len("_report.pdf")]
        sj = self.pane.current.parent / f"{sid}.json"
        if sj.exists():
            self.redecode_requested.emit(sj)


# ------------------------------------------------------------------------------------------
# help: the operator's guide (docs/DSES_EVE_Modem_Operators_Guide.md) in the window
# ------------------------------------------------------------------------------------------
DOCS = Path(__file__).resolve().parents[1] / "docs"
GUIDE_MD = DOCS / "DSES_EVE_Modem_Operators_Guide.md"
GUIDE_PDF = DOCS / "DSES_EVE_Modem_Operators_Guide.pdf"
DESIGN_PDF = DOCS / "DSES_EVE_Modem_Design_and_ICD.pdf"


class HelpDialog(QtWidgets.QDialog):
    """The operator's guide (docs/DSES_EVE_Modem_Operators_Guide.md rendered as Markdown in a
    QTextBrowser) with a find box and buttons that open the guide and design PDFs. Help >
    Operator's guide, F1."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DSES EVE modem — Operator's guide")
        self.resize(900, 760)
        lay = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("find in the guide (Enter for next)")
        self.search.returnPressed.connect(self._find)
        row.addWidget(self.search, 1)
        b = QtWidgets.QPushButton("Open the guide PDF")
        b.setEnabled(GUIDE_PDF.exists())
        b.clicked.connect(lambda: open_pdf(GUIDE_PDF))
        row.addWidget(b)
        b = QtWidgets.QPushButton("Design document PDF")
        b.setEnabled(DESIGN_PDF.exists())
        b.clicked.connect(lambda: open_pdf(DESIGN_PDF))
        row.addWidget(b)
        lay.addLayout(row)
        self.view = QtWidgets.QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setSearchPaths([str(DOCS)])
        try:
            md = GUIDE_MD.read_text(encoding="utf-8")
            # the document header table and the width hints are for the PDF build
            md = "\n".join(ln for ln in md.splitlines() if not ln.startswith("<!-- widths"))
            self.view.setMarkdown(md)
        except Exception as e:      # noqa: BLE001
            self.view.setPlainText(f"The guide is not available: {e}\n(expected at {GUIDE_MD})")
        lay.addWidget(self.view, 1)

    def _find(self):
        """Enter in the find box: jump to the next match of the text, wrapping to the top."""
        q = self.search.text()
        if q and not self.view.find(q):
            self.view.moveCursor(QtGui.QTextCursor.Start)
            self.view.find(q)


# ------------------------------------------------------------------------------------------
# main window
# ------------------------------------------------------------------------------------------
class EveApp(QtWidgets.QMainWindow):
    """The main window: Setup, Run, and Report tabs (operator's guide sections 4, 5, 6), the
    File and Help menus, and the RunController. Settings, window geometry, and splitter
    states are restored from the INI file at start and saved on every Start, on File > Save
    settings, and on exit; the start-up update check runs after the window is built."""
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.ctl = RunController(self)
        self.ctl.log.connect(self._log)
        self.ctl.session_ready.connect(self._session_ready)
        self.ctl.session_done.connect(self._session_done)
        self.ctl.preview_ready.connect(self._preview_ready)
        self.ctl.notice.connect(self._notice)
        self.ctl.finished.connect(self._finished)
        self.ctl.state.connect(self._state)
        self.w: Dict[str, QtWidgets.QWidget] = {}
        self.setWindowTitle(f"DSES EVE modem  {__version__}")
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.setup = self._build_setup()
        self.panel = OperatorPanel()
        self.report = ReportView(self.settings)
        self.report.redecode_requested.connect(self._redecode)
        self.tabs.addTab(self.setup, "Setup")
        self.run_page = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(self.run_page)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self._build_generator_bar())
        rl.addWidget(self.panel, 1)
        self.tabs.addTab(self.run_page, "Run")
        self.tabs.addTab(self.report, "Report")
        self.tabs.setTabToolTip(0, "Choose the run mode and its settings, preview the schedule, start and abort.")
        self.tabs.setTabToolTip(1, "The live session: tone strip, running decisions, chunk and key state, radio and GPS clock, ephemeris.")
        self.tabs.setTabToolTip(2, "Session reports (PDF) in the archive folder; the newest opens when a run ends.")
        self.status = QtWidgets.QLabel("idle")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(QtWidgets.QLabel(f"settings: {self.settings.path}"))
        filem = self.menuBar().addMenu("&File")
        a = filem.addAction("Save settings")
        a.setShortcut("Ctrl+S")
        a.triggered.connect(self._save)
        a = filem.addAction("Open archive folder")
        a.triggered.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(self.w["archive"].text() or ".").resolve()))))
        filem.addSeparator()
        a = filem.addAction("E&xit")
        a.setShortcut("Ctrl+Q")
        a.setMenuRole(QtGui.QAction.QuitRole)
        a.triggered.connect(self.close)          # goes through closeEvent: abort prompt, settings saved
        helpm = self.menuBar().addMenu("&Help")
        a = helpm.addAction("Operator's guide")
        a.setShortcut("F1")
        a.triggered.connect(self._help)
        a = helpm.addAction("Open the guide PDF")
        a.triggered.connect(lambda: open_pdf(GUIDE_PDF))
        a = helpm.addAction("Design description and ICD (PDF)")
        a.triggered.connect(lambda: open_pdf(DESIGN_PDF))
        helpm.addSeparator()
        a = helpm.addAction("Check for updates…")
        a.triggered.connect(self._check_updates_manual)
        helpm.addSeparator()
        a = helpm.addAction("About")
        a.triggered.connect(lambda: QtWidgets.QMessageBox.about(
            self, "DSES EVE modem",
            f"DSES Earth-Venus-Earth modem {__version__}\n\n"
            f"Waveform: ORI 'Spiral #2' by Pete Wyckoff, KA3WCA; reference implementation by Michelle Thompson "
            f"(Open Research Institute, GPL-3.0). We could not have done this without them.\n"
            f"Station software and receiver: Rick Hambly, K0GD, Deep Space Exploration Society.\n"
            f"Supported by a grant from Amateur Radio Digital Communications.\n\n"
            f"Settings: {self.settings.path}"))
        self._help_dlg = None
        self._load()
        self._mode_changed()
        self._coherence()
        wrap_tooltips(self)
        for name in ("setup_split",):
            st = self.settings.q.value(name)
            if st is not None:
                getattr(self, name).restoreState(st)
        for name, obj in (("report_split", self.report.split), ("run_hsplit", self.panel.hsplit), ("run_vsplit", self.panel.vsplit)):
            st = self.settings.q.value(name)
            if st is not None:
                obj.restoreState(st)
        geo = self.settings.q.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1320, 880)
        self._update_dialog = None
        self._installer = None
        self._start_update_check()

    # ---- setup tab ----------------------------------------------------------------------
    def _build_setup(self) -> QtWidgets.QWidget:
        """Build the Setup tab: run mode, waveform, radio, keying, and mode-specific groups
        on the left (in a scroll area, splitter), the schedule preview, START / ABORT, and
        the controller log on the right. Every control registered in self.w gets its TIPS
        tooltip, and its row label the same."""
        w = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(w)
        outer.setContentsMargins(4, 4, 4, 4)
        left_w = QtWidgets.QWidget()
        left = QtWidgets.QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        left_scroll.setWidget(left_w)
        right_w = QtWidgets.QWidget()
        right = QtWidgets.QVBoxLayout(right_w)
        right.setContentsMargins(0, 0, 0, 0)
        self.setup_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.setup_split.addWidget(left_scroll)
        self.setup_split.addWidget(right_w)
        self.setup_split.setStretchFactor(0, 3)
        self.setup_split.setStretchFactor(1, 2)
        self.setup_split.setSizes([780, 520])
        outer.addWidget(self.setup_split)

        # mode
        gb = QtWidgets.QGroupBox("Run mode")
        v = QtWidgets.QVBoxLayout(gb)
        self.mode_group = QtWidgets.QButtonGroup(self)
        for i, (key, label) in enumerate(MODES):
            rb = QtWidgets.QRadioButton(label)
            rb.setProperty("mode", key)
            self.mode_group.addButton(rb, i)
            v.addWidget(rb)
        self.mode_group.idClicked.connect(lambda _i: self._mode_changed(user=True))
        self.btn_defaults = QtWidgets.QPushButton("Set defaults for this run mode")
        self.btn_defaults.setToolTip(TIPS["set_defaults"])
        self.btn_defaults.clicked.connect(self._set_defaults)
        v.addWidget(self.btn_defaults)
        self.lbl_coherence = QtWidgets.QLabel("")
        self.lbl_coherence.setWordWrap(True)
        self.lbl_coherence.setStyleSheet(f"color: {TEAL};")
        v.addWidget(self.lbl_coherence)
        left.addWidget(gb)

        # waveform
        gb = QtWidgets.QGroupBox("Waveform and message")
        f = QtWidgets.QFormLayout(gb)
        self.w["variant"] = cb = QtWidgets.QComboBox()
        cb.addItems(["A", "B"])
        cb.setToolTip("A: ORI 2.87 Hz bins, 473 frames per symbol. B: DSES 23 cm monostatic, 1.5 Hz bins, 247 frames (design 6.1.1)")
        cb.currentTextChanged.connect(lambda _t: (self._symbol_changed(), self._coherence()))
        f.addRow("Variant", cb)
        self.w["f_dial_mhz"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(70.0, 6000.0)
        sp.setDecimals(4)
        sp.setSuffix(" MHz")
        sp.setToolTip("dial frequency; the comb starts 25 kHz above it (1296 bench, 1299.5 Venus 2026, 2304 / 2400 13 cm)")
        f.addRow("Dial frequency", sp)
        self.w["message"] = le = QtWidgets.QLineEdit()
        le.setMaxLength(11)
        le.setToolTip("up to 11 ASCII characters (90 bits); the Venus message is K0PRT K0PRT")
        f.addRow("Message", le)
        self.w["repeat"] = sp = QtWidgets.QSpinBox()
        sp.setRange(1, 12)
        sp.setToolTip("passes of the message; the receiver combines them (design 4.5)")
        f.addRow("Repeat count", sp)
        self.w["pilot"] = ck = QtWidgets.QCheckBox("pilot frames before each pass")
        f.addRow("Pilot", ck)
        symrow = QtWidgets.QHBoxLayout()
        self.w["full_symbol"] = ck = QtWidgets.QCheckBox("full length")
        ck.setToolTip("473 frames per symbol (Variant A) = 164.8 s; a message is 30.2 min per pass. Untick for a short TEST symbol.")
        ck.toggled.connect(lambda _b: (self._symbol_changed(), self._coherence()))
        symrow.addWidget(ck)
        symrow.addWidget(QtWidgets.QLabel("  test length:"))
        self.w["n_frames_test"] = sp = QtWidgets.QSpinBox()
        sp.setRange(2, 2000)
        sp.setSuffix(" frames")
        sp.setToolTip("frames per symbol for a short test (6 = 2.1 s per symbol, 23 s per message pass)")
        sp.valueChanged.connect(lambda _v: self._symbol_changed())
        symrow.addWidget(sp)
        symrow.addWidget(QtWidgets.QLabel("pilot"))
        self.w["pilot_frames_test"] = sp = QtWidgets.QSpinBox()
        sp.setRange(1, 200)
        sp.setSuffix(" frames")
        symrow.addWidget(sp)
        symrow.addStretch(1)
        f.addRow("Symbol length", symrow)
        self.lbl_sym = QtWidgets.QLabel()
        self.lbl_sym.setWordWrap(True)
        self.lbl_sym.setStyleSheet(f"color: {TEAL};")
        f.addRow("", self.lbl_sym)
        left.addWidget(gb)

        # radio
        gb = QtWidgets.QGroupBox("Radio and reference (B210)")
        f = QtWidgets.QFormLayout(gb)
        self.w["serial"] = le = QtWidgets.QLineEdit()
        le.setPlaceholderText("first B2xx found")
        f.addRow("B210 serial", le)
        self.w["tx_gain"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 89.75)
        sp.setSuffix(" dB")
        sp.setToolTip("0 dB for the loopback bench (internal leakage is enough); the driver's input requirement sets it for the air. "
                      "In the signal generator it applies while running.")
        sp.valueChanged.connect(lambda v: self._live_level(tx_gain_db=float(v)))
        f.addRow("TX gain", sp)
        self.w["rx_gain"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 76.0)
        sp.setSuffix(" dB")
        f.addRow("RX gain", sp)
        prow = QtWidgets.QHBoxLayout()
        self.w["tx_port"] = cb = QtWidgets.QComboBox()
        cb.addItems(["A", "B"])
        cb.setToolTip("Which of the B210's two frontends transmits (its TX/RX port). The station is wired "
                      "to A; B is for a board with a bad side or for bench comparisons.")
        prow.addWidget(QtWidgets.QLabel("TX frontend"))
        prow.addWidget(cb, 1)
        prow.addSpacing(12)
        self.w["rx_port"] = cb = QtWidgets.QComboBox()
        cb.addItems(["A : RX2", "B : RX2", "A : TX/RX", "B : TX/RX"])
        cb.setToolTip("Receive frontend and port. The station's LNA feeds A : RX2 (ICD 7.1). A TX/RX port "
                      "is accepted only on the frontend the transmitter does not use, for comparing a "
                      "suspect RX2 port on the bench.")
        cb.currentTextChanged.connect(lambda _t: self._ports_changed())
        prow.addWidget(QtWidgets.QLabel("RX port"))
        prow.addWidget(cb, 1)
        f.addRow("Ports", prow)
        self.w["tx_port"].currentTextChanged.connect(lambda _t: self._ports_changed())
        self.lbl_ports = QtWidgets.QLabel("")
        self.lbl_ports.setWordWrap(True)
        self.lbl_ports.setStyleSheet(f"color: {TEAL};")
        f.addRow("", self.lbl_ports)
        self.w["clock"] = cb = QtWidgets.QComboBox()
        cb.addItems(["external", "gpsdo", "internal"])
        cb.setToolTip("external = 10 MHz on REF IN and 1 PPS on PPS IN from the station reference, whichever it is "
                      "(GPS clock, HP5065A rubidium plus a PPS source); gpsdo = an Ettus GPSDO board inside the B210; "
                      "internal = the B210's TCXO, bench only, no lock check")
        cb.currentTextChanged.connect(lambda _t: self._reference_changed())
        f.addRow("Clock source", cb)
        sub = QtWidgets.QHBoxLayout()
        sub.addSpacing(24)
        self.w["gpsdo"] = ck = QtWidgets.QCheckBox("Leo Bodnar GPS clock on USB (program and check it first)")
        ck.setToolTip(TIPS["gpsdo_sub"])
        sub.addWidget(ck, 1)
        f.addRow("", sub)
        self.w["time_host"] = ck = QtWidgets.QCheckBox("host-timed (no PPS: epoch from the PC clock)")
        f.addRow("Time", ck)
        self.w["lo_offset_khz"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(-2000.0, 2000.0)
        sp.setSuffix(" kHz")
        sp.setToolTip("LO parked off the dial frequency so its leakage stays out of the comb (7.1)")
        f.addRow("LO offset", sp)
        arow = QtWidgets.QHBoxLayout()
        self.w["archive"] = le = QtWidgets.QLineEdit()
        arow.addWidget(le, 1)
        b = QtWidgets.QPushButton("Browse…")
        b.clicked.connect(self._browse_archive)
        arow.addWidget(b)
        f.addRow("Archive folder", arow)
        left.addWidget(gb)

        # keying
        gb = QtWidgets.QGroupBox("Keying (TX key and LNA: the modem is the sequencer)")
        f = QtWidgets.QFormLayout(gb)
        self.w["keyer_kind"] = cb = QtWidgets.QComboBox()
        cb.addItems(list(KEYER_KINDS))
        cb.currentTextChanged.connect(lambda _t: self._keyer_changed())
        f.addRow("Switching", cb)
        krow = QtWidgets.QHBoxLayout()
        self.w["keyer_port"] = pc = QtWidgets.QComboBox()
        pc.setEditable(True)
        krow.addWidget(pc, 1)
        b = QtWidgets.QPushButton("Refresh")
        b.setToolTip("Re-scan the serial ports.")
        b.clicked.connect(self._refresh_ports)
        krow.addWidget(b)
        self.btn_testkey = QtWidgets.QPushButton("Test TX")
        self.btn_testkey.setToolTip("Relay 1 (TX key) on for a second, then off; the board's status reply goes to the log. "
                                    "The radio is not touched.")
        self.btn_testkey.clicked.connect(lambda: self._test_output("tx"))
        krow.addWidget(self.btn_testkey)
        self.btn_testlna = QtWidgets.QPushButton("Test LNA")
        self.btn_testlna.setToolTip("Relay 2 (LNA control) energized = LNA OFF for a second, then released.")
        self.btn_testlna.clicked.connect(lambda: self._test_output("lna"))
        krow.addWidget(self.btn_testlna)
        f.addRow("USB board port", krow)
        trow = QtWidgets.QHBoxLayout()
        self.w["keyer_lna_guard_ms"] = sp = QtWidgets.QSpinBox()
        sp.setRange(0, 2000)
        sp.setSuffix(" ms")
        trow.addWidget(QtWidgets.QLabel("LNA off to TX on"))
        trow.addWidget(sp)
        trow.addSpacing(12)
        self.w["keyer_lna_release_ms"] = sp = QtWidgets.QSpinBox()
        sp.setRange(0, 2000)
        sp.setSuffix(" ms")
        trow.addWidget(QtWidgets.QLabel("TX off to LNA on"))
        trow.addWidget(sp)
        trow.addStretch(1)
        f.addRow("Guard times", trow)
        self.lbl_keyer = QtWidgets.QLabel("")
        self.lbl_keyer.setWordWrap(True)
        self.lbl_keyer.setStyleSheet(f"color: {TEAL};")
        f.addRow("", self.lbl_keyer)
        left.addWidget(gb)
        self._refresh_ports()

        # mode specific
        self.mode_stack = QtWidgets.QStackedWidget()
        # sim
        gb = QtWidgets.QGroupBox("Software simulation")
        f = QtWidgets.QFormLayout(gb)
        self.w["sim_cn0"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(-20.0, 60.0)
        sp.setSuffix(" dB-Hz")
        sp.setToolTip("C/N0 of the simulated echo; the Venus design point is about 0 dB-Hz, 20 is an easy check")
        f.addRow("C/N0", sp)
        self.w["sim_range_km"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 1e9)
        sp.setDecimals(1)
        sp.setSuffix(" km")
        sp.setToolTip("one-way range of the synthetic target (375,000 km = the Moon's 2.5 s round trip)")
        f.addRow("Range", sp)
        self.w["sim_seed"] = sp = QtWidgets.QSpinBox()
        sp.setRange(0, 999999)
        f.addRow("Noise seed", sp)
        self.mode_stack.addWidget(gb)
        # bench
        gb = QtWidgets.QGroupBox("Bench loopback")
        gb.setToolTip("Transmit at low gain, receive the B210's internal leakage on RX2.")
        f = QtWidgets.QFormLayout(gb)
        self.w["bench_range_km"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 1e9)
        sp.setDecimals(3)
        sp.setSuffix(" km")
        sp.setToolTip("synthetic range; the leakage path has no delay, so keep this tiny to put the receive window on the transmission")
        f.addRow("Synthetic range", sp)
        self.w["bench_chunk_s"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.5, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Chunk length", sp)
        self.w["bench_t_off_min"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Minimum off time", sp)
        self.mode_stack.addWidget(gb)
        # signal generator
        gb = QtWidgets.QGroupBox("Signal generator")
        gb.setToolTip(TIPS["mode:siggen"])
        f = QtWidgets.QFormLayout(gb)
        self.w["siggen_signal"] = cb = QtWidgets.QComboBox()
        cb.addItems(["CW", "Two-tone", "EVE waveform"])
        cb.setToolTip(TIPS["siggen_signal"])
        cb.currentTextChanged.connect(lambda _t: self._live_signal())
        f.addRow("Signal", cb)
        self.w["siggen_offset_khz"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(-500.0, 500.0)
        sp.setDecimals(3)
        sp.setSuffix(" kHz")
        sp.setToolTip(TIPS["siggen_offset_khz"])
        sp.valueChanged.connect(lambda _v: self._live_signal())
        f.addRow("Offset from dial", sp)
        self.w["siggen_spacing_khz"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.001, 400.0)
        sp.setDecimals(3)
        sp.setSuffix(" kHz")
        sp.setToolTip(TIPS["siggen_spacing_khz"])
        sp.valueChanged.connect(lambda _v: self._live_signal())
        f.addRow("Two-tone spacing", sp)
        self.w["siggen_level_db"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(-60.0, 0.0)
        sp.setDecimals(1)
        sp.setSingleStep(0.5)
        sp.setSuffix(" dB")
        sp.setToolTip(TIPS["siggen_level_db"])
        sp.valueChanged.connect(lambda v: self._live_level(scale_db=float(v)))
        f.addRow("Digital scale", sp)
        self.w["siggen_pa"] = ck = QtWidgets.QCheckBox("amplifier in the chain: enforce the 7.2 limits (300 s on / 240 s off)")
        ck.setToolTip(TIPS["siggen_pa"])
        f.addRow("PA", ck)
        self.w["siggen_sim"] = ck = QtWidgets.QCheckBox("simulated radio (no B210): exercise the sequencer, the level controls and the report")
        ck.setToolTip(TIPS["siggen_sim"])
        f.addRow("Dry run", ck)
        self.w["siggen_sweep"] = ck = QtWidgets.QCheckBox("timed TX-gain sweep")
        ck.setToolTip(TIPS["siggen_sweep"])
        f.addRow("Sweep", ck)
        srow = QtWidgets.QHBoxLayout()
        for key, label, lo, hi in (("siggen_sweep_start_db", "from", 0.0, 89.75), ("siggen_sweep_stop_db", "to", 0.0, 89.75),
                                   ("siggen_sweep_step_db", "step", 0.25, 30.0), ("siggen_sweep_hold_s", "hold", 1.0, 600.0)):
            self.w[key] = sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setDecimals(2)
            sp.setSuffix(" s" if key.endswith("_s") else " dB")
            srow.addWidget(QtWidgets.QLabel(label))
            srow.addWidget(sp)
        srow.addStretch(1)
        f.addRow("", srow)
        self.lbl_siggen = QtWidgets.QLabel("The generator runs until STOP on the Run tab, where KEY / UNKEY drive the "
                                           "sequencer. Signal, offset, spacing, TX gain (Radio group) and the digital "
                                           "scale apply at once while it runs. The report lists every key event and step.")
        self.lbl_siggen.setWordWrap(True)
        self.lbl_siggen.setStyleSheet(f"color: {TEAL};")
        f.addRow("", self.lbl_siggen)
        self.mode_stack.addWidget(gb)

        # interop
        gb = QtWidgets.QGroupBox("Interop with a partner station")
        f = QtWidgets.QFormLayout(gb)
        self.w["interop_dir"] = cb = QtWidgets.QComboBox()
        cb.addItems(["Transmit only (the partner receives)", "Receive only (the partner transmits)"])
        cb.currentTextChanged.connect(lambda _t: self._coherence())
        f.addRow("Direction", cb)
        self.w["interop_chunk_s"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(10.0, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Block length", sp)
        self.w["interop_search_frames"] = sp = QtWidgets.QSpinBox()
        sp.setRange(0, 600)
        sp.setSuffix(" frames")
        f.addRow("Start search range", sp)
        lbl = QtWidgets.QLabel("Both ends use Variant A, full-length symbols, the same dial frequency, the same message, "
                               "and the same UTC start (below). The ORI generator sends no pilot: untick Pilot when receiving from it.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {TEAL};")
        f.addRow("", lbl)
        self.mode_stack.addWidget(gb)
        # eme
        gb = QtWidgets.QGroupBox("EME (Moon)")
        f = QtWidgets.QFormLayout(gb)
        self.w["eme_chunk_s"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.5, 3600.0)
        sp.setSuffix(" s")
        sp.setToolTip("monostatic Moon: transmit at most the round trip (2.5 s) minus the guard, then listen (design 4.6)")
        f.addRow("Chunk length", sp)
        self.w["eme_rtt_guard"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 60.0)
        sp.setDecimals(2)
        sp.setSuffix(" s")
        f.addRow("Round-trip guard", sp)
        self.w["eme_t_off_min"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Minimum off time", sp)
        self.w["eme_t_on_max"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(1.0, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Maximum on time", sp)
        self.w["eme_pa"] = ck = QtWidgets.QCheckBox("amplifier in the chain: enforce the 7.2 limits")
        ck.setToolTip("300 s on / 240 s off (design 7.2).")
        f.addRow("PA", ck)
        self.mode_stack.addWidget(gb)
        # venus
        gb = QtWidgets.QGroupBox("Venus")
        f = QtWidgets.QFormLayout(gb)
        self.w["sky_chunk_s"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(1.0, 3600.0)
        sp.setSuffix(" s")
        sp.setToolTip("monostatic: 240 s chunks against the 272 s round trip (design 4.2)")
        f.addRow("Chunk length", sp)
        self.w["sky_rtt_guard"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 600.0)
        sp.setSuffix(" s")
        f.addRow("Round-trip guard", sp)
        self.w["sky_t_on_max"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(1.0, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Maximum on time", sp)
        self.w["sky_t_off_min"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(0.0, 3600.0)
        sp.setSuffix(" s")
        f.addRow("Minimum off time", sp)
        self.w["sky_pa"] = ck = QtWidgets.QCheckBox("amplifier in the chain: enforce the 7.2 limits")
        f.addRow("PA", ck)
        self.mode_stack.addWidget(gb)
        left.addWidget(self.mode_stack)

        # sky common (EME + Venus)
        self.sky_box = gb = QtWidgets.QGroupBox("Sky session (EME and Venus)")
        f = QtWidgets.QFormLayout(gb)
        self.w["sky_mode"] = cb = QtWidgets.QComboBox()
        cb.addItems(["monostatic", "bistatic_tx"])
        cb.setToolTip("monostatic: DSES transmits and receives its own echo. bistatic_tx: DSES transmits only, a partner receives (chunked by the PA limits)")
        f.addRow("Station mode", cb)
        srow = QtWidgets.QHBoxLayout()
        self.w["sky_start_now"] = ck = QtWidgets.QCheckBox("start now +")
        srow.addWidget(ck)
        self.w["lead_s"] = sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(12.0, 3600.0)
        sp.setSuffix(" s")
        sp.setToolTip("seconds from Start to the first chunk (the radio needs about 8 s to arm; also used by the bench)")
        srow.addWidget(sp)
        srow.addWidget(QtWidgets.QLabel("   or at UTC"))
        self.w["sky_start_utc"] = dt = QtWidgets.QDateTimeEdit()
        dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        dt.setTimeSpec(QtCore.Qt.UTC)
        dt.setCalendarPopup(True)
        srow.addWidget(dt)
        srow.addStretch(1)
        f.addRow("Start", srow)
        self.w["sky_source"] = cb = QtWidgets.QComboBox()
        cb.addItems(["auto", "horizons", "astropy"])
        cb.setToolTip("JPL Horizons (needs internet) is primary; astropy with the local DE440s is the fallback (design 4.3)")
        f.addRow("Ephemeris", cb)
        self.w["sky_precomp"] = ck = QtWidgets.QCheckBox("pre-compensate TX Doppler for our own receiver")
        ck.setToolTip("Monostatic: the transmitted tone is steered so the echo lands on the nominal tone at Haswell.")
        f.addRow("Doppler", ck)
        self.w["sky_rx_doppler"] = ck = QtWidgets.QCheckBox("remove the model Doppler on receive")
        ck.setToolTip("For a receiver the transmitter did not pre-compensate for (bistatic).")
        f.addRow("", ck)
        left.addWidget(gb)
        left.addStretch(1)
        # Entry fields: an explicit small minimum (a layout uses it instead of the widget's
        # minimumSizeHint, which for a combo box is its widest item) and Expanding so the
        # form shrinks with the splitter yet the fields fill the row. NOT QSizePolicy.Ignored:
        # on macOS Qt's form layout (FieldsStayAtSizeHint) laid Ignored fields out at zero
        # width and the Setup page showed no fields at all (1.0.2, 2026-09-14).
        for wdg in left_w.findChildren(QtWidgets.QWidget):
            if not isinstance(wdg, (QtWidgets.QLineEdit, QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox)):
                continue
            wdg.setMinimumWidth(90)
            pol = wdg.sizePolicy()
            pol.setHorizontalPolicy(QtWidgets.QSizePolicy.Expanding)
            wdg.setSizePolicy(pol)
        for form in left_w.findChildren(QtWidgets.QFormLayout):
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)   # macOS default keeps fields at size hint
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

        # right: preview + buttons
        right.addWidget(QtWidgets.QLabel("Schedule preview"))
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet(mono() + " font-size: 11px;")
        right.addWidget(self.preview, 1)
        brow = QtWidgets.QHBoxLayout()
        self.btn_preview = QtWidgets.QPushButton("Preview schedule")
        self.btn_preview.setToolTip("build the schedule (and fetch the ephemeris for a sky session) without touching the radio")
        self.btn_preview.clicked.connect(self._preview)
        brow.addWidget(self.btn_preview)
        self.btn_start = QtWidgets.QPushButton("START")
        self.btn_start.setStyleSheet(f"background: {TEAL}; color: white; font-weight: bold; padding: 8px; font-size: 14px;")
        self.btn_start.clicked.connect(self._start)
        brow.addWidget(self.btn_start)
        self.btn_abort = QtWidgets.QPushButton("ABORT")
        self.btn_abort.setStyleSheet(ABORT_STYLE + " QPushButton { padding: 8px; font-size: 14px; }")
        self.btn_abort.clicked.connect(lambda: self.ctl.abort("operator abort from the Setup tab"))
        self.btn_abort.setEnabled(False)
        brow.addWidget(self.btn_abort)
        right.addLayout(brow)
        self.btn_save = QtWidgets.QPushButton("Save settings now")
        self.btn_save.clicked.connect(self._save)
        right.addWidget(self.btn_save)
        self.setup_log = QtWidgets.QPlainTextEdit()
        self.setup_log.setReadOnly(True)
        self.setup_log.setMaximumBlockCount(500)
        self.setup_log.setStyleSheet(mono() + " font-size: 11px;")
        right.addWidget(self.setup_log, 1)
        # operator help on every control, and on its row label
        for key, wd in self.w.items():
            tip = TIPS.get(key)
            if tip:
                wd.setToolTip(tip)
                lay = wd.parentWidget().layout() if wd.parentWidget() else None
                if isinstance(lay, QtWidgets.QFormLayout):
                    lbl = lay.labelForField(wd)
                    if lbl is not None:
                        lbl.setToolTip(tip)
        for b in self.mode_group.buttons():
            b.setToolTip(TIPS.get("mode:" + b.property("mode"), ""))
        self.btn_preview.setToolTip("Build the schedule with the current settings and show it here, without touching "
                                    "the radio. For EME and Venus this fetches the ephemeris, which takes a few seconds.")
        self.btn_start.setToolTip("Save the settings, program the GPS clock (if ticked), open the radio, build the "
                                  "schedule, and run it. The Run tab shows the session; the Report tab opens when it ends.")
        self.btn_abort.setToolTip("Release the key line at once, stop the streams, close the archive, and write the "
                                  "session log. The same as the ABORT button on the Run tab.")
        self.btn_save.setToolTip(f"Settings are saved on every Start and on exit; this saves them now. File: {self.settings.path}")
        self.preview.setToolTip("The schedule that Start would run: one line per chunk with its frames, transmit and "
                                "receive windows, round trip, elevation, and Doppler; then the total duration.")
        self.setup_log.setToolTip("What the controller did: GPS clock, radio open, schedule, session, offline decode, report.")
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.setup_log.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        return w

    # ---- settings <-> form -------------------------------------------------------------
    def _load(self) -> None:
        """Fill the form from the settings (legacy keyer labels mapped to the current one);
        the coherence rules are held off while loading."""
        self._loading = True
        s = self.settings
        mode = s.get("mode")
        for b in self.mode_group.buttons():
            b.setChecked(b.property("mode") == mode)
        for key, wd in self.w.items():
            v = s.get(key)
            if key == "keyer_kind":
                v = _LEGACY_KEYER_KINDS.get(str(v), v)
            if isinstance(wd, QtWidgets.QCheckBox):
                wd.setChecked(bool(v))
            elif isinstance(wd, QtWidgets.QComboBox):
                i = wd.findText(str(v))
                if i >= 0:
                    wd.setCurrentIndex(i)
                elif wd.isEditable():
                    wd.setEditText(str(v))
                else:
                    wd.setCurrentIndex(0)
            elif isinstance(wd, QtWidgets.QDoubleSpinBox):
                wd.setValue(float(v))
            elif isinstance(wd, QtWidgets.QSpinBox):
                wd.setValue(int(v))
            elif isinstance(wd, QtWidgets.QDateTimeEdit):
                dt = QtCore.QDateTime.fromString(str(v), QtCore.Qt.ISODate) if v else QtCore.QDateTime()
                if not dt.isValid():
                    dt = QtCore.QDateTime.currentDateTimeUtc().addSecs(600)
                wd.setDateTime(dt.toUTC())
            elif isinstance(wd, QtWidgets.QLineEdit):
                wd.setText(str(v))
        self._loading = False
        self._symbol_changed()

    def _values(self) -> Dict:
        """The form as a dict keyed like Settings.DEFAULTS plus 'mode'; keyer_kind is
        translated from its label to the kind name the worker expects (KEYER_KINDS)."""
        cfg: Dict = {"mode": self.mode()}
        for key, wd in self.w.items():
            if isinstance(wd, QtWidgets.QCheckBox):
                cfg[key] = wd.isChecked()
            elif isinstance(wd, QtWidgets.QComboBox):
                cfg[key] = wd.currentText()
            elif isinstance(wd, (QtWidgets.QDoubleSpinBox, QtWidgets.QSpinBox)):
                cfg[key] = wd.value()
            elif isinstance(wd, QtWidgets.QDateTimeEdit):
                cfg[key] = wd.dateTime().toUTC().toString(QtCore.Qt.ISODate)
            elif isinstance(wd, QtWidgets.QLineEdit):
                cfg[key] = wd.text().strip()
        cfg["keyer_kind"] = KEYER_KINDS.get(str(cfg.get("keyer_kind", "none")), "none")   # label -> kind for the worker
        return cfg

    def _save(self) -> None:
        """File > Save settings (Ctrl+S), also run on Start and at exit: write the form, the
        window geometry, and the splitter states to the INI file."""
        cfg = self._values()
        for k, v in cfg.items():
            self.settings.set(k, v)
        self.settings.set("geometry", self.saveGeometry())
        self.settings.set("setup_split", self.setup_split.saveState())
        self.settings.set("report_split", self.report.split.saveState())
        self.settings.set("run_hsplit", self.panel.hsplit.saveState())
        self.settings.set("run_vsplit", self.panel.vsplit.saveState())
        self.settings.sync()
        self.status.setText(f"settings saved to {self.settings.path}")

    def mode(self) -> str:
        """The selected run mode key: sim, bench, interop, eme, or eve (bench if none is
        checked)."""
        b = self.mode_group.checkedButton()
        return b.property("mode") if b is not None else "bench"

    def _refresh_ports(self) -> None:
        """Refresh button beside the USB board port: re-scan the serial ports into the combo
        ('auto' first), keeping the current choice."""
        from .keyer import list_serial_ports
        pc = self.w["keyer_port"]
        keep = pc.currentText()
        pc.blockSignals(True)
        pc.clear()
        pc.addItem("auto")
        for d in list_serial_ports():
            pc.addItem(d)
        if keep:
            i = pc.findText(keep)
            if i >= 0:
                pc.setCurrentIndex(i)
            else:
                pc.setEditText(keep)
        pc.blockSignals(False)

    def _keyer_changed(self) -> None:
        """Switching combo changed: enable the port, guard-time, and test controls only for
        the sequencer and describe the outputs under the group (guide 4.3, design 7.2)."""
        k = KEYER_KINDS.get(self.w["keyer_kind"].currentText(), "none")
        seq = k == "sequencer"
        for key in ("keyer_port", "keyer_lna_guard_ms", "keyer_lna_release_ms"):
            self.w[key].setEnabled(seq)
        self.btn_testkey.setEnabled(seq)
        self.btn_testlna.setEnabled(seq)
        self.lbl_keyer.setText({"none": "Nothing is switched: fine for the loopback bench, simulation, and receive only.",
                                "sequencer": "Relay 1 / GPIO_0 = TX key (energized or high = transmit); relay 2 / GPIO_1 = LNA "
                                             "(energized or high = LNA off). Both released = receive. The USB board is found by "
                                             "itself; without it the GPIO lines carry on and the run says so. "
                                             "docs/hardware/diustou_dstur_t20.md."}.get(k, ""))

    def _test_output(self, which: str) -> None:
        """Click one relay of the USB board for a second (TX = relay 1, LNA = relay 2)."""
        from .keyer import find_relay_board, UsbRelayBoard
        hint = self.w["keyer_port"].currentText()
        self.status.setText("looking for the USB relay board ...")
        QtWidgets.QApplication.processEvents()
        port = find_relay_board(hint)
        if not port:
            self.status.setText("no USB relay board answered on any COM port")
            self._log("test: no USB relay board answered (auto search of the COM ports)")
            return
        try:
            b = UsbRelayBoard(port)
            b.log = self._log
            b.open()
            if which == "tx":
                b.set_tx(True)
                self._log(f"test: TX key relay energized on {port} (1 s)")
            else:
                b.set_lna(False)
                self._log(f"test: LNA control relay energized = LNA OFF on {port} (1 s)")
            QtWidgets.QApplication.processEvents()
            time.sleep(1.0)
            self._log(f"test: status {b.query()!r}")
            b.set_tx(False)
            b.set_lna(True)
            self._log(f"test: released; status {b.query()!r}; fault: {b.fault or 'none'}")
            b.close()
            self.status.setText(f"test {which.upper()} on {port}: {'FAULT ' + b.fault if b.fault else 'ok'}")
        except Exception as e:      # noqa: BLE001
            self.status.setText(f"test {which.upper()} failed: {e}")
            self._log(f"test {which.upper()} failed: {e}")

    def _reference_changed(self) -> None:
        """Clock source or mode changed: the Leo Bodnar box is enabled only for the external
        clock outside simulation."""
        ext = self.w["clock"].currentText() == "external" and self.mode() != "sim"
        self.w["gpsdo"].setEnabled(ext)

    def _apply_values(self, d: Dict) -> None:
        """Set form widgets from a key -> value dict (unknown keys ignored; the date-time
        field is not covered)."""
        for key, v in d.items():
            wd = self.w.get(key)
            if wd is None:
                continue
            if isinstance(wd, QtWidgets.QCheckBox):
                wd.setChecked(bool(v))
            elif isinstance(wd, QtWidgets.QComboBox):
                i = wd.findText(str(v))
                if i >= 0:
                    wd.setCurrentIndex(i)
            elif isinstance(wd, QtWidgets.QDoubleSpinBox):
                wd.setValue(float(v))
            elif isinstance(wd, QtWidgets.QSpinBox):
                wd.setValue(int(v))
            elif isinstance(wd, QtWidgets.QLineEdit):
                wd.setText(str(v))

    def _set_defaults(self) -> None:
        """'Set defaults for this run mode' button: apply COMMON_DEFAULTS plus the mode's
        MODE_DEFAULTS, then the coherence rules; the keys that changed go to the status line
        and the log."""
        m = self.mode()
        d = dict(COMMON_DEFAULTS)
        d.update(MODE_DEFAULTS[m])
        before = self._values()
        self._apply_values(d)
        changed = [k for k in d if before.get(k) != self._values().get(k)]
        self._coherence()
        msg = f"defaults for {m}: " + (", ".join(changed) if changed else "nothing to change")
        self.lbl_coherence.setText(msg)
        self.status.setText(msg)
        self._log(msg)

    def _coherence(self) -> List[str]:
        """Run _coherence_rules() unless the form is loading or the rules are already running
        (a setter they call re-enters here); returns the notes."""
        if getattr(self, "_loading", False) or getattr(self, "_in_coherence", False):
            return []          # not while loading, and not re-entered from a setter it just called
        self._in_coherence = True
        try:
            return self._coherence_rules()
        finally:
            self._in_coherence = False

    def _coherence_rules(self) -> List[str]:
        """Rules that keep the settings consistent with the run mode and the variant; applied
        after a mode, variant, symbol-length, or interop-direction change. Returns notes."""
        m = self.mode()
        notes: List[str] = []
        v = self.w["variant"].currentText()
        p = EveParams.named(v)
        # chunks must hold the pilot plus data (bench, sim, interop use the bench chunk)
        if self.w["pilot"].isChecked():
            need_s = (int(self.w["pilot_frames_test"].value()) + 2) / p.r_bw if not self.w["full_symbol"].isChecked() else (p.pilot_frames + 2) / p.r_bw
            for key in (("bench_chunk_s",) if m in ("bench", "sim") else ("interop_chunk_s",) if m == "interop" else ()):
                if self.w[key].value() < need_s:
                    self.w[key].setValue(math.ceil(need_s * 10 - 1e-9) / 10.0)
                    notes.append(f"chunk raised to {self.w[key].value():.1f} s (Variant {v}: {p.t_frame:.3f} s frames, pilot + 2)")
        if m in ("eme", "eve", "interop"):
            if not self.w["full_symbol"].isChecked():
                self.w["full_symbol"].setChecked(True)
                notes.append("full-length symbols (the air interface)")
            if self.w["clock"].currentText() == "internal":
                self.w["clock"].setCurrentText("external")
                notes.append("clock source external (no internal reference on the air)")
        if m == "eve":
            if abs(self.w["f_dial_mhz"].value() - 1296.0) < 1e-6:
                self.w["f_dial_mhz"].setValue(1299.5)
                notes.append("dial 1299.5 MHz (Venus 2026 with the 23 cm package)")
            if not self.w["sky_pa"].isChecked():
                self.w["sky_pa"].setChecked(True)
                notes.append("amplifier limits on")
            if self.w["repeat"].value() < 2:
                self.w["repeat"].setValue(5)
                notes.append("repeat 5")
        if m in ("eme", "eve") and self.w["keyer_kind"].currentText() == "none":
            notes.append("NO KEY LINE selected: the sequencer will not be keyed (fine only for the bare-B210 EME test)")
        if self.w["keyer_kind"].currentText() != "none":
            # the sequencer needs lead + lag + LNA guard + release + the USB board's four
            # acknowledged switches between two chunks' RF (the session's preflight checks it)
            from .keyer import sequencer_gap_s
            need = math.ceil((sequencer_gap_s(self.w["keyer_lna_guard_ms"].value() / 1e3,
                                              self.w["keyer_lna_release_ms"].value() / 1e3) + 0.1) * 10) / 10
            for key, label in (("bench_t_off_min", "bench"), ("eme_t_off_min", "Moon")):
                if key in self.w and self.w[key].value() < need:
                    self.w[key].setValue(need)
                    notes.append(f"{label} off time {need:.1f} s (the sequencer needs the gap)")
        if m == "bench" and self.w["tx_gain"].value() > 20.0:
            self.w["tx_gain"].setValue(0.0)
            notes.append("TX gain 0 dB (loopback needs none)")
        if m == "interop":
            rx_only = self.w["interop_dir"].currentText().startswith("Receive")
            if rx_only and self.w["pilot"].isChecked():
                self.w["pilot"].setChecked(False)
                notes.append("pilot off (ORI's generator sends none)")
            if not rx_only and not self.w["pilot"].isChecked():
                self.w["pilot"].setChecked(True)
                notes.append("pilot on")
        if m == "sim" and self.w["sim_range_km"].value() < 100.0 and self.w["bench_chunk_s"].value() < 1.0:
            self.w["bench_chunk_s"].setValue(2.4)
            notes.append("chunk 2.4 s")
        self._reference_changed()
        self._symbol_changed()
        if notes:
            msg = "adjusted for " + m + ": " + "; ".join(notes)
            self.lbl_coherence.setText(msg)
            self.status.setText(msg)
            self._log(msg)
        return notes

    def _mode_changed(self, user: bool = False) -> None:
        """Run-mode radio clicked (user=True) or start-up: switch the mode-specific group,
        show the sky group, enable the radio and keying controls outside simulation, and
        apply the coherence rules when the operator made the change."""
        if hasattr(self, "gen_bar"):
            self.gen_bar.setVisible(self.mode() == "siggen")
        m = self.mode()
        self.mode_stack.setCurrentIndex([k for k, _ in MODES].index(m))
        self.sky_box.setVisible(m in ("eme", "eve", "interop"))
        self.w["tx_gain"].setEnabled(True)
        for key in ("sky_mode", "sky_source", "sky_precomp", "sky_rx_doppler"):
            self.w[key].setEnabled(m in ("eme", "eve"))
        radio = m != "sim"
        for key in ("serial", "tx_gain", "rx_gain", "clock", "time_host", "gpsdo", "lo_offset_khz", "tx_port", "rx_port"):
            self.w[key].setEnabled(radio)
        self.w["keyer_kind"].setEnabled(radio)
        self._symbol_changed()
        self._reference_changed()
        self._keyer_changed()
        self._ports_changed()
        if user:
            self._coherence()
        else:
            self.lbl_coherence.setText("")

    def _symbol_changed(self) -> None:
        """Variant, full-length, or test-frames changed: rewrite the symbol-length line
        (seconds per symbol, minutes per pass, the one-pass C/N0 threshold from
        cn0_threshold_db) and enable the test-length fields only for a test symbol."""
        v = self.w["variant"].currentText()
        p = EveParams.named(v)
        full = self.w["full_symbol"].isChecked()
        n_full = FULL_FRAMES.get(v, p.n_frames)
        n = n_full if full else int(self.w["n_frames_test"].value())
        t_sym = n * p.n_fft / p.modem_rate
        thr = cn0_threshold_db(v, n)
        self.lbl_sym.setText(f"{n} frames per symbol = {t_sym:.1f} s; one message pass = {t_sym * p.n_sym / 60:.1f} min; "
                             f"decodes above about {thr:+.1f} dB-Hz C/N0 in one pass"
                             + ("" if full else f"   (TEST length: no on-air significance; full length {cn0_threshold_db(v, n_full):+.1f} dB-Hz)"))
        for key in ("n_frames_test", "pilot_frames_test"):
            self.w[key].setEnabled(not full)

    def _ports_changed(self) -> None:
        """TX frontend or RX port changed: warn in red when the receive port is the
        transmitting frontend's TX/RX, note in teal when the choice is not the station wiring
        (ICD 7.1)."""
        tx = self.w["tx_port"].currentText()
        rx = self.w["rx_port"].currentText()
        if rx.endswith("TX/RX") and rx.startswith(tx):
            self.lbl_ports.setText(f"Not allowed: receive on {rx} while frontend {tx} transmits. Pick an RX2 port "
                                   f"or the other frontend's TX/RX.")
            self.lbl_ports.setStyleSheet("color: #c0392b;")
        elif tx != "A" or rx != "A : RX2":
            self.lbl_ports.setText(f"TX on frontend {tx}, RX on {rx}: not the station wiring (TX/RX A to the driver, "
                                   f"LNA to RX2 A). Fine on the bench; on the air move the cables to match.")
            self.lbl_ports.setStyleSheet(f"color: {TEAL};")
        else:
            self.lbl_ports.setText("")

    def _live_signal(self) -> None:
        """Signal, offset or spacing changed while a signal-generator run is active: send the
        new signal to the worker (applied at the next sample block, keyed or not)."""
        if self.mode() == "siggen" and self.ctl.busy and self.ctl.conn is not None and not self._loading:
            kind = {"CW": "cw", "Two-tone": "two_tone", "EVE waveform": "eve"}.get(self.w["siggen_signal"].currentText(), "cw")
            try:
                self.ctl.conn.send(("signal", kind, float(self.w["siggen_offset_khz"].value()) * 1e3,
                                    float(self.w["siggen_spacing_khz"].value()) * 1e3))
            except Exception:
                pass

    def _gen_command(self, *cmd) -> None:
        """KEY / UNKEY / Sweep / STOP from the Run tab's generator bar."""
        if self.ctl.busy and self.ctl.conn is not None:
            try:
                self.ctl.conn.send(tuple(cmd))
            except Exception:
                pass

    def _live_level(self, tx_gain_db=None, scale_db=None) -> None:
        """TX gain or digital scale changed while a signal-generator run is active: send the
        changed value to the worker (applied at once; recorded as a level step). Only the
        changed one goes, so turning the scale during a sweep leaves the sweep's gain alone."""
        if self.mode() == "siggen" and self.ctl.busy and self.ctl.conn is not None and not self._loading:
            try:
                self.ctl.conn.send(("level", tx_gain_db, scale_db))
            except Exception:
                pass

    def _browse_archive(self) -> None:
        """Browse button: choose the archive folder and point the Report tab at it."""
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Archive folder", self.w["archive"].text() or ".")
        if d:
            self.w["archive"].setText(d)
            self.report.set_archive(d)

    # ---- actions -------------------------------------------------------------------------
    def _log(self, s: str) -> None:
        """Append a time-stamped line to the Setup tab log and the Run tab log."""
        self.setup_log.appendPlainText(f"{iso_utc(time.time(), 0)[11:19]}  {s}")
        self.panel.append_log(s)

    def _preview(self) -> None:
        """'Preview schedule' button: save the settings and launch a preview-only worker; the
        text arrives through preview_ready."""
        self._save()
        self.preview.setPlainText("building the schedule ...")
        if not self.ctl.start(self._values(), preview_only=True):
            self.preview.setPlainText("busy")

    def _notice(self, text: str) -> None:
        """Something the operator must know but that must not stop a timed run: a
        non-modal box plus the status bar and the log."""
        self.status.setText(text)
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning, "EVE modem", text, QtWidgets.QMessageBox.Ok, self)
        box.setModal(False)
        box.show()
        self._notice_box = box

    def _preview_ready(self, text: str) -> None:
        """Worker preview text: show it in the schedule preview box."""
        self.preview.setPlainText(text)

    def _start(self) -> None:
        """START button: save, confirm a test-length symbol on the air, point the Report tab
        at the archive, launch the run, and switch to the Run tab."""
        self._save()
        cfg = self._values()
        if cfg["mode"] in ("eme", "eve", "interop") and cfg["full_symbol"] is False:
            r = QtWidgets.QMessageBox.question(self, "Test-length symbols on the air",
                                               "The symbol length is a TEST length, not the 473-frame air interface. "
                                               "A partner station could not decode this. Start anyway?")
            if r != QtWidgets.QMessageBox.Yes:
                return
        self.report.set_archive(cfg["archive"])
        self.preview.setPlainText("")
        if self.ctl.start(cfg):
            self.tabs.setCurrentWidget(self.run_page)
        else:
            self.status.setText("a run is already in progress")

    def _build_generator_bar(self) -> QtWidgets.QWidget:
        """The signal generator's controls on the Run tab: KEY / UNKEY (through the
        sequencer), Sweep (run the timed gain sweep now), STOP (end the run cleanly, the
        report is written). Visible in the signal generator mode only; enabled while it runs."""
        bar = QtWidgets.QGroupBox("Signal generator")
        h = QtWidgets.QHBoxLayout(bar)
        self.btn_gen_key = QtWidgets.QPushButton("KEY")
        self.btn_gen_key.setStyleSheet("QPushButton { background: #c0392b; color: white; font-weight: bold; padding: 8px 18px; font-size: 14px; }"
                                       "QPushButton:disabled { background: #d9a7a1; color: #f4f4f4; }")
        self.btn_gen_key.setToolTip("Key the transmitter: LNA off, guard, TX key (relay 1 / GPIO_0), then the RF after the key lead. "
                                    "With the amplifier limits ticked the key is released after 300 s and refused for 240 s after a release.")
        self.btn_gen_key.clicked.connect(lambda: self._gen_command("key", True))
        self.btn_gen_unkey = QtWidgets.QPushButton("UNKEY")
        self.btn_gen_unkey.setStyleSheet("QPushButton { background: #1e7d3a; color: white; font-weight: bold; padding: 8px 18px; font-size: 14px; }"
                                         "QPushButton:disabled { background: #a6c9b0; color: #f4f4f4; }")
        self.btn_gen_unkey.setToolTip("Release the transmitter: RF off, key lag, TX key off, release, LNA on. The generator stays up; KEY again when ready.")
        self.btn_gen_unkey.clicked.connect(lambda: self._gen_command("key", False))
        self.btn_gen_sweep = QtWidgets.QPushButton("Sweep")
        self.btn_gen_sweep.setToolTip("Run the timed TX-gain sweep now (the key must be down; the sweep from the Setup tab must be ticked).")
        self.btn_gen_sweep.clicked.connect(lambda: self._gen_command("sweep"))
        self.btn_gen_stop = QtWidgets.QPushButton("STOP generator")
        self.btn_gen_stop.setToolTip("End the run cleanly: the key is released, the radio closed, the report written. ABORT does the same and marks the run aborted.")
        self.btn_gen_stop.clicked.connect(lambda: self._gen_command("stop"))
        for b in (self.btn_gen_key, self.btn_gen_unkey, self.btn_gen_sweep, self.btn_gen_stop):
            b.setEnabled(False)
            h.addWidget(b)
        self.lbl_gen = QtWidgets.QLabel("TX gain, digital scale and the signal change live on the Setup tab.")
        self.lbl_gen.setWordWrap(True)
        self.lbl_gen.setStyleSheet(f"color: {TEAL};")
        h.addWidget(self.lbl_gen, 1)
        bar.setVisible(False)
        self.gen_bar = bar
        return bar

    def _session_ready(self, sess) -> None:
        """Worker sent the session: bind the operator panel to it and show the schedule in
        the preview box."""
        self.panel.bind(sess)
        self.ctl.panel_bound()
        self.preview.setPlainText(S.describe(sess.sched))

    def _session_done(self) -> None:
        """Worker is done with the session: unbind the panel (its last picture stays)."""
        self.panel.unbind()          # keeps what is drawn; drops the references
        self.ctl.panel_released()

    def _state(self, st: str) -> None:
        """Controller state changed: status bar, the busy badge before a session exists, and
        the enable state of START, Preview, ABORT, and Re-decode."""
        self.status.setText(st)
        running = st != "idle"
        if self.ctl.session is None and st != "idle":
            self.panel.set_phase(st, "busy")       # before the session exists (opening the radio, ephemeris, schedule)
        self.btn_start.setEnabled(not running)
        self.btn_preview.setEnabled(not running)
        self.btn_abort.setEnabled(st in ("preparing", "running"))
        gen_on = st == "running" and self.mode() == "siggen"
        for b in (self.btn_gen_key, self.btn_gen_unkey, self.btn_gen_sweep, self.btn_gen_stop):
            b.setEnabled(gen_on)
        self.report.btn_redecode.setEnabled(not running)

    def _finished(self, result: Dict) -> None:
        """Run result from the worker: warn on an error, refresh the Report tab, and show the
        new report with the verdict in the status bar."""
        pdf = result.get("pdf")
        if result.get("error"):
            self.status.setText("failed: " + result["error"])
            QtWidgets.QMessageBox.warning(self, "Run failed", result["error"])
        self.report.refresh()
        if pdf:
            self.report.select(Path(pdf))
            self.tabs.setCurrentWidget(self.report)
            self.status.setText(("DECODED  " if result.get("ok") else "not decoded  ") + result.get("session_id", ""))

    def _redecode(self, session_json: Path) -> None:
        """Report tab asked for a re-decode: launch it through the controller, or say busy."""
        if not self.ctl.redecode(session_json):
            self.status.setText("busy")

    # ---- updates (Help menu; eve/update_ui.py) --------------------------------------------
    def _start_update_check(self) -> None:
        """Create the start-up UpdateChecker and, when auto_check is on and the last check
        (last_check_iso) is older than check_interval_hours, run it 2.5 s after start (guide
        section 3)."""
        from . import update_ui as U
        self._update_checker = U.UpdateChecker(self.settings, parent=self)
        self._update_checker.update_available.connect(self._show_update_dialog)
        if not self.settings.get("auto_check") or not str(self.settings.get("manifest_url")).strip():
            return
        last = str(self.settings.get("last_check_iso")).strip()
        if last:
            try:
                from datetime import datetime
                if (datetime.now() - datetime.fromisoformat(last)).total_seconds() < max(1, int(self.settings.get("check_interval_hours"))) * 3600:
                    return
            except ValueError:
                pass
        QtCore.QTimer.singleShot(2500, self._update_checker.check_now)

    def _check_updates_manual(self) -> None:
        """Help > Check for updates: check the manifest now and answer with 'nothing to do',
        the update dialog, or the failure in a message box."""
        from . import update_ui as U
        url = str(self.settings.get("manifest_url")).strip()
        if not url:
            QtWidgets.QMessageBox.information(self, "Updates", f"No manifest URL is set (manifest_url in {self.settings.path}).")
            return
        chk = U.UpdateChecker(self.settings, parent=self)
        chk.update_available.connect(self._show_update_dialog)
        chk.no_update.connect(lambda latest: QtWidgets.QMessageBox.information(
            self, "Updates", f"You are running {__version__}; the published version is {latest}. Nothing to do."))
        chk.check_failed.connect(lambda msg: QtWidgets.QMessageBox.warning(self, "Update check failed", msg))
        self._manual_checker = chk
        self.status.setText("checking for updates …")
        chk.check_now()

    def _show_update_dialog(self, latest: str, url: str, notes: str) -> None:
        """An update is available: show UpdateNotificationDialog unless the start-up check
        found a version the operator skipped (dismissed_version); Skip records the version,
        Install goes to _install_update."""
        from . import update_ui as U
        dismissed = str(self.settings.get("dismissed_version")).strip()
        if dismissed and U.parse_version(latest) <= U.parse_version(dismissed) and self.sender() is getattr(self, "_update_checker", None):
            return
        dlg = U.UpdateNotificationDialog(latest, url, notes, parent=self)
        dlg.dismissed_for_version.connect(lambda v: (self.settings.set("dismissed_version", v), self.settings.sync()))
        dlg.install_requested.connect(self._install_update)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._update_dialog = dlg
        self.status.setText(f"update {latest} available")

    def _install_update(self, download_url: str, latest: str) -> None:
        """Install Update requested: refuse during a run, ask where (InstallUpdateDialog),
        then run UpdateInstaller under a progress dialog."""
        from . import update_ui as U
        if not download_url:
            return
        if self.ctl.busy:
            QtWidgets.QMessageBox.information(self, "Updates", "A run is in progress; install the update after it ends.")
            return
        install_dir = Path(__file__).resolve().parents[1]
        choose = U.InstallUpdateDialog(install_dir, latest, parent=self)
        if choose.exec() != QtWidgets.QDialog.Accepted:
            return
        mode, dest, make_shortcut = choose.result_choice()
        prog = QtWidgets.QProgressDialog("Preparing…", "", 0, 0, self)
        prog.setWindowTitle("Installing the update")
        prog.setCancelButton(None)
        prog.setWindowModality(QtCore.Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        inst = U.UpdateInstaller(download_url, mode, dest, make_shortcut, latest, parent=self)

        def on_progress(done_b, total_b):
            if total_b > 0:
                prog.setMaximum(total_b)
                prog.setValue(done_b)
            else:
                prog.setRange(0, 0)
        inst.status.connect(prog.setLabelText)
        inst.progress.connect(on_progress)
        inst.done.connect(lambda ok, msg, path: self._on_install_done(ok, msg, mode, install_dir, prog))
        self._installer = inst
        self._install_progress = prog
        prog.show()
        inst.start()

    def _on_install_done(self, ok: bool, msg: str, mode: str, install_dir: Path, prog) -> None:
        """Installer finished: report a failure (nothing was changed), offer a restart after
        an in-place install (update_ui.relaunch, then close), or say where the new copy went."""
        from . import update_ui as U
        prog.close()
        if not ok:
            QtWidgets.QMessageBox.critical(self, "Update failed", f"{msg}\n\nThe current installation was left unchanged.")
            return
        if mode == "in_place":
            r = QtWidgets.QMessageBox.question(self, "Update installed",
                                               "The update was installed over the current version.\n\nRestart now to use it?",
                                               QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.Yes)
            if r == QtWidgets.QMessageBox.Yes:
                U.relaunch(install_dir)
                self.close()
        else:
            QtWidgets.QMessageBox.information(self, "Update installed", msg)

    def _help(self) -> None:
        """Help > Operator's guide (F1): show the HelpDialog, created on first use."""
        if self._help_dlg is None:
            self._help_dlg = HelpDialog(self)
        self._help_dlg.show()
        self._help_dlg.raise_()

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        """Window close (also File > Exit): confirm when a run is in progress and abort it,
        save the settings, and stop the worker process."""
        if self.ctl.busy:
            r = QtWidgets.QMessageBox.question(self, "A run is in progress", "Abort the run and quit?")
            if r != QtWidgets.QMessageBox.Yes:
                ev.ignore()
                return
            self.ctl.abort("window closed")
            time.sleep(1.0)
        self._save()
        self.ctl.close_radio()
        ev.accept()


def main(argv=None) -> int:
    """Entry point of eve_app.py: create the QApplication (name 'DSES EVE modem', window icon
    icons/eve_modem.ico), show EveApp, and return the event loop's exit code."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("DSES EVE modem")
    icon = Path(__file__).resolve().parents[1] / "icons" / "eve_modem.ico"
    if icon.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon)))
    win = EveApp()
    win.show()
    return app.exec()
