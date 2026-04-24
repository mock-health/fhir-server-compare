#!/usr/bin/env bash
# setup-host.sh — pre-flight host tuning for the FHIR loadtest.
#
# Sets the CPU governor to `performance` (removes DVFS-driven p99 jitter)
# and surfaces other host-tuning knobs that affect benchmark validity. Idempotent.
#
# Usage:
#   sudo bash scripts/setup-host.sh
#   bash scripts/setup-host.sh --check    # report only, don't change anything
#
# Why this matters: with `powersave` (the default on most Linux distros),
# AMD `amd_pstate` ramps clock dynamically. p99 latency picks up the
# clock-ramp transient on every workload start. Health Samurai will run with
# `performance`. So should we.

set -uo pipefail
# Intentionally NOT `set -e` — this script does numeric comparisons
# whose "false" outcome is informational, not an error.

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

echo "=== Host benchmark readiness check ==="
echo

# CPU governor
GOVERNOR=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo "unknown")
echo "CPU governor: $GOVERNOR"
if [[ "$GOVERNOR" != "performance" ]]; then
  if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "  WARNING: not 'performance' — DVFS will inject p99 jitter."
    echo "  Run without --check (as root) to fix."
  else
    if [[ $EUID -ne 0 ]]; then
      echo "  ERROR: need root to set governor. Re-run with sudo."
      exit 1
    fi
    for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
      echo performance > "$f"
    done
    echo "  -> set to 'performance' on all cores"
  fi
else
  echo "  OK"
fi

# Transparent Huge Pages
THP=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | grep -oE '\[[a-z]+\]' | tr -d '[]' || echo "unknown")
echo "Transparent Huge Pages: $THP"
if [[ "$THP" == "always" ]]; then
  echo "  NOTE: 'always' is fine for HAPI/Aidbox/Medplum. SQL Server prefers 'madvise'."
elif [[ "$THP" == "never" ]]; then
  echo "  NOTE: 'never' is conservative. 'madvise' is the usual benchmark choice."
fi

# Swappiness
SWAPPINESS=$(cat /proc/sys/vm/swappiness 2>/dev/null || echo "unknown")
echo "vm.swappiness: $SWAPPINESS"
if [[ "$SWAPPINESS" =~ ^[0-9]+$ && "$SWAPPINESS" -gt 10 ]]; then
  echo "  NOTE: high swappiness can move DB hot pages to swap under memory pressure."
  echo "        Consider 'sysctl vm.swappiness=10' for benchmarks."
fi

# Docker daemon log driver — JSON logs at high event rate slow containers down
LOG_DRIVER=$(docker info --format '{{.LoggingDriver}}' 2>/dev/null || echo "unknown")
echo "Docker log driver: $LOG_DRIVER"
if [[ "$LOG_DRIVER" == "json-file" ]]; then
  echo "  NOTE: json-file is fine for our log volume but if you see disk pressure"
  echo "        during ingest, consider 'local' driver in /etc/docker/daemon.json."
fi

# Docker storage driver
STORAGE=$(docker info --format '{{.Driver}}' 2>/dev/null || echo "unknown")
echo "Docker storage driver: $STORAGE"

# Free disk on the docker root
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "/var/lib/docker")
FREE=$(df -BG --output=avail "$DOCKER_ROOT" 2>/dev/null | tail -1 | tr -d 'G ')
echo "Free space on $DOCKER_ROOT: ${FREE}GiB"
if [[ "$FREE" =~ ^[0-9]+$ && "$FREE" -lt 200 ]]; then
  echo "  WARNING: <200 GiB free. 100K-patient run needs ~150 GiB for DBs alone."
fi

# Container limit on file descriptors — bursts of conn-pool churn can hit 1024 cap
ULIMIT_N=$(ulimit -n 2>/dev/null || echo "unknown")
echo "ulimit -n: $ULIMIT_N"

echo
echo "=== Done ==="
if [[ $CHECK_ONLY -eq 0 ]]; then
  echo "Re-run with --check to confirm."
fi
exit 0
