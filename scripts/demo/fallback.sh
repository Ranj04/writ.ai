#!/usr/bin/env bash
#
# Play the most recent backup recording, full screen. One keystroke, no fumbling
# in front of judges.
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'USAGE'
usage: scripts/demo/fallback.sh [FILE]

Opens FILE, or the newest recording in scripts/demo/recordings/, full screen.
USAGE
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
esac

recording="${1:-}"

if [[ -z "$recording" ]]; then
  if [[ ! -d "$RECORDING_DIR" ]]; then
    bad "No recordings directory. Record one with: scripts/demo/up.sh --record"
    exit 1
  fi
  # Newest by modification time, chosen in the helper. `find | xargs ls -t`
  # runs `ls -t` with no operands when the directory is empty, which lists the
  # CWD and hands an unrelated file to the player in front of judges.
  recording="$("$(helper_python)" "$DEMO_API" newest-recording "$RECORDING_DIR" 2>/dev/null || true)"
fi

if [[ -z "$recording" || ! -f "$recording" ]]; then
  bad "No backup recording found in $RECORDING_DIR."
  say "Record the first clean run: scripts/demo/up.sh --record"
  exit 1
fi

banner "FALLBACK — playing the backup recording"
say "$recording"

if [[ "$OSTYPE" == darwin* ]]; then
  if open -a "QuickTime Player" "$recording" 2>/dev/null; then
    # Present mode is QuickTime's full screen. Bounded and detached on purpose:
    # if the file will not open, QuickTime puts up a modal error and AppleScript
    # waits behind it forever. Verified — an unplayable file hung this script.
    # The video is already open by now, so full screen is a nicety, never a
    # reason for the fallback to stop responding in front of judges.
    osascript -e 'with timeout of 5 seconds
      tell application "QuickTime Player"
        activate
        if (count of documents) > 0 then
          present document 1
          play document 1
        end if
      end tell
    end timeout' >/dev/null 2>&1 &
    ok "Playing. If it did not go full screen, press Control-Command-F."
    exit 0
  fi
  open "$recording" && ok "Opened with the default player." && exit 0
  bad "Could not open $recording."
  exit 1
fi

if have xdg-open; then
  xdg-open "$recording" >/dev/null 2>&1 &
  ok "Opened with xdg-open."
  exit 0
fi

bad "No player found. Open this file by hand: $recording"
exit 1
