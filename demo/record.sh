#!/usr/bin/env bash
# Recording helper: "types" each command, then runs it. Used with
#   asciinema rec -c "bash demo/record.sh <scene>" out.cast
set -u
cd "$(dirname "$0")/.." || exit 1
export PS1='$ '
type_run() {
  printf '\033[1;32m$\033[0m '
  local text="$1" i
  for ((i = 0; i < ${#text}; i++)); do printf '%s' "${text:i:1}"; sleep 0.025; done
  printf '\n'
  sleep 0.4
  eval "$1"
  sleep "${2:-1.5}"
}

case "${1:-status}" in
  status)
    type_run "hermes -p hrtest headroom status" 2.5
    type_run "hermes -p hrtest headroom selftest" 3
    ;;
  explain)
    type_run "hermes -p hrtest headroom explain 'cd web && npm ci && npx jest'" 2.5
    type_run "hermes -p hrtest headroom explain 'pytest -n auto tests/'" 2
    type_run "hermes -p hrtest headroom explain 'make -j && cargo build --release'" 2
    type_run "hermes -p hrtest headroom explain 'git commit -m \"npm install docs\"'" 2.5
    ;;
  agent)
    type_run "cat demo/leaky-report/test/report.test.js | head -8" 2
    type_run "hermes -p hrtest chat -q 'Run npm test in demo/leaky-report and tell me in two sentences what happened. Do not change any files.'" 2
    type_run "hermes -p hrtest headroom events | tail -4" 3
    ;;
esac
