"""Print a quick text summary of a benchmark round to stdout — for
`make benchmark-summary`. One table per profile showing p50 (median) per
server per checkpoint. Missing checkpoints render as '-'."""

from __future__ import annotations

import argparse
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _print_profile(d: dict, profile_id: str) -> None:
    # Collect the checkpoint ladder across all cells in this profile.
    checkpoints: set[int] = set()
    for c in d["cells"]:
        if c["profile_id"] != profile_id:
            continue
        for row in c.get("evidence", []):
            ck = row.get("checkpoint")
            if isinstance(ck, int):
                checkpoints.add(ck)
    ckpt_list = sorted(checkpoints)

    label = profile_id.upper()
    print(f"Profile: {profile_id}  ({label} p50 ms (median) by checkpoint, '-' = no data)")
    print()

    headers = ["server", "status"] + [_fmt_ckpt(c) for c in ckpt_list]
    widths = [10, 6] + [9] * len(ckpt_list)
    print("  " + " ".join(h.rjust(w) if i > 1 else h.ljust(w)
                          for i, (h, w) in enumerate(zip(headers, widths))))
    print("  " + " ".join("-" * w for w in widths))

    for c in d["cells"]:
        if c["profile_id"] != profile_id:
            continue
        by_ck = {row["checkpoint"]: row for row in c.get("evidence", [])}
        cells = []
        for ck in ckpt_list:
            row = by_ck.get(ck)
            cells.append("-" if row is None else _fmt_ms(row.get("p50_ms")))
        sid = c["server_id"]
        st = c["status"]
        line = [sid.ljust(widths[0]), st.ljust(widths[1])] + [
            cells[i].rjust(widths[2 + i]) for i in range(len(ckpt_list))
        ]
        print("  " + " ".join(line))


def _fmt_ckpt(n: int) -> str:
    if n >= 1000 and n % 1000 == 0:
        return f"{n // 1000}K"
    return str(n)


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 10_000:
        return f"{v / 1000:.1f}s"
    if v >= 1000:
        return f"{v / 1000:.2f}s"
    if v >= 10:
        return f"{v:.0f}ms"
    return f"{v:.1f}ms"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True)
    p.add_argument("--profile", default=None)
    args = p.parse_args()

    round_path = REPO_ROOT / "results" / "rounds" / args.round / "benchmark.json"
    d = json.loads(round_path.read_text())

    print(f"Round {d['round_id']} — generated {d['generated_at']}")
    print()

    profiles = [args.profile] if args.profile else [p["id"] for p in d["profiles"]]
    for i, pid in enumerate(profiles):
        if i > 0:
            print()
        _print_profile(d, pid)


if __name__ == "__main__":
    main()
