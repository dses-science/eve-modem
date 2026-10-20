"""Ephemeris: round-trip time, two-way Doppler, elevation and visibility for target
`venus` or `moon` from a site (design document 4.3, 4.4, 5.1, 6.5).

Two sources produce the same EphemerisTable (one row per step, UTC):
  * JPL Horizons (primary): fetch_horizons() calls the Horizons API (observer table,
    topocentric range and range rate, azimuth and elevation) and the table is saved as
    CSV next to the schedule; load_table() reads it back offline.
  * astropy (fallback): compute_astropy() uses astropy's solar-system ephemeris
    ('builtin' analytic, or 'de440s' via jplephem when the file is available), the same
    method as docs/make_figures.py.

DopplerModel wraps a table: rtt_s(t), doppler_hz(t, f_rf), rate_hz_s(t, f_rf),
elevation_deg(t), and window(min_el). Monostatic: the echo of a frame transmitted at
t_tx arrives at t_tx + rtt(t_tx); the two-way Doppler is -2 f (dr/dt) / c evaluated
at the mid-time of the round trip. Bistatic: uplink from the transmit site plus downlink
to the receive site, each with its own table.

Times are UTC. Accept datetime, ISO-8601 strings ('2026-10-24T15:30:00Z'), or float
Unix seconds; everything internal is float Unix seconds.
"""
from __future__ import annotations

import csv
import io
import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

import numpy as np

C_KM_S = 299792.458
AU_KM = 149597870.7
HORIZONS_ID = {"venus": "299", "moon": "301", "mars": "499", "sun": "10"}
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"

TimeLike = Union[float, int, str, datetime]


@dataclass(frozen=True)
class Site:
    """An observing site: geodetic latitude and longitude in degrees (east positive) and
    height in meters. Serialized as the schedule file's transmitter / receiver entries
    (design document 8.1)."""
    name: str
    lat_deg: float
    lon_deg: float           # east positive
    alt_m: float

    def to_dict(self):
        """The schedule-file form: site, lat, lon, alt_m."""
        return {"site": self.name, "lat": self.lat_deg, "lon": self.lon_deg, "alt_m": self.alt_m}

    @classmethod
    def from_dict(cls, d) -> "Site":
        """From the schedule-file form ('name' accepted for 'site'; alt_m defaults to 0)."""
        return cls(d.get("site", d.get("name", "")), float(d["lat"]), float(d["lon"]), float(d.get("alt_m", 0.0)))


DSES_HASWELL = Site("DSES Haswell", 38.380833, -103.156111, 1311.0)


# ---- time helpers ---------------------------------------------------------------------
def to_unix(t: TimeLike) -> float:
    """Any accepted time (float or int Unix seconds, datetime, ISO-8601 text, astropy
    Time) to float Unix seconds; naive datetimes and text without a zone are taken as UTC."""
    if isinstance(t, (float, int, np.floating, np.integer)):
        return float(t)
    if isinstance(t, datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp()
    if isinstance(t, str):
        s = t.strip().replace("Z", "+00:00")
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if hasattr(t, "unix"):          # astropy Time
        return float(t.unix)
    raise TypeError(f"cannot interpret time {t!r}")


def to_datetime(t: TimeLike) -> datetime:
    """Any accepted time to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(to_unix(t), tz=timezone.utc)


def iso_utc(t: TimeLike, digits: int = 3) -> str:
    """ISO-8601 UTC text 'YYYY-MM-DDTHH:MM:SS[.fff]Z' with `digits` fractional digits
    (0 = whole seconds); the form used throughout the schedule, sidecars and logs."""
    # Round to the requested digit BEFORE formatting, carrying into the seconds field: a
    # fraction that rounds up to the next whole second (e.g. 53.9996 s at 3 digits) must read
    # 54.000, not 53.000. The old form took the rounded fraction modulo 10^digits and left
    # the seconds alone, which dropped a whole second in about one timestamp per thousand;
    # a full-length EME schedule (1,735 chunks, four times each) hit it at chunk 866 on
    # 2026-10-20 and the GUI refused its own schedule (tx window 3.090 s for 6 frames).
    unit_us = 10 ** (6 - digits) if digits else 10 ** 6
    dt = to_datetime(t)
    rounded_us = int(round(dt.microsecond / unit_us)) * unit_us
    dt = dt.replace(microsecond=0) + timedelta(microseconds=rounded_us)
    s = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if digits:
        s += f".{dt.microsecond // unit_us:0{digits}d}"
    return s + "Z"


# ---- the table ----------------------------------------------------------------------------
@dataclass
class EphemerisTable:
    """A uniformly stepped topocentric ephemeris of one target from one site: Unix
    seconds, range in km, range rate in km/s (positive = receding), azimuth and elevation
    in degrees, and the source it came from. The *_at() accessors interpolate linearly and
    refuse times more than one step outside the table."""
    target: str
    site: Site
    t_unix: np.ndarray            # seconds, increasing, uniform step
    range_km: np.ndarray          # topocentric distance
    range_rate_km_s: np.ndarray   # d(range)/dt, positive = receding
    az_deg: np.ndarray
    el_deg: np.ndarray
    source: str = ""

    @property
    def step_s(self) -> float:
        """Row spacing in seconds (0 for a single row)."""
        return float(self.t_unix[1] - self.t_unix[0]) if self.t_unix.size > 1 else 0.0

    @property
    def t_start(self) -> float:
        """First row time, Unix seconds."""
        return float(self.t_unix[0])

    @property
    def t_stop(self) -> float:
        """Last row time, Unix seconds."""
        return float(self.t_unix[-1])

    def _interp(self, col: np.ndarray, t: np.ndarray) -> np.ndarray:
        if np.any(t < self.t_start - self.step_s) or np.any(t > self.t_stop + self.step_s):
            raise ValueError("time outside the ephemeris table")
        return np.interp(t, self.t_unix, col)

    def range_at(self, t) -> np.ndarray:
        """Range in km at Unix times t (scalar or array), interpolated; always an array."""
        return self._interp(self.range_km, np.atleast_1d(np.asarray(t, dtype=float)))

    def rate_at(self, t) -> np.ndarray:
        """Range rate in km/s at Unix times t, interpolated; always an array."""
        return self._interp(self.range_rate_km_s, np.atleast_1d(np.asarray(t, dtype=float)))

    def el_at(self, t) -> np.ndarray:
        """Elevation in degrees at Unix times t, interpolated; always an array."""
        return self._interp(self.el_deg, np.atleast_1d(np.asarray(t, dtype=float)))

    def az_at(self, t) -> np.ndarray:
        """Azimuth in degrees at Unix times t, interpolated; always an array."""
        return self._interp(self.az_deg, np.atleast_1d(np.asarray(t, dtype=float)))

    # ---- CSV --------------------------------------------------------------------------
    HEADER = ["utc", "range_km", "range_rate_km_s", "az_deg", "el_deg"]

    def save(self, path: Union[str, Path]) -> Path:
        """Write the table as CSV: a '# {json}' header line carrying target, site and
        source, then utc, range_km, range_rate_km_s, az_deg, el_deg rows. This is the file
        the schedule's doppler.table entry names (8.1). Returns the path."""
        path = Path(path)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("# " + json.dumps({"target": self.target, "site": self.site.name,
                                    "lat": self.site.lat_deg, "lon": self.site.lon_deg,
                                    "alt_m": self.site.alt_m, "source": self.source}) + chr(10))
            w = csv.writer(f)
            w.writerow(self.HEADER)
            for i in range(self.t_unix.size):
                w.writerow([iso_utc(self.t_unix[i], 0), f"{self.range_km[i]:.6f}",
                            f"{self.range_rate_km_s[i]:.9f}", f"{self.az_deg[i]:.6f}", f"{self.el_deg[i]:.6f}"])
        return path

    @classmethod
    def load(cls, path: Union[str, Path]) -> "EphemerisTable":
        """Read a table written by save(); a CSV without the '#' header line loads with an
        empty site and source 'csv'."""
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            first = f.readline()
            meta = {}
            if first.startswith("#"):
                meta = json.loads(first[1:].strip())
                body = f.read()
            else:
                body = first + f.read()
        rows = list(csv.DictReader(io.StringIO(body)))
        site = Site(meta.get("site", ""), float(meta.get("lat", 0)), float(meta.get("lon", 0)), float(meta.get("alt_m", 0)))
        return cls(meta.get("target", ""), site,
                   np.array([to_unix(r["utc"]) for r in rows]),
                   np.array([float(r["range_km"]) for r in rows]),
                   np.array([float(r["range_rate_km_s"]) for r in rows]),
                   np.array([float(r["az_deg"]) for r in rows]),
                   np.array([float(r["el_deg"]) for r in rows]),
                   source=meta.get("source", "csv"))


# ---- JPL Horizons -----------------------------------------------------------------------
def fetch_horizons(target: str, site: Site, t_start: TimeLike, t_stop: TimeLike,
                   step: str = "1 m", timeout: float = 60.0) -> EphemerisTable:
    """Observer ephemeris from the Horizons API: airless apparent az/el, topocentric range
    (delta, AU) and range rate (deldot, km/s). Needs the network."""
    tid = HORIZONS_ID.get(target.lower(), target)
    a, b = to_datetime(t_start), to_datetime(t_stop)
    params = {
        "format": "text", "COMMAND": f"'{tid}'", "OBJ_DATA": "'NO'", "MAKE_EPHEM": "'YES'",
        "EPHEM_TYPE": "'OBSERVER'", "CENTER": "'coord@399'", "COORD_TYPE": "'GEODETIC'",
        "SITE_COORD": f"'{site.lon_deg:.6f},{site.lat_deg:.6f},{site.alt_m / 1000:.4f}'",
        "START_TIME": f"'{a.strftime('%Y-%m-%d %H:%M:%S')}'", "STOP_TIME": f"'{b.strftime('%Y-%m-%d %H:%M:%S')}'",
        "STEP_SIZE": f"'{step}'", "QUANTITIES": "'4,20'", "CSV_FORMAT": "'YES'",
        "TIME_DIGITS": "'SECONDS'", "ANG_FORMAT": "'DEG'", "APPARENT": "'AIRLESS'",
        "RANGE_UNITS": "'KM'",
    }
    url = HORIZONS_URL + "?" + urllib.parse.urlencode(params)
    txt = urllib.request.urlopen(url, timeout=timeout).read().decode("utf-8", "replace")
    return parse_horizons(txt, target, site)


def parse_horizons(txt: str, target: str, site: Site) -> EphemerisTable:
    """Parse a Horizons text response: the CSV rows between $$SOE and $$EOE (date,
    presence flag, blank, azimuth, elevation, delta, deldot) into a table. delta is taken
    as km, or as AU when the number is small enough to be one (the server ignored
    RANGE_UNITS); deldot is km/s. ValueError when the block or its rows are missing."""
    i, j = txt.find("$$SOE"), txt.find("$$EOE")
    if i < 0 or j < 0:
        raise ValueError("Horizons response has no $$SOE/$$EOE block:\n" + txt[:2000])
    t, rng, rate, az, el = [], [], [], [], []
    for line in txt[i + 5:j].strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 7:
            continue
        # Date, solar/lunar presence flag, (blank), Azi, Elev, delta, deldot
        dt = None
        for fmt in ("%Y-%b-%d %H:%M:%S.%f", "%Y-%b-%d %H:%M:%S", "%Y-%b-%d %H:%M"):
            try:
                dt = datetime.strptime(cols[0], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if dt is None:
            continue
        t.append(dt.timestamp())
        az.append(float(cols[3])); el.append(float(cols[4]))
        d = float(cols[5])
        rng.append(d if d > 1e5 else d * AU_KM)     # KM requested; AU if the server ignored it
        rate.append(float(cols[6]))
    if not t:
        raise ValueError("Horizons response had no data rows")
    return EphemerisTable(target.lower(), site, np.array(t), np.array(rng), np.array(rate),
                          np.array(az), np.array(el), source="horizons")


# ---- astropy fallback -------------------------------------------------------------------
def compute_astropy(target: str, site: Site, t_start: TimeLike, t_stop: TimeLike,
                    step_s: float = 60.0, ephemeris: str = "builtin") -> EphemerisTable:
    """Topocentric range, range rate (central difference), az/el with astropy. The
    interpreter must have the env's Library/bin on PATH (conda activate) or numpy's BLAS
    delay-load fails silently on Windows."""
    from astropy.time import Time
    from astropy.coordinates import get_body, EarthLocation, AltAz, solar_system_ephemeris
    import astropy.units as u
    loc = EarthLocation(lat=site.lat_deg * u.deg, lon=site.lon_deg * u.deg, height=site.alt_m * u.m)
    t0, t1 = to_unix(t_start), to_unix(t_stop)
    tt = np.arange(t0, t1 + 0.5 * step_s, step_s)
    times = Time(tt, format="unix", scale="utc")
    h = 1.0 * u.s
    with solar_system_ephemeris.set(ephemeris):
        body = get_body(target.lower(), times, loc)
        rng = body.distance.to(u.km).value
        r_plus = get_body(target.lower(), times + h, loc).distance.to(u.km).value
        r_minus = get_body(target.lower(), times - h, loc).distance.to(u.km).value
        aa = body.transform_to(AltAz(obstime=times, location=loc))
    rate = (r_plus - r_minus) / 2.0
    return EphemerisTable(target.lower(), site, tt, rng, rate, aa.az.deg, aa.alt.deg,
                          source=f"astropy:{ephemeris}")


# ---- synthetic table (bench and simulation) -------------------------------------------------
def synthetic_table(target: str, site: Site, t_start: TimeLike, t_stop: TimeLike, range_km: float,
                    range_rate_km_s: float = 0.0, el_deg: float = 45.0, step_s: float = 1.0) -> EphemerisTable:
    """A constant-range (optionally constant range-rate) table for the software bench and
    the B210 loopback: e.g. range 375,000 km gives the Moon's 2.5 s round trip."""
    t0, t1 = to_unix(t_start), to_unix(t_stop)
    tt = np.arange(t0, t1 + 0.5 * step_s, step_s)
    rng = range_km + range_rate_km_s * (tt - t0)
    return EphemerisTable(target.lower(), site, tt, rng, np.full(tt.size, range_rate_km_s),
                          np.full(tt.size, 180.0), np.full(tt.size, el_deg), source="synthetic")


# ---- the model --------------------------------------------------------------------------
class DopplerModel:
    """Round trip and Doppler from one table (monostatic) or two (bistatic: tx table for
    the uplink, rx table for the downlink)."""

    def __init__(self, tx_table: EphemerisTable, rx_table: Optional[EphemerisTable] = None):
        self.tx = tx_table
        self.rx = rx_table or tx_table

    @property
    def monostatic(self) -> bool:
        """True when one table serves both legs (the receiver is the transmitter's site)."""
        return self.rx is self.tx

    def rtt_s(self, t_tx: TimeLike) -> float:
        """Round-trip time for a signal leaving the transmitter at t_tx (light-time
        iterated: uplink range at the reflection time, downlink range at arrival)."""
        t = to_unix(t_tx)
        tau_up = float(self.tx.range_at(t)[0]) / C_KM_S
        for _ in range(3):
            t_refl = t + tau_up
            tau_up = float(self.tx.range_at(t_refl)[0]) / C_KM_S       # range at reflection
            tau_dn = float(self.rx.range_at(t_refl + tau_up)[0]) / C_KM_S
        return tau_up + tau_dn

    def doppler_hz(self, t_tx: TimeLike, f_rf_hz: float) -> float:
        """Two-way Doppler shift (Hz) of a signal transmitted at t_tx, as seen by the
        receiver: -f (rate_up + rate_down) / c, rates at the reflection time."""
        t = to_unix(t_tx)
        rtt = self.rtt_s(t)
        t_refl = t + rtt / 2.0
        r_up = float(self.tx.rate_at(t_refl)[0])
        r_dn = float(self.rx.rate_at(t_refl)[0])
        return -f_rf_hz * (r_up + r_dn) / C_KM_S

    def doppler_series(self, t_tx: np.ndarray, f_rf_hz: float) -> np.ndarray:
        """doppler_hz() for each transmit time in t_tx (Unix seconds), as an array of Hz."""
        return np.array([self.doppler_hz(t, f_rf_hz) for t in np.atleast_1d(t_tx)])

    def rate_hz_s(self, t_tx: TimeLike, f_rf_hz: float, dt: float = 30.0) -> float:
        """Doppler rate in Hz/s at transmit time t_tx, by central difference of doppler_hz
        over +/- dt seconds (design document 4.4: peaks near -0.48 Hz/s at 2304 MHz)."""
        t = to_unix(t_tx)
        return (self.doppler_hz(t + dt, f_rf_hz) - self.doppler_hz(t - dt, f_rf_hz)) / (2 * dt)

    def elevation_deg(self, t: TimeLike, which: str = "tx") -> float:
        """Target elevation in degrees at time t from the 'tx' (default) or 'rx' site table."""
        tab = self.tx if which == "tx" else self.rx
        return float(tab.el_at(to_unix(t))[0])

    def azimuth_deg(self, t: TimeLike, which: str = "tx") -> float:
        """Target azimuth in degrees at time t from the 'tx' (default) or 'rx' site table."""
        tab = self.tx if which == "tx" else self.rx
        return float(tab.az_at(to_unix(t))[0])

    def window(self, min_el_deg: float = 20.0) -> List[Tuple[float, float]]:
        """Intervals (t_start, t_stop) in the table where BOTH sites see the target above
        min_el_deg."""
        t = self.tx.t_unix
        up = self.tx.el_deg > min_el_deg
        if not self.monostatic:
            up &= self.rx.el_at(t) > min_el_deg
        out = []
        start = None
        for i, u in enumerate(up):
            if u and start is None:
                start = t[i]
            if (not u or i == len(up) - 1) and start is not None:
                out.append((start, t[i] if u else t[i - 1]))
                start = None
        return out

    def summary(self, t: TimeLike, f_rf_hz: float) -> str:
        """One line: target, time, round trip, Doppler and its rate at f_rf_hz, and the
        transmit site's elevation."""
        return (f"{self.tx.target} at {iso_utc(t, 0)}: rtt {self.rtt_s(t):.2f} s, "
                f"doppler {self.doppler_hz(t, f_rf_hz):+.1f} Hz, rate {self.rate_hz_s(t, f_rf_hz):+.3f} Hz/s, "
                f"el {self.elevation_deg(t):.1f} deg")


def make_model(target: str, site: Site, t_start: TimeLike, t_stop: TimeLike,
               source: str = "auto", table_path: Optional[Union[str, Path]] = None,
               step: str = "1 m", rx_site: Optional[Site] = None) -> DopplerModel:
    """Build a model: 'horizons' (fetch, and save to table_path if given), 'astropy',
    'csv' (load table_path), or 'auto' (csv if the file exists, else horizons, else
    astropy)."""
    def one(site_: Site) -> EphemerisTable:
        if source in ("csv",) or (source == "auto" and table_path and Path(table_path).exists()):
            return EphemerisTable.load(table_path)
        if source in ("horizons", "auto"):
            try:
                tab = fetch_horizons(target, site_, t_start, t_stop, step)
                if table_path and site_ is site:
                    tab.save(table_path)
                return tab
            except Exception:
                if source == "horizons":
                    raise
        step_s = _step_seconds(step)
        return compute_astropy(target, site_, t_start, t_stop, step_s)

    tx = one(site)
    rx = one(rx_site) if rx_site is not None and rx_site != site else None
    return DopplerModel(tx, rx)


def _step_seconds(step: str) -> float:
    n, unit = step.split()
    return float(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit[0].lower()]
