#!/usr/bin/env bash
# Build the DSES EVE Modem Design and ICD PDF from its Markdown source (macOS / Linux).
# Mirrors build_docs.ps1: regenerates the figures, then runs the Workbench's
# DSES house-style generator (build_doc.py -> .docx -> PDF via LibreOffice).
#
#   docs/build_docs.sh            # uses the Workbench clone at ~/dev/dses-workbench
#   WB=/path/to/dses-workbench docs/build_docs.sh
#
# Python: the Workbench's project-local conda env (has python-docx, matplotlib,
# astropy). Falls back to python3 on PATH if that env is missing.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WB="${WB:-$HOME/dev/dses-workbench}"
if [[ -x "$WB/.conda/bin/python" ]]; then
    PY="$WB/.conda/bin/python"
else
    PY="$(command -v python3)"
    echo "note: $WB/.conda/bin/python not found; using $PY" >&2
fi
GEN="$WB/build_doc.py"
LOGO="$WB/reports/assets/DSES_Logo_Compact_Teal.png"
[[ -f "$GEN" ]] || { echo "build_doc.py not found at $GEN (set WB=<Workbench clone>)" >&2; exit 1; }

"$PY" "$here/make_figures.py"

"$PY" "$GEN" "$here/DSES_EVE_Modem_Design_and_ICD.md" \
    --pdf   "$here/DSES_EVE_Modem_Design_and_ICD.pdf" \
    --docx  "$here/DSES_EVE_Modem_Design_and_ICD.docx" \
    --title 'Earth-Venus-Earth Modem' \
    --subtitle 'Design Description and Interface Control Document' \
    --version 'Rev C - DRAFT' \
    --header-logo "$LOGO" --force

# Operator's guide (same Markdown as the application's Help menu)
"$PY" "$GEN" "$here/DSES_EVE_Modem_Operators_Guide.md"     --pdf "$here/DSES_EVE_Modem_Operators_Guide.pdf"     --docx "$here/DSES_EVE_Modem_Operators_Guide.docx"     --title 'Earth-Venus-Earth Modem' --subtitle "Operator's Guide"     --version 'Rev C - DRAFT' --header-logo "$LOGO" --force

# Control-station integration test report (Engineering Report tier; test_reports/)
"$PY" "$GEN" "$here/test_reports/EVE_Modem_Station_Integration_Test.md"     --pdf   "$here/test_reports/EVE_Modem_Station_Integration_Test.pdf"     --docx  "$here/test_reports/EVE_Modem_Station_Integration_Test.docx"     --title 'Earth-Venus-Earth Modem'     --subtitle 'Control Station Integration Test Report'     --version 'Rev A - DRAFT'     --header-logo "$LOGO" --force

# Release workflow (does not ship): stamped with the program version, like the
# Workbench's own workflow PDF.
ver="$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$here/../eve/__init__.py")"
"$PY" "$GEN" "$here/Release_Workflow.md" \
    --pdf   "$here/DSES_EVE_Modem_Release_Workflow.pdf" \
    --docx  "$here/DSES_EVE_Modem_Release_Workflow.docx" \
    --title 'Earth-Venus-Earth Modem' \
    --subtitle 'Release Workflow' \
    --version "v$ver" \
    --header-logo "$LOGO" --force

