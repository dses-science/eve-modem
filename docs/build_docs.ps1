# Build the DSES EVE Modem Design and ICD PDF from its Markdown source.
# Uses the DSES house-style generator from the Workbench repo (build_doc.py:
# Minion/Myriad/Source Code Pro, teal banner headings, cover, TOC, Acrobat
# Distiller PDF with embedded fonts). Run from anywhere:
#     powershell -File docs\build_docs.ps1
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$wb   = 'C:\Users\rick\Documents\DSES\Science\DSES_Workbench'
$py   = Join-Path $wb '.conda\python.exe'
$gen  = Join-Path $wb 'build_doc.py'
$logo = Join-Path $wb 'reports\assets\DSES_Logo_Compact_Teal.png'

# Figures need astropy: use this repo's env (its Library\bin must be on PATH for numpy's MKL).
$eve = Join-Path $here '..\.conda'
$env:PATH = (Join-Path $eve 'Library\bin') + ';' + $env:PATH
& (Join-Path $eve 'python.exe') (Join-Path $here 'make_figures.py')

& $py $gen (Join-Path $here 'DSES_EVE_Modem_Design_and_ICD.md') `
    --pdf   (Join-Path $here 'DSES_EVE_Modem_Design_and_ICD.pdf') `
    --docx  (Join-Path $here 'DSES_EVE_Modem_Design_and_ICD.docx') `
    --title 'Earth-Venus-Earth Modem' `
    --subtitle 'Design Description and Interface Control Document' `
    --version 'Rev C - DRAFT' `
    --header-logo $logo --force

# Operator's guide: the same Markdown feeds the application's Help menu.
& $py $gen (Join-Path $here 'DSES_EVE_Modem_Operators_Guide.md') `
    --pdf   (Join-Path $here 'DSES_EVE_Modem_Operators_Guide.pdf') `
    --docx  (Join-Path $here 'DSES_EVE_Modem_Operators_Guide.docx') `
    --title 'Earth-Venus-Earth Modem' `
    --subtitle "Operator's Guide" `
    --version 'Rev C - DRAFT' `
    --header-logo $logo --force

# Release workflow (does not ship): stamped with the program version, like the
# Workbench's own workflow PDF.
$ver = (Select-String -Path (Join-Path $here '..\eve\__init__.py') `
        -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
& $py $gen (Join-Path $here 'Release_Workflow.md') `
    --pdf   (Join-Path $here 'DSES_EVE_Modem_Release_Workflow.pdf') `
    --docx  (Join-Path $here 'DSES_EVE_Modem_Release_Workflow.docx') `
    --title 'Earth-Venus-Earth Modem' `
    --subtitle 'Release Workflow' `
    --version "v$ver" `
    --header-logo $logo --force

