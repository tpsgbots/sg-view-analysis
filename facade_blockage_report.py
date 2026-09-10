"""
For the subject building, compute % of view blocked per side (N/E/S/W,
treating the building as sitting in a simple 4-sided box, not its real
facade angles) at EVERY floor from 1 up to its known storey count (or a
given range) -- a floor x side summary table instead of one 3D ray fan per
floor, for a "boss can read this in 10 seconds" report.

Reuses analyze_view.py's analyze() unchanged (same math/thresholds, same
height-plausibility heuristic) -- this is purely a different way of
aggregating and presenting its output across many floors at once, not a
new analysis method.

Sides are defined as four 90-degree bearing quadrants:
    N = 315-45, E = 45-135, S = 135-225, W = 225-315
This is a simplification -- an irregularly-shaped building's real facades
may not align with true N/E/S/W. Good enough for a summary; use the
viewer's per-facade click for a specific wall's exact bearing.

Usage:
    python facade_blockage_report.py cache/one-raffles-place.geojson
    python facade_blockage_report.py cache/one-raffles-place.geojson --max-floor 30 --step 10
Writes:
    cache/<slug>_facade_blockage.csv
"""
import argparse
import csv
import json
from pathlib import Path

from analyze_view import analyze, find_subject_building, DEFAULT_METERS_PER_STOREY, DEFAULT_EYE_HEIGHT_ABOVE_FLOOR

SIDES = {
    "N": lambda b: b >= 315 or b < 45,
    "E": lambda b: 45 <= b < 135,
    "S": lambda b: 135 <= b < 225,
    "W": lambda b: 225 <= b < 315,
}


def side_breakdown(directions):
    result = {}
    for side, in_range in SIDES.items():
        bucket = [d for d in directions if in_range(d["bearing"])]
        if not bucket:
            result[side] = None
            continue
        n = len(bucket)
        blocked = sum(1 for d in bucket if d["status"] == "blocked")
        uncertain = sum(1 for d in bucket if d["status"] == "uncertain")
        open_ = n - blocked - uncertain
        result[side] = {
            "blocked_pct": round(100 * blocked / n, 1),
            "uncertain_pct": round(100 * uncertain / n, 1),
            "open_pct": round(100 * open_ / n, 1),
        }
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("geojson", help="Path to a buildings geojson from fetch_buildings.py")
    ap.add_argument("--min-floor", type=int, default=1)
    ap.add_argument("--max-floor", type=int, default=None, help="Default: subject building's own known storey count, or 40 if unknown")
    ap.add_argument("--step", type=int, default=1, help="Check every Nth floor (default 1 = every floor)")
    ap.add_argument("--meters-per-storey", type=float, default=DEFAULT_METERS_PER_STOREY)
    ap.add_argument("--eye-height-above-floor", type=float, default=DEFAULT_EYE_HEIGHT_ABOVE_FLOOR)
    ap.add_argument("--radius", type=float, default=None)
    ap.add_argument("--ray-step", type=int, default=5, help="Degree step between rays within analyze() (default 5)")
    ap.add_argument("--max-obstruction-distance", type=float, default=None,
                     help="Ignore buildings beyond this distance as obstructions -- 'background noise' "
                          "you can see around, even if technically taller than eye height (default: no cutoff)")
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    radius = args.radius or (data.get("subject", {}).get("radius_m") or 500)

    max_floor = args.max_floor
    if max_floor is None:
        subject_id = find_subject_building(data["features"])
        subject = next((f for f in data["features"] if f["properties"].get("osm_id") == subject_id), None)
        if subject and subject["properties"].get("levels"):
            max_floor = int(subject["properties"]["levels"])
            print(f"Subject building's own known storey count: {max_floor}")
        else:
            max_floor = 40
            print(f"Subject building's own height is unknown -- defaulting to {max_floor} floors. "
                  f"Pass --max-floor N if you know the real count.")

    rows = []
    for floor in range(args.min_floor, max_floor + 1, args.step):
        report = analyze(geojson_path, floor, args.meters_per_storey, args.eye_height_above_floor,
                          radius, None, None, args.ray_step, args.max_obstruction_distance)
        sides = side_breakdown(report["directions"])
        row = {"floor": floor, "eye_height_m": report["eye_height_m"]}
        for side, stats in sides.items():
            if stats is None:
                row[f"{side}_blocked_%"] = row[f"{side}_uncertain_%"] = row[f"{side}_open_%"] = ""
            else:
                row[f"{side}_blocked_%"] = stats["blocked_pct"]
                row[f"{side}_uncertain_%"] = stats["uncertain_pct"]
                row[f"{side}_open_%"] = stats["open_pct"]
        rows.append(row)

    # console summary
    print(f"\n{'Floor':>5}  {'Eye(m)':>7}  " + "  ".join(f"{s} blocked%" for s in SIDES))
    for row in rows:
        vals = "  ".join(f"{row[f'{s}_blocked_%']:>11}" for s in SIDES)
        print(f"{row['floor']:>5}  {row['eye_height_m']:>7.1f}  {vals}")

    out_path = geojson_path.parent / f"{geojson_path.stem}_facade_blockage.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
