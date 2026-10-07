# DSES EVE Modem

Earth-Venus-Earth (EVE) modem for the DSES 60-foot dish at Haswell, built on
the ORI "Spiral #2" waveform by Pete Wyckoff, KA3WCA (Open Research Institute,
GPL-3.0). Target: the Venus inferior conjunction of 2026-10-24, preceded by an
Earth-Moon-Earth proof test.

This is a **separate project** from the DSES Radio Astronomy Workbench
(`..\DSES_Workbench`), by decision of 2026-09-09: EVE is a station controller
(real transmit drive, keying interlocks, an ephemeris-driven schedule, 30-minute
frames), while the Workbench is a receive-side observing tool that locks its
transmitter at minimum. The two share code by factoring the Workbench's B210
radio classes into an importable module, not by living in one file.

## Getting it

Releases for Windows, macOS, and Linux, with the Operator's Guide and the Design and
ICD document, are at <https://gpstime.com/sw_distribution/eve-modem/>. The program
updates itself from there (Help → Check for updates). On a Mac or Linux machine that
already has [radioconda](https://github.com/ryanvolz/radioconda), one line installs
and starts it:

```
curl -fsSL https://gpstime.com/sw_distribution/eve-modem/mac_first_run.sh | bash
```

Windows: unzip the release, create the environment with `conda env create --prefix
.conda -f environment.yml`, run `launcher.bat`. Section 10 of the Operator's Guide has
the details. The release zip is self-contained; running from this source tree also
needs the Workbench's `dses_radio.py` beside `eve_app.py` (the release builder bundles it).

This GitHub repository — <https://github.com/dses-science/eve-modem> — is a read-only mirror
of the project's master repository, kept current by the maintainer; issues and pull
requests are welcome but are merged on the master side. The Workbench it borrows
`dses_radio.py` from is mirrored the same way at
<https://github.com/dses-science/dses-workbench>. License: GPL-3.0 (see LICENSE); the
waveform is ORI's, the station software is DSES's.

## Running it

`eve_app.py` is the application: one window for the software simulation, the B210
loopback bench, EME, and Venus sessions, with settings remembered between runs
(`%APPDATA%\DSES\EVE_Modem.ini`), the live operator display, and the session report
PDF shown when a run ends. Create the desktop icon once with
`powershell -File install-shortcut.ps1` (it runs `launcher.ps1`, which starts the app
from this repo's `.conda` env with `Library\bin` on PATH). Every control has a tooltip.
Help → Operator's guide (F1) shows `docs/DSES_EVE_Modem_Operators_Guide.md`; the same
file builds `docs/DSES_EVE_Modem_Operators_Guide.pdf` (build_docs.ps1 builds both documents).
The command-line tools in `tools/` do the same work for scripts.

## Distribution and updates

`make-release.ps1` / `make-release.sh` (both call `tools/make_release.py`) build
`dist/eve-modem-<version>.zip` with its `.sha256` sidecar: one forward-slash zip for
Windows, macOS, and Linux, with the Workbench's `dses_radio.py` bundled. Releases are
published to `https://gpstime.com/sw_distribution/eve-modem/` with a `manifest.json`;
the program checks it at start-up and on Help → Check for updates, and installs
verified updates over itself. Procedure: `docs/Release_Workflow.md`;
`tools/verify_release.py` checks a published release the way the updater will.

## Layout

- `docs/DSES_EVE_Modem_Design_and_ICD.md` — the design description and
  interface control document (source of record). Build the PDF with
  `docs/build_docs.ps1` (uses the Workbench's `build_doc.py`, DSES house style).
- `docs/make_figures.py` — regenerates the document figures (system Python 3.12
  with astropy + matplotlib).
- `eve/` — the modem package (module plan: design document section 5.1).
  Pure NumPy core: `params.py` (Variant A / B / MATLAB sets and derived
  quantities), `bch.py` (BCH(127,106) encoder and decoder, no galois
  dependency), `message.py` (text, CRC-16-CCITT, payload), `modem.py`
  (symbol packing, frame map, phase-continuous streaming synthesizer, FFT-bank
  receiver and accumulators), `channel.py` (Pete's channel.m plus a streaming
  channel with Doppler ramp, gaps, timing offset), `montecarlo.py` (ORI's
  chi-square model, Pete's frame channel, whole-message streaming FER),
  `doppler.py` (JPL Horizons primary, astropy fallback; round trip, two-way
  Doppler, elevation, visibility), `schedule.py` (the section 8.1 JSON
  contract: chunk planning against the round trip and the amplifier duty
  limits, frame-to-time map, validation), `sync.py` (pilot frequency and
  presence, whole-frame epoch check, known-symbol and blind grid search,
  block-accumulating frequency tracker, window receiver, repeat-and-combine
  across passes), `sigmf_io.py` (ORI-format export with the `ori:design` and
  `dses:schedule` blocks; import of ORI's files).
  Radio side: `_workbench.py` (finds the Workbench clone and imports its shared
  `dses_radio.py`), `radio.py` (`EveRadio`: one B210 for both directions,
  reference and PPS, UTC time on a PPS edge, verified LO-offset tuning, rate
  readback, GPIO keying), `gr_blocks.py` (`EveToneSource`, the schedule-driven
  tone source with a `tx_time` tag; `RxDecimator`, the two-stage front end;
  `EveRxSink`, the per-window archive writer with gap padding), `station.py`
  (`Session`: keyer with T_lead/T_lag, PA duty interlocks, abort, one flowgraph
  for the whole session, session log; `SimRadio` for the software bench).
  `display.py` (`OperatorWindow`, PySide6 + pyqtgraph: tone strip, accumulated
  metric of the current symbol, running decisions with margins, chunk and keying
  state, radio status, ephemeris, abort; `--display` on the session and bench tools).
  `gpsdo.py` (Leo Bodnar GPS reference clock over USB HID: status, configuration,
  exact divider planner, setup and lock check; `--gpsdo` on the session and bench
  tools; bench note in `docs/bench/`). `decode.py` (offline decode, the decision of
  record), `report.py` (session report PDF), `siggen.py` (the bench signal generator:
  CW, two-tone or the EVE waveform, keyed at will through the sequencer, live signal, level and timed
  gain sweep, one-page report), `app.py` (the application window).
- `tools/` — `eve_session.py` (plan a session from Horizons, run a schedule on
  the radio, or `sim` it), `eve_decode.py` (offline decode of an archive, the
  decision of record), `eve_bench.py` (B210 loopback: transmit at minimum gain,
  receive the internal leakage, archive, decode), `eve_txcw.py` (one comb tone into
  the lab counter: the transmit-frequency check).
- `tests/` — stage 0-4 tests of the validation plan (design document 5.4):
  Appendix C vectors, galois cross-check, ORI-equation match, loopback on all
  variants, channel calibration, model agreement; conjunction-day geometry
  against the document, schedule contract, sync, Doppler pre-compensation,
  repeat-and-combine, SigMF round trip; the session engine through the software bench.
- `link_budget/` — the ORI link-budget classes and the DSES cases behind the
  document's tables.

## Environment

Project-local conda env, like the Workbench:

    C:\ProgramData\radioconda\Scripts\conda.exe env create --prefix .\.conda -f environment.yml
    conda activate .\.conda          (numpy's BLAS needs the env's Library\bin on PATH)
    python -m pytest tests -q            # 36 tests, ~80 s
    python tools/eve_session.py sim --display     # software bench with the operator display
    python tools/eve_bench.py             # B210 loopback, ~1 min, no antenna needed
    .conda\python.exe -m eve.montecarlo                 # ORI-style link table, Variant A
    .conda\python.exe -m eve.montecarlo --variant B

The core (`eve/`) needs only NumPy; GNU Radio and UHD are used by the two radio
modules only.

## Acknowledgments

The waveform is the Open Research Institute's. Pete Wyckoff, KA3WCA, designed Spiral #2
and sized the link; Michelle Thompson wrote ORI's Python implementation, which this modem
reproduces bit for bit and treats as the air-interface specification, and maintained the
link-budget notebook we run unchanged. ORI published all of it under the GPL and answered
every question. We could not have done this without them. The station hardware this
software drives was built and measured by the DSES EVE team. The design document's
Acknowledgments section says more.

Supported by a grant from Amateur Radio Digital Communications. The DSES EVE-26 project, of which this
modem is part, is funded by a 2026 grant from Amateur Radio Digital Communications (ARDC)
to the Deep Space Exploration Society.

## For reviewers

Start with `docs/DSES_EVE_Modem_Design_and_ICD.pdf`: sections 4 and 6 are the waveform and
timing as implemented, section 5 the software design with the receiver algorithms (5.5),
the process architecture and fault handling (5.6) and the test map (5.7), section 10 the
decision register with the reasoning behind every choice. Every module and public function
in `eve/` carries a docstring that names the design section it implements. The tests in
`tests/` are the executable validation plan.

## References

- ORI EVE repository: https://github.com/OpenResearchInstitute/EVE
  (`signal_design/` MATLAB simulation; `signal_design/Python_Implementation/`
  transmit-side SigMF generator, the interoperability baseline).
- The earlier proposal that this project grew from:
  `..\DSES_Workbench\eve\DESIGN.md`.
