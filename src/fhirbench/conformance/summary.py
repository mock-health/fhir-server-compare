"""Print a quick text summary of a round to stdout — for `make conformance-summary`."""

from __future__ import annotations

import argparse
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _print_profile(d: dict, profile_id: str) -> None:
    print(f"Profile: {profile_id}")
    print()
    print(f"  {'server':<10} {'status':<6} {'%':>7}   {'passed/total':<14} (MUST · SHOULD · MAY)")
    print(f"  {'-' * 10} {'-' * 6} {'-' * 7}   {'-' * 14}  ---------------------")
    for c in d["cells"]:
        if c["profile_id"] != profile_id:
            continue
        pct = c["percentage"]
        pct_s = "-" if pct is None else f"{pct:5.1f}%"
        ps = sum(c["passed"].values())
        ts = sum(c["total"].values())
        b = c["passed"], c["total"]
        bucket = (
            f"{b[0]['MUST']}/{b[1]['MUST']}     "
            f"{b[0]['SHOULD']}/{b[1]['SHOULD']}     "
            f"{b[0]['MAY']}/{b[1]['MAY']}"
        )
        print(f"  {c['server_id']:<10} {c['status']:<6} {pct_s:>7}   {f'{ps}/{ts}':<14}  {bucket}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True)
    p.add_argument("--profile", default=None,
                   help="profile id to summarize (default: all profiles in the round)")
    args = p.parse_args()

    round_path = REPO_ROOT / "results" / "rounds" / args.round / "conformance.json"
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
