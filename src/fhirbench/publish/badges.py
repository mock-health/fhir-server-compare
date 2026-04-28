"""
Generate one shields.io-style SVG badge per (server, profile) cell.

Output:
  fhir-studio/frontend/public/badges/<server>/<profile>.svg

Vendors embed these in their READMEs:
  ![mock.health fhir-r4-base](https://mock.health/badges/hapi/fhir-r4-base.svg)

Self-contained SVG — no external deps. Width is computed from a fixed glyph
estimate (close enough for the limited label set; anti-aliased text won't be
pixel-perfect either way).

Color matches the cell:
  green  ≥ 95%   #4c1   (shields' "brightgreen")
  amber 70–94%   #dfb317 (shields' "yellow")
  red   <  70%   #e05d44 (shields' "red")
  grey  N/A      #9f9f9f
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_STUDIO = REPO_ROOT.parent / "fhir-studio"


COLORS = {
    "green": "#4c1",
    "amber": "#dfb317",
    "red":   "#e05d44",
    "grey":  "#9f9f9f",
    # N/A: a muted slate that reads as "intentionally out of scope" vs. the
    # "not-yet-tested" neutral grey. Matches frontend STATUS_COLOR_CLASSES.
    "na":    "#6c757d",
}

LEFT_LABEL = "mock.health"
LEFT_BG = "#555"
TEXT_COLOR = "#fff"

# Approximate width in px for a 11px Verdana glyph. Good enough for short
# labels — shields.io uses font metrics for exactness; we don't need that.
PX_PER_CHAR = 6.5


def _label_width(text: str) -> int:
    return int(len(text) * PX_PER_CHAR + 12)


def render_svg(left_text: str, right_text: str, color_hex: str) -> str:
    left_w = _label_width(left_text)
    right_w = _label_width(right_text)
    total_w = left_w + right_w
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" role="img" aria-label="{left_text}: {right_text}">
  <title>{left_text}: {right_text}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{left_w}" height="20" fill="{LEFT_BG}"/>
    <rect x="{left_w}" width="{right_w}" height="20" fill="{color_hex}"/>
    <rect width="{total_w}" height="20" fill="url(#s)"/>
  </g>
  <g fill="{TEXT_COLOR}" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text aria-hidden="true" x="{left_w / 2:.1f}" y="15" fill="#010101" fill-opacity=".3">{left_text}</text>
    <text x="{left_w / 2:.1f}" y="14">{left_text}</text>
    <text aria-hidden="true" x="{left_w + right_w / 2:.1f}" y="15" fill="#010101" fill-opacity=".3">{right_text}</text>
    <text x="{left_w + right_w / 2:.1f}" y="14">{right_text}</text>
  </g>
</svg>
'''


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True)
    p.add_argument("--studio-dir", default=str(DEFAULT_STUDIO))
    args = p.parse_args()

    round_path = REPO_ROOT / "results" / "rounds" / args.round / "conformance.json"
    data = json.loads(round_path.read_text())

    studio = pathlib.Path(args.studio_dir).resolve()
    badges_root = studio / "frontend" / "public" / "badges"

    n = 0
    for cell in data["cells"]:
        sid = cell["server_id"]
        pid = cell["profile_id"]
        status = cell["status"]
        pct = cell.get("percentage")
        if status == "na":
            right = "N/A"
        elif pct is None:
            right = "not yet tested"
        else:
            right = f"{pct:.0f}%"
        left = f"{LEFT_LABEL} {pid}"
        svg = render_svg(left, right, COLORS.get(status, COLORS["grey"]))

        out_dir = badges_root / sid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{pid}.svg"
        out_path.write_text(svg)
        n += 1

    print(f"[ok] wrote {n} badges to {badges_root}", file=sys.stderr)


if __name__ == "__main__":
    main()
