"""B210 setup for a session (design document 5.1, 7.1, 7.2): external reference and PPS,
ref_locked readback, UTC time on a PPS edge, IF-offset tuning with readback, rate
readback, the GPIO keying line. Built on the Workbench's shared dses_radio layer.

One B210 serves both directions: TX/RX A drives the amplifier chain (or the feed
directly for EME), RX2 A takes the LNA. The modem refuses to start with the receive
port on TX/RX (7.1). Both gr-uhd blocks open the same device (UHD caches the handle), so
reference, time, and GPIO settings made through the source apply to the sink too.

LO placement. The comb sits f_IF .. f_IF + BW above the dial frequency (25 to 48.5 kHz for
Variant A). With the LO on the dial frequency its leakage would land 25 kHz below tone 0
and, after the receive mix and decimation to the modem rate, alias into the comb. Both
directions therefore park the LO lo_offset_hz away (default -300 kHz: leakage well outside
the decimator's passband, inside the radio's analog bandwidth), using the verified tune
path; if the offset cannot be applied honestly the radio reports it and the session
tool decides.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ._workbench import import_dses_radio
from .params import EveParams

R = import_dses_radio()
from gnuradio import uhd  # noqa: E402  (after dses_radio, which sets UHD_IMAGES_DIR)


@dataclass
class RadioConfig:
    """Everything the session tool or the application decides about the B210 before it is
    opened (design document 7.1). Frequencies in Hz, gains in dB. clock_source selects the
    10 MHz reference ('external' = the GPS clock on REF IN, 'gpsdo' = a board GPSDO,
    'internal' = the TCXO, bench only); time_source None follows the clock source, 'host'
    means no PPS is available and the device time is set from the host's NTP clock instead
    (4.3). key_bank / key_mask name the J504 lines the sequencer drives: GPIO_0 = TX key,
    GPIO_1 = LNA control, both low = receive (7.2, decision D24)."""
    serial: str = ""                    # empty = first B2xx found
    f_dial_hz: float = 1299.5e6
    tx_gain_db: float = 0.0             # B210 TX gain; the session tool sets the real value
    rx_gain_db: float = 40.0
    clock_source: str = "external"      # external | gpsdo | internal (bench only)
    time_source: Optional[str] = None   # default: follows clock_source; "host" = no PPS available:
                                        # device time set from the host clock (NTP) with set_time_now
    require_ref_lock: bool = True       # refuse to run unless ref_locked (7.1)
    lo_offset_hz: float = -300e3
    tx_antenna: str = "TX/RX"
    tx_frontend: str = "A"              # B210 frontend for the transmitter: A or B (its TX/RX port)
    rx_antenna: str = "A : RX2"         # receiver frontend and port (Workbench naming): "A : RX2",
                                        # "B : RX2", or the TX/RX port of the frontend the TX does not use
    key_bank: str = "FP0"
    key_mask: int = 0x03                # GPIO_0 = TX key (high = transmit), GPIO_1 = LNA (high = off)
    rx_stream_args: str = "recv_frame_size=8192,num_recv_frames=1024"


def check_ports(cfg: "RadioConfig") -> None:
    """The station rule (ICD 7.1): the receiver takes an RX2 port, TX/RX belongs to the
    transmitter. A TX/RX port is accepted for receive only on the frontend the transmitter
    is NOT using (bench comparisons of a suspect RX2 port); the same frontend's TX/RX for
    both directions would put the transmitter into its own receiver through the T/R switch."""
    fe = (cfg.tx_frontend or "A").strip().upper()[:1]
    rx = cfg.rx_antenna.strip()
    if rx.endswith("TX/RX"):
        rx_fe = rx.split(":")[0].strip().upper()[:1] if ":" in rx else "A"
        if rx_fe == fe:
            raise ValueError(f"receive on {rx} while the transmitter is on frontend {fe}: use an RX2 port, "
                             f"or the other frontend's TX/RX (ICD 7.1)")


@dataclass
class RadioStatus:
    """A snapshot of the open radio as read back from UHD: reference and lock state, device
    time (seconds on the USRP's own clock) and the last PPS edge, the time-set error against
    the PPS or the host clock (seconds; None before the time was set), the actual sample
    rates (S/s), the tuned frequencies (Hz), whether each LO offset was applied honestly,
    the gains (dB) and the ports. summary() is the one line the session log carries."""
    serial: str
    ref_locked: bool
    clock_source: str
    time_source: str
    device_time: float
    time_last_pps: float
    pps_error_s: Optional[float]
    rx_rate: float
    tx_rate: float
    rx_freq: float
    tx_freq: float
    rx_lo_offset_ok: bool
    tx_lo_offset_ok: bool
    rx_gain: float
    tx_gain: float
    rx_antenna: str
    tx_antenna: str

    def summary(self) -> str:
        """One line with every field, for the log and the session report's radio entry."""
        return (f"B210 {self.serial}: ref {self.clock_source}/{self.time_source} locked={self.ref_locked}; "
                f"device time {self.device_time:.3f} (last PPS {self.time_last_pps:.0f}, set error "
                f"{'n/a' if self.pps_error_s is None else f'{self.pps_error_s:+.6f} s'}); "
                f"rx {self.rx_rate:.2f} S/s @ {self.rx_freq / 1e6:.6f} MHz gain {self.rx_gain:.0f} dB on {self.rx_antenna} "
                f"(LO offset {'ok' if self.rx_lo_offset_ok else 'FELL BACK'}); "
                f"tx {self.tx_rate:.2f} S/s @ {self.tx_freq / 1e6:.6f} MHz gain {self.tx_gain:.0f} dB on {self.tx_antenna} "
                f"(LO offset {'ok' if self.tx_lo_offset_ok else 'FELL BACK'})")


class EveRadio:
    """The session's B210. open() builds both blocks, sets the reference, checks lock,
    sets the time; the flowgraph then owns rx/tx. key() drives the sequencer line."""

    def __init__(self, params: EveParams, cfg: Optional[RadioConfig] = None):
        self.p = params
        self.cfg = cfg or RadioConfig()
        self.rx = None          # dses_radio.UhdB200Source
        self.tx = None          # uhd.usrp_sink
        self.tx_events = None   # gr_blocks.TxEventCounter, fed by the sink's async_msgs port
        self.rx_rate = 0.0
        self.tx_rate = 0.0
        self.rx_lo_ok = False
        self.tx_lo_ok = False
        self.pps_error_s: Optional[float] = None
        self._keyed = False
        self.host_timed = False
        self.log = lambda s: print(s, file=sys.stderr)

    # ---- lifecycle -----------------------------------------------------------------------
    def open(self, set_time: bool = True) -> RadioStatus:
        """create() then configure(). The application's worker calls the two halves
        itself, with the GPS clock preflight in between: see create()."""
        self.create()
        return self.configure(self.p, self.cfg, set_time)

    def create(self) -> None:
        """Construct the USRP source and sink (one device open, about 8 s with the FPGA
        image load). Do NOTHING on USB right before this: the constructor faulted
        (access violation, 1 in 10) when the Leo Bodnar clock's HID handle had just
        been closed by the preflight; libusb's enumeration and a device-change race.
        So: create first, talk to the clock next, configure last. Never create a second
        source for the same B210 in one process while one exists."""
        cfg = self.cfg
        if not cfg.serial:
            devs = R.find_b200_uhd()
            if not devs:
                raise RuntimeError("no B2xx device found")
            cfg.serial = devs[0]["serial"]
        check_ports(cfg)
        rate = self.p.radio_rate
        self.rx = R.UhdB200Source(cfg.serial, rate, cfg.f_dial_hz, cfg.rx_gain_db,
                                  antenna=cfg.rx_antenna, stream_args=cfg.rx_stream_args)
        self.tx = R.make_usrp_sink(cfg.serial, rate, cfg.f_dial_hz, cfg.tx_gain_db,
                                   antenna=cfg.tx_antenna, lo_offset_hz=0.0)
        from .gr_blocks import TxEventCounter
        self.tx_events = TxEventCounter()
        self._tx_frontend = "A"
        self._set_tx_frontend(cfg.tx_frontend)

    def _set_tx_frontend(self, fe: str) -> None:
        """Route TX channel 0 to frontend A or B (B210 subdev "A:A" / "A:B"). The sink is
        created on A; switching re-initialises the frontend, so only do it on a change."""
        fe = (fe or "A").strip().upper()[:1]
        if fe not in ("A", "B"):
            raise ValueError(f"TX frontend must be A or B, not {fe!r}")
        if fe != self._tx_frontend:
            self.tx.set_subdev_spec(f"A:{fe}", 0)
            self._tx_frontend = fe
            self.log(f"TX on frontend {fe} (subdev A:{fe})")

    def reconfigure(self, params: EveParams, cfg: RadioConfig, set_time: bool = True) -> RadioStatus:
        """New waveform (rate) and settings on the open radio: the next run without a
        second device open. The serial cannot change here."""
        if self.rx is None or self.tx is None:
            raise RuntimeError("radio is not open")
        if cfg.serial and cfg.serial != self.cfg.serial:
            raise ValueError(f"serial {cfg.serial} differs from the open radio {self.cfg.serial}: close and open")
        cfg.serial = self.cfg.serial
        check_ports(cfg)
        self.p = params
        self.cfg = cfg
        return self.configure(params, cfg, set_time)

    def configure(self, params: EveParams, cfg: RadioConfig, set_time: bool = True) -> RadioStatus:
        """Apply the waveform and the settings to the open blocks and return the status.
        Order: receiver rate, gain, port and LO-offset tune; transmitter frontend, rate,
        gain, port and tune (both directions park the LO lo_offset_hz from the dial, 7.1);
        then the 10 MHz reference and the PPS source for the motherboard (one call covers
        both streams), a 5 s wait for the ref_locked sensor, which the B210 reports only
        for an external or GPSDO reference (it reads False on the internal one), and a
        refusal to run unlocked when require_ref_lock is set and the clock is not internal.
        With set_time the device time is set to UTC on the next PPS edge (set error checked
        to 1 ms) or, host-timed, from time.time() through set_time_now (checked to 50 ms;
        4.3). Finally the key_mask lines become manual GPIO outputs and the TX key is
        released."""
        rate = params.radio_rate
        self.rx.set_samp_rate(rate)
        self.rx.set_gain(cfg.rx_gain_db)
        if self.rx.current_antenna != cfg.rx_antenna:
            self.rx.set_antenna(cfg.rx_antenna)
        self.rx.set_center_freq(cfg.f_dial_hz)
        self.rx_lo_ok = self.rx.set_lo_offset(cfg.lo_offset_hz)
        self.rx_rate = self.rx.get_actual_samp_rate()
        self._set_tx_frontend(cfg.tx_frontend)
        self.tx.set_samp_rate(rate)
        self.tx.set_gain(cfg.tx_gain_db, 0)
        self.tx.set_antenna(cfg.tx_antenna, 0)
        self.tx_lo_ok = R.tune_with_lo_offset(self.tx, cfg.f_dial_hz, cfg.lo_offset_hz, 0, self.log)
        self.tx_rate = float(self.tx.get_samp_rate())
        # reference and time (per motherboard: one call covers both streams)
        self.host_timed = cfg.time_source == "host"
        R.set_reference(self.rx.block, cfg.clock_source, "internal" if self.host_timed else cfg.time_source)
        locked = R.wait_ref_locked(self.rx.block, 5.0)
        if cfg.require_ref_lock and cfg.clock_source != "internal" and not locked:
            raise RuntimeError(f"reference not locked ({cfg.clock_source}); refusing to start (ICD 7.1)")
        if set_time and self.host_timed:
            # No PPS (e.g. a 10 MHz-only GPS reference): the epoch comes from the host's NTP
            # clock through set_time_now. Accuracy = NTP error + a few ms of command latency,
            # well inside the one-frame (0.35 s) tolerance of 4.3; the frequency is still exact.
            t_host = time.time()
            self.rx.block.set_time_now(uhd.time_spec(t_host))
            self.pps_error_s = self.device_time() - time.time()
            if abs(self.pps_error_s) > 0.05:
                raise RuntimeError(f"USRP time did not follow the host clock (error {self.pps_error_s:+.3f} s)")
        elif set_time:
            self.pps_error_s = R.set_time_utc_on_pps(self.rx.block)
            if abs(self.pps_error_s) > 1e-3:
                raise RuntimeError(f"USRP time did not take on the PPS edge (error {self.pps_error_s:+.6f} s)")
        R.gpio_setup_output(self.rx.block, cfg.key_bank, cfg.key_mask)
        self.key(False)
        return self.status()

    def close(self) -> None:
        """Release both USRP streamers. Every reference to the usrp_source / usrp_sink
        must be gone before the same B210 is opened again in this process (see
        Session.release); gc.collect() makes that prompt."""
        import gc
        try:
            self.key(False)
        except Exception:
            pass
        rx, tx = self.rx, self.tx
        self.tx = None
        self.rx = None
        if rx is not None:
            try:
                rx.block = None
            except Exception:
                pass
        del rx, tx
        gc.collect()

    # ---- time ------------------------------------------------------------------------------
    def device_time(self) -> float:
        """The USRP's own clock in seconds (UTC once set on a PPS edge or from the host),
        read through the source block. The schedule and the keyer run on this clock, not
        the host's."""
        return float(self.rx.block.get_time_now().get_real_secs())

    def verify_pps(self, wait_s: float = 2.5) -> bool:
        """Two consecutive PPS timestamps differ by exactly one second (4.3: 'verified
        against a second PPS before the session'). Host-timed radios have no PPS to
        verify; the check is the host-clock agreement instead."""
        if self.host_timed:
            return abs(self.device_time() - time.time()) < 0.05
        t1 = float(self.rx.block.get_time_last_pps().get_real_secs())
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            t2 = float(self.rx.block.get_time_last_pps().get_real_secs())
            if t2 != t1:
                return abs((t2 - t1) - round(t2 - t1)) < 1e-6 and round(t2 - t1) >= 1
            time.sleep(0.02)
        return False

    # ---- keying ------------------------------------------------------------------------------
    def key(self, on: bool) -> None:
        """TX line alone (GPIO_0); the LNA line is left as it is."""
        R.gpio_write(self.rx.block, 1 if on else 0, self.cfg.key_bank, 0x01)
        self._keyed = bool(on)

    def set_lines(self, tx: bool, lna_active: bool) -> None:
        """Both station lines at once: GPIO_0 high = transmit, GPIO_1 high = LNA off (so
        both low = receive, the state the lines rest in when nothing drives them)."""
        value = (1 if tx else 0) | (0 if lna_active else 2)
        R.gpio_write(self.rx.block, value, self.cfg.key_bank, self.cfg.key_mask)
        self._keyed = bool(tx)

    @property
    def keyed(self) -> bool:
        """Last commanded state of the TX line (True = GPIO_0 high = transmit); not a readback."""
        return self._keyed

    def key_readback(self) -> bool:
        """Read the GPIO bank back and report whether the transmitter line (GPIO_0) is high."""
        return bool(R.gpio_read(self.rx.block, self.cfg.key_bank) & 0x01)

    # ---- transmit events ---------------------------------------------------------------------
    def connect_tx_events(self, tb) -> None:
        """Wire the sink's "async_msgs" port to the event counter; the session that owns the
        flowgraph calls this right after connecting the tone source to the sink."""
        if self.tx is None or self.tx_events is None:
            return
        try:
            tb.msg_connect((self.tx, "async_msgs"), (self.tx_events, "in"))
        except Exception as e:      # noqa: BLE001
            self.log(f"transmit event counter not connected: {e}")

    def tx_event_counts(self) -> Dict[str, int]:
        """underflow / late / seq_error / other counts since the radio was created ({} before)."""
        return self.tx_events.snapshot() if self.tx_events is not None else {}

    # ---- status ------------------------------------------------------------------------------
    def status_text(self) -> str:
        """One-line summary for session reports (Session.report.radio); '' if not open."""
        try:
            return self.status().summary()
        except Exception as e:
            return f"status unavailable: {e}"

    def status(self) -> RadioStatus:
        """Read the radio back (lock sensor, clock and time sources, device time, last PPS,
        tuned frequencies, gains, ports) into a RadioStatus. The rates and the LO-offset
        results are the values remembered from configure()."""
        b = self.rx.block
        return RadioStatus(
            serial=self.cfg.serial, ref_locked=R.ref_locked(b),
            clock_source=b.get_clock_source(0), time_source=b.get_time_source(0),
            device_time=self.device_time(), time_last_pps=float(b.get_time_last_pps().get_real_secs()),
            pps_error_s=self.pps_error_s,
            rx_rate=self.rx_rate, tx_rate=self.tx_rate,
            rx_freq=float(b.get_center_freq(0)), tx_freq=float(self.tx.get_center_freq(0)),
            rx_lo_offset_ok=self.rx_lo_ok, tx_lo_offset_ok=self.tx_lo_ok,
            rx_gain=float(b.get_gain(0)), tx_gain=float(self.tx.get_gain(0)),
            rx_antenna=self.rx.current_antenna, tx_antenna=f"{self._tx_frontend} : {self.tx.get_antenna(0)}")

    def rate_error_ppm(self) -> float:
        """Actual radio rate vs the modem's nominal 32 x modem rate, in ppm. The residual is
        absorbed by the frequency tracker (7.1)."""
        return (self.tx_rate - self.p.radio_rate) / self.p.radio_rate * 1e6
