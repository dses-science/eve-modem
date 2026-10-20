"""The application, headless: settings round trip, a software-simulation run through the
RunController (schedule, session, offline decode, report PDF), the report rendered in the
window, and a re-decode. Screenshots of the three tabs land in the archive folder."""
from __future__ import annotations

import os
import sys
import time

os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts" if sys.platform == "win32" else "/usr/share/fonts")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path  # noqa: E402

from PySide6 import QtCore, QtWidgets  # noqa: E402

from eve import app as A  # noqa: E402


def _pump(app, seconds: float) -> None:
    t = time.monotonic()
    while time.monotonic() - t < seconds:
        app.processEvents()
        time.sleep(0.02)


def test_app_sim_run(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # isolate the settings file
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, str(tmp_path / "cfg"))
    win = A.EveApp()
    archive = tmp_path / "archive_app"
    # configure a short simulation
    for b in win.mode_group.buttons():
        b.setChecked(b.property("mode") == "sim")
    win._mode_changed()
    win.w["full_symbol"].setChecked(False)
    win.w["n_frames_test"].setValue(4)
    win.w["pilot_frames"].setValue(2)
    win.w["repeat"].setValue(1)
    win.w["sim_cn0"].setValue(25.0)
    win.w["sim_range_km"].setValue(0.15)
    win.w["archive"].setText(str(archive))
    win.w["lead_s"].setValue(12.0)
    assert "4 frames per symbol" in win.lbl_sym.text()
    win._save()
    assert win.settings.get("n_frames_test") == 4 and win.settings.get("mode") == "sim"

    # preview (no radio)
    win._preview()
    for _ in range(300):
        _pump(app, 0.1)
        if "duration" in win.preview.toPlainText():
            break
    assert "SIM-" in win.preview.toPlainText() and "duration" in win.preview.toPlainText()

    # run
    results = []
    win.ctl.finished.connect(results.append)
    win._start()
    assert win.ctl.busy
    for _ in range(1800):        # up to 3 min
        _pump(app, 0.1)
        if results:
            break
    assert results, "the run did not finish"
    r = results[0]
    assert r.get("error") is None, r
    assert r["ok"], r
    pdf = Path(r["pdf"])
    assert pdf.exists() and pdf.stat().st_size > 10_000
    assert (archive / f"{r['session_id']}_session.json").exists()
    _pump(app, 1.0)
    # the report tab shows pages
    assert win.tabs.currentWidget() is win.report
    assert win.report.pages_lay.count() >= 2
    win.resize(1320, 880)
    win.tabs.setCurrentWidget(win.setup)
    _pump(app, 0.3)
    win.grab().save(str(archive / "screenshot_setup.png"))
    win.tabs.setCurrentWidget(win.run_page)
    _pump(app, 0.3)
    win.grab().save(str(archive / "screenshot_run.png"))
    win.tabs.setCurrentWidget(win.report)
    _pump(app, 0.3)
    win.grab().save(str(archive / "screenshot_report.png"))

    # re-decode the same session
    results.clear()
    win.report._redecode()
    for _ in range(600):
        _pump(app, 0.1)
        if results:
            break
    assert results and results[0]["ok"]
    # a second run reuses the window (no restart needed)
    results.clear()
    win._start()
    for _ in range(1800):
        _pump(app, 0.1)
        if results:
            break
    assert results and results[0]["ok"]
    assert len(list(archive.glob("*_report.pdf"))) == 2
    win.close()


def test_eme_defaults_fit_the_moon_chunk(tmp_path):
    """EME 'Set defaults' must give a configuration the worker accepts. Found at Haswell on
    2026-10-19: the defaults put the design's 40-frame pilot into a 2.4 s Moon chunk of six
    frames (the worker refuses pilot + 2 > chunk), and the rules forced full length back on
    while the guide's test plan starts EME at test length."""
    import math
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, str(tmp_path / "cfg"))
    win = A.EveApp()

    def select(mode):
        for b in win.mode_group.buttons():
            b.setChecked(b.property("mode") == mode)
        win._mode_changed()

    def fits(cfg):          # the worker's check, with the planner's frame count
        p = A.EveParams.named(cfg["variant"])
        n_chunk = int(math.floor(cfg["eme_chunk_s"] * p.r_bw + 1e-9))
        return (not cfg["pilot"]) or cfg["pilot_frames"] + 2 <= n_chunk

    select("eme")
    win._set_defaults()
    cfg = win._values()
    # Variant B on the Moon (D13): the 2.4 s chunk holds three 0.667 s frames, one pilot + two data;
    # TX gain 55 dB on the air
    assert cfg["variant"] == "B" and cfg["pilot"] and cfg["pilot_frames"] == 1 and cfg["full_symbol"] is False
    assert fits(cfg) and cfg["tx_gain"] == 55.0
    win.w["variant"].setCurrentText("A")        # Variant A: six frames per chunk
    # full length stays full on the Moon (the rules no longer flip it) and the pilot still fits
    win.w["full_symbol"].setChecked(True)
    cfg = win._values()
    assert cfg["full_symbol"] is True and cfg["pilot"] and fits(cfg)
    # the design pilot is capped to the chunk, with a note
    win.w["pilot_frames"].blockSignals(True)
    win.w["pilot_frames"].setValue(40)
    win.w["pilot_frames"].blockSignals(False)
    notes = win._coherence()
    cfg = win._values()
    assert cfg["pilot_frames"] == 4 and fits(cfg), notes
    assert any(n.startswith("pilot 4 frames") for n in notes), notes
    # no room at all: the pilot goes off
    win.w["pilot_frames"].blockSignals(True)
    win.w["pilot_frames"].setValue(40)
    win.w["pilot_frames"].blockSignals(False)
    win.w["eme_chunk_s"].setValue(0.9)
    notes = win._coherence()
    cfg = win._values()
    assert cfg["pilot"] is False and any(n.startswith("pilot off") for n in notes), notes
    # Venus: Variant B, the design pilot and full length; the bench its 2-frame pilot and test length
    select("eve")
    win._set_defaults()
    cfg = win._values()
    assert cfg["variant"] == "B" and cfg["full_symbol"] is True and cfg["pilot"] and cfg["pilot_frames"] == 40
    assert cfg["tx_gain"] == 55.0
    # interop is pinned to Variant A (ORI receivers implement A only)
    select("interop")
    win._set_defaults()
    assert win._values()["variant"] == "A"
    win.w["variant"].setCurrentText("B")
    assert win._values()["variant"] == "A"
    select("bench")
    win._set_defaults()
    cfg = win._values()
    assert cfg["full_symbol"] is False and cfg["pilot_frames"] == 2
    # an INI from before 1.0.10 (pilot_frames_test only) keeps its pilot length
    win.settings.q.remove("pilot_frames")
    win.settings.q.setValue("pilot_frames_test", 3)
    win.settings.q.setValue("full_symbol", False)
    win.settings.sync()
    win._load()
    assert win.w["pilot_frames"].value() == 3
    win.close()


if __name__ == "__main__":
    import tempfile
    test_app_sim_run(Path(tempfile.mkdtemp()))
    print("PASS test_app_sim_run")
