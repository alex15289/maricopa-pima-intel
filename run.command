#!/usr/bin/env bash
#
# run.command — double-click launcher for the daily Pima recorder pull (macOS).
#
# Finder starts .command files in your HOME folder, not the project folder,
# so the first thing we do is cd to wherever this file lives. The window is
# held open at the end so the result never vanishes.
#
# NOTE FOR MAINTAINERS: macOS-only by design; a Windows launcher should be a
# PARALLEL file (run.cmd) later, with its own OS-specific logic.

cd "$(dirname "$0")" || { printf 'Could not find the project folder.\n'; read -r -p "Press Enter to close... "; exit 1; }

hold() { printf '\n'; read -r -p "Press Enter to close this window... "; }

if [[ ! -x .venv/bin/python ]]; then
  printf 'Setup has not been run yet on this Mac (no .venv folder found).\n\n'
  printf 'One-time setup: open Terminal and run:\n\n'
  printf '    bash "%s/setup.sh"\n\n' "$PWD"
  printf 'Then double-click run.command again.\n'
  hold
  exit 1
fi

printf '====================================================\n'
printf '  Pima County Recorder — daily pull (last %s days)\n' "${PIMA_DAYS:-3}"
printf '====================================================\n'
printf 'A Chrome window will open on the county disclaimer page.\n'
printf 'Accept the disclaimer and solve the captcha IN THAT WINDOW.\n'
printf 'Nothing to type here — this window just shows live progress.\n\n'

.venv/bin/python -u scrapers/pima_recorder.py --days "${PIMA_DAYS:-3}"
status=$?

printf '\n'
if [[ $status -eq 0 ]]; then
  printf '[DONE] New records were merged into data/pima_recorder_docs_portal.jsonl\n'
  printf '       The dashboard freshness pill will show "today" after the next\n'
  printf '       leads build.\n'
else
  printf '[STOPPED] The scraper stopped early — the message above says why.\n'
  printf '          Usually you can just double-click run.command again: it\n'
  printf '          resumes where it left off after you re-accept the disclaimer.\n'
fi
hold
exit $status
