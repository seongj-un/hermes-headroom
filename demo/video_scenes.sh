#!/usr/bin/env bash
# Scenes for the hackathon demo video. Each runs real commands on this server.
#   asciinema rec -c "bash demo/video_scenes.sh <scene>" out.cast
set -u
# Show plain `hermes` on screen; the demo profile has headroom enabled and reasoning hidden.
hermes() { command hermes -p hrtest "$@"; }
cd "$HOME/projects/hermes-headroom" || exit 1
type_run() {
  printf '\033[1;32m$\033[0m '
  local text="$1" i
  for ((i = 0; i < ${#text}; i++)); do printf '%s' "${text:i:1}"; sleep 0.03; done
  printf '\n'
  sleep 0.5
  eval "$1"
  sleep "${2:-2}"
}

case "$1" in
  status)
    type_run "hermes headroom status" 3
    ;;
  agent)
    type_run "head -12 demo/leaky-report/test/report.test.js" 2
    type_run "hermes chat --oneshot -q 'Run npm test in ~/projects/hermes-headroom/demo/leaky-report. In two short sentences, tell me what happened and whether you are OK. Do not change any files. Answer in English.'" 3
    ;;
  events)
    type_run "hermes headroom events | tail -2" 2
    type_run "free -h | head -2" 3
    ;;
  selftest)
    type_run "hermes headroom selftest" 3
    ;;
esac
