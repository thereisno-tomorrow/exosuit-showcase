#!/usr/bin/env bash
# Diablo's own memory, printed for the 05:30 self-check's monitor (Hermes cron --monitor-script).
# Output unchanged since the last run skips the model entirely; changed output wakes it to move
# anything Nic would be hurt to lose into Brain Home as a suggestion (Nic, 29 Sep 2026).
set -u
home="${HERMES_HOME:-/opt/data}"
for name in MEMORY.md USER.md; do
  printf '== %s\n' "$name"
  cat "$home/memories/$name" 2>/dev/null || printf '(no file)\n'
done
