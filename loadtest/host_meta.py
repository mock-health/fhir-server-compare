"""Capture host + container metadata for benchmark reproducibility.

Written once per run into `results/loadtest/<run-id>/meta.json`. Anyone trying
to reproduce these numbers (or argue they're wrong) needs to see the host
kernel, CPU model, governor, THP setting, swappiness, docker version, and
the exact image digests pulled. Without this, "we got 3000 res/s on HAPI" is
a number with no surface for replication.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
from pathlib import Path


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return (proc.stdout or proc.stderr).strip()
    except Exception as exc:
        return f"<error: {exc}>"


def collect_host_meta() -> dict:
    """Build the metadata dict. All fields are best-effort — missing values
    show as empty strings rather than failing the run."""
    cpu_model = ""
    cores = ""
    threads = ""
    for line in _run(["lscpu"]).splitlines():
        if line.startswith("Model name:"):
            cpu_model = line.split(":", 1)[1].strip()
        elif line.startswith("Core(s) per socket:"):
            cores = line.split(":", 1)[1].strip()
        elif line.startswith("Thread(s) per core:"):
            threads = line.split(":", 1)[1].strip()

    mem_kb = ""
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_kb = line.split()[1]
            break

    governor = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    thp = _read("/sys/kernel/mm/transparent_hugepage/enabled")
    swappiness = _read("/proc/sys/vm/swappiness")

    docker_version = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    docker_compose_version = _run(["docker", "compose", "version", "--short"])

    # lsblk includes loop devices we don't care about — filter to physical disks.
    nvme_model = "\n".join(
        line for line in _run(["lsblk", "-d", "-o", "NAME,MODEL,SIZE"]).splitlines()
        if not line.startswith("loop")
    )

    fs_root = ""
    for line in _run(["findmnt", "-n", "-o", "FSTYPE,TARGET", "/"]).splitlines():
        fs_root = line.strip()
        break

    return {
        "captured_at": time.time(),
        "host": {
            "hostname": platform.node(),
            "kernel": platform.release(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "cpu": {
            "model": cpu_model,
            "physical_cores_per_socket": cores,
            "threads_per_core": threads,
            "governor": governor,
            "logical_cpus": _run(["nproc"]),
        },
        "memory": {
            "total_kb": mem_kb,
            "swappiness": swappiness,
        },
        "kernel_tuning": {
            "transparent_hugepage": thp,
        },
        "storage": {
            "block_devices": nvme_model,
            "root_fs": fs_root,
        },
        "docker": {
            "engine": docker_version,
            "compose": docker_compose_version,
        },
    }


def collect_image_digests() -> dict:
    """Resolve currently-pulled image digests for the FHIR images in play.

    The compose file pins by sha256 already, but capturing what `docker images`
    actually has on disk is a separate (and stronger) claim — proves the test
    ran the bytes the digest references.
    """
    images = [
        "hapiproject/hapi",
        "healthsamurai/aidboxone",
        "healthsamurai/aidboxdb",
        "medplum/medplum-server",
        "medplum/medplum-app",
        "mcr.microsoft.com/healthcareapis/r4-fhir-server",
        "mcr.microsoft.com/mssql/server",
        "postgres",
        "redis",
    ]
    out: dict[str, str] = {}
    for ref in images:
        digest = _run(["docker", "image", "inspect", ref, "--format", "{{(index .RepoDigests 0)}}"])
        if digest and not digest.startswith("<"):
            out[ref] = digest
    return out


def write_meta(path: Path, *, run_id: str, checkpoints: tuple, servers: tuple) -> None:
    """Write the merged meta.json. Idempotent — overwrites if rerun."""
    meta = {
        "run_id": run_id,
        "checkpoints": list(checkpoints),
        "servers": list(servers),
        **collect_host_meta(),
        "image_digests_on_disk": collect_image_digests(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2))
    print(f"  [meta] wrote {path}")


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/meta.json")
    write_meta(out, run_id="adhoc", checkpoints=(), servers=())
    print(out.read_text())
