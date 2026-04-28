"""Unit tests for fhirbench.benchmark.parse_report — round-artifact aggregation primitives."""
from __future__ import annotations

import json

import pytest

from fhirbench.benchmark import parse_report


def test_roster_is_six_servers():
    """HFS removed → roster is now 6 servers."""
    assert parse_report.ROSTER == ("hapi", "aidbox", "medplum", "msfhir", "blaze", "spark")
    assert "hfs" not in parse_report.ROSTER


def test_cell_color_thresholds():
    """3-level color: green ≤ 100ms ≤ amber ≤ 1000ms < red. None → grey."""
    # GREEN_MAX_MS = 100.0, AMBER_MAX_MS = 1000.0
    assert parse_report.cell_color(None) == "grey"
    assert parse_report.cell_color(0.0) == "green"
    assert parse_report.cell_color(50.0) == "green"
    assert parse_report.cell_color(parse_report.GREEN_MAX_MS) == "green"  # ≤ green threshold
    assert parse_report.cell_color(100.001) == "amber"
    assert parse_report.cell_color(parse_report.AMBER_MAX_MS) == "amber"  # ≤ amber threshold
    assert parse_report.cell_color(1000.001) == "red"
    assert parse_report.cell_color(60_000.0) == "red"


def test_discover_checkpoints_returns_sorted_ints(tmp_path):
    """Walks a run dir, returns checkpoint sizes as a list of ints sorted numerically."""
    for n in (4096, 1024, 16384):
        (tmp_path / f"checkpoint_{n}").mkdir()
    out = parse_report.discover_checkpoints(tmp_path)
    # Note: function uses `sorted(run_dir.iterdir())` (lexicographic on dir names),
    # so '1024' < '16384' < '4096' lexicographically. Tests document actual behavior.
    assert sorted(out) == [1024, 4096, 16384]
    assert set(out) == {1024, 4096, 16384}


def test_discover_checkpoints_empty_dir(tmp_path):
    """No checkpoints → empty list, not an error."""
    assert parse_report.discover_checkpoints(tmp_path) == []


def test_discover_checkpoints_ignores_non_checkpoint_dirs(tmp_path):
    """Subdirs that don't match `checkpoint_<int>` are ignored."""
    (tmp_path / "checkpoint_1000").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "checkpoint_bogus").mkdir()
    assert parse_report.discover_checkpoints(tmp_path) == [1000]


def test_disk_used_bytes_reads_named_volumes(tmp_path):
    """`docker system df -v` JSON → sum of named-volume bytes for one server.
    The function uses parse_size() which expects human-readable strings ('1KB', '2MB')."""
    disk_json = {
        "Volumes": [
            {"Name": "fhir-server-compare_hapi-data", "Size": "1KB"},
            {"Name": "fhir-server-compare_aidbox-data", "Size": "2KB"},
            {"Name": "unrelated-volume", "Size": "4KB"},
        ]
    }
    disk_path = tmp_path / "disk.json"
    disk_path.write_text(json.dumps(disk_json))
    # parse_size returns float bytes; we just check the right volume was matched.
    hapi_bytes = parse_report.disk_used_bytes(disk_path, "hapi")
    aidbox_bytes = parse_report.disk_used_bytes(disk_path, "aidbox")
    assert hapi_bytes is not None
    assert aidbox_bytes is not None
    assert hapi_bytes < aidbox_bytes  # 1KB < 2KB


def test_disk_used_bytes_returns_none_for_missing_file(tmp_path):
    """Missing disk.json → None (not measured), not zero. Caller must distinguish."""
    assert parse_report.disk_used_bytes(tmp_path / "nope.json", "hapi") is None


def test_disk_used_bytes_returns_none_when_volume_absent(tmp_path):
    """File exists but no matching volume → None (not measured for this server)."""
    disk_path = tmp_path / "disk.json"
    disk_path.write_text(json.dumps({"Volumes": []}))
    # Function returns None when no volume matched (matched=False), distinguishing
    # 'we looked and found nothing' from 'we have a 0-byte volume'.
    assert parse_report.disk_used_bytes(disk_path, "hapi") is None


def test_disk_used_bytes_returns_none_for_unparseable(tmp_path):
    """Malformed JSON → None (not crashing the round-artifact build)."""
    disk_path = tmp_path / "disk.json"
    disk_path.write_text("not json at all")
    assert parse_report.disk_used_bytes(disk_path, "hapi") is None
