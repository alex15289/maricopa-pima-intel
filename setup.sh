#!/usr/bin/env bash
#
# setup.sh — ONE-TIME setup for the Pima recorder scraper (macOS).
#
# Run it once in Terminal:   bash setup.sh
# Daily use afterwards:      double-click run.command
#
# It checks the tools this project needs (Python 3.12+, git), creates the
# project's private Python environment (.venv), installs the Python packages,
# and downloads the scraper's browser. Safe to re-run any time — it skips
# what's already done, and if a download is interrupted, re-running resumes.
#
# NOTE FOR MAINTAINERS: this file is macOS-only by design. Windows support
# should be added later as PARALLEL files (setup.ps1 / run.cmd) with all
# OS-specific logic kept inside each file — do not try to share logic here.

set -u

LOG=".setup.log"

say()  { printf '\n%s\n' "$*"; }
ok()   { printf '   [ok] %s\n' "$*"; }

# fail <what went wrong> <how to fix it>
fail() {
  printf '\n   PROBLEM: %s\n' "$1"
  printf '\n   HOW TO FIX: %s\n\n' "$2"
  if [[ -s "$LOG" ]]; then
    printf '   (Technical details were saved to %s — you can send that file\n' "$PWD/$LOG"
    printf '   to your developer if the fix above does not work.)\n\n'
  fi
  exit 1
}

cd "$(dirname "$0")" || { printf 'Could not find the project folder.\n'; exit 1; }
: > "$LOG"

say "Pima Recorder scraper — one-time setup (Mac)"
say "Checking your machine..."

# --- macOS only -------------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || fail \
  "This setup script only works on a Mac." \
  "On other systems, follow the manual setup steps in README.md."

# --- git --------------------------------------------------------------------
if command -v git >/dev/null 2>&1; then
  ok "git is installed ($(git --version 2>/dev/null | head -1))"
else
  fail "git is not installed." \
"Open Terminal, run:  xcode-select --install  — click Install in the window
   that appears (it's Apple's free developer tools), then run this script again."
fi

# --- Python 3.12+ -----------------------------------------------------------
PYBIN="" PYVER=""
for c in python3.14 python3.13 python3.12 python3; do
  command -v "$c" >/dev/null 2>&1 || continue
  v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
  maj=${v%%.*}; min=${v#*.}
  if [[ "$maj" -eq 3 && "$min" -ge 12 ]]; then PYBIN="$c"; PYVER="$v"; break; fi
done
if [[ -z "$PYBIN" ]]; then
  found=$(command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "none")
  fail "Python 3.12 or newer was not found (newest on this Mac: $found)." \
"Go to https://www.python.org/downloads/ , click the big download button,
   open the downloaded file and click through the installer, then run this
   script again."
fi
ok "Python $PYVER is installed ($PYBIN)"

# --- project Python environment (.venv) -------------------------------------
if [[ -x .venv/bin/python ]]; then
  ok "project Python environment already exists (.venv)"
else
  say "Creating the project's Python environment..."
  "$PYBIN" -m venv .venv >>"$LOG" 2>&1 || fail \
    "could not create the Python environment (.venv folder)." \
"Make sure this project folder isn't read-only (right-click it in Finder >
   Get Info), then run this script again."
  ok "created .venv"
fi

# --- Python packages --------------------------------------------------------
say "Installing Python packages (about a minute)..."
.venv/bin/python -m pip install --upgrade pip >>"$LOG" 2>&1  # best-effort
.venv/bin/python -m pip install -r requirements.txt >>"$LOG" 2>&1 || fail \
  "the Python packages did not install." \
"This is almost always an internet hiccup — check your connection and run
   this script again."
ok "packages installed"

# --- scraper browser (Chromium) ---------------------------------------------
say "Downloading the scraper's browser (Chromium, ~150 MB — first time only)..."
.venv/bin/python -m playwright install chromium >>"$LOG" 2>&1 || fail \
  "the browser download did not finish." \
"Check your internet connection and run this script again — the download
   resumes where it stopped."
ok "browser ready"

# --- sanity: can the scraper actually start? --------------------------------
.venv/bin/python - >>"$LOG" 2>&1 <<'PYEOF' || fail \
  "something installed, but the scraper can't start its browser." \
"Run this script once more. If it fails again, send .setup.log to your
   developer."
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    assert pw.chromium.executable_path
PYEOF
ok "verified: the scraper can launch its browser"

printf '\n==============================================\n'
printf ' Setup complete.\n'
printf ' Daily use: double-click  run.command\n'
printf ' (a browser window opens — accept the county\n'
printf '  disclaimer + captcha there, then just wait)\n'
printf '==============================================\n'
