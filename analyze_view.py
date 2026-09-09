"""
Sightline / view analysis: from a subject point + floor, is each compass
direction blocked or open, and by what?

Model (deliberately simple, matches what "is my view blocked" usually means):
  - Treat the sightline as horizontal from eye height outward (not a downward
    angle to a specific target) -- reasonable at the distances involved here.
  - For each compass bearing, cast a ray outward and find every building
    footprint it crosses, with the distance to each crossing.
  - The nearest building along that ray whose height exceeds the eye height
    is the blocker; the view in that direction is blocked from that distance
    onward. If no such building exists within the search radius, that
    direction is open.
  - A building with unknown height (no OSM tag, no manual override) that
    sits closer than any confirmed blocker makes that direction "uncertain"
    rather than guessing -- flagged so you know to check it manually.
  - The subject's own building footprint (whichever polygon contains the
    origin point) is excluded from obstructing itself.

Usage:
    python analyze_view.py cache/55-newton-road.geojson --floor 12
    python analyze_view.py cache/55-newton-road.geojson --floor 12 --bearing 45 --arc 90
Writes:
    cache/<slug>_floor<N>_view.json
"""
import argparse
import json
import math
from pathlib import Path

from fetch_buildings import plausible_max_height_m

HERE = Path(__file__).parent
DEFAULT_METERS_PER_STOREY = 3.0
DEFAULT_EYE_HEIGHT_ABOVE_FLOOR = 1.5


def point_in_polygon(x, y, ring):
    """Standard ray-casting point-in-polygon test."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def ray_segment_intersection(ox, oy, dx, dy, p1, p2):
    """Distance t>=0 along ray (ox,oy)+t*(dx,dy) where it crosses segment p1-p2, or None.

    Solve O + t*D = P1 + s*E for (t, s), E = P2-P1, R = P1-O:
        t = (ey*rx - ex*ry) / denom
        s = (dy*rx - dx*ry) / denom
        denom = dx*ey - dy*ex
    """
    x1, y1 = p1
    x2, y2 = p2
    ex, ey = x2 - x1, y2 - y1
    rx, ry = x1 - ox, y1 - oy
    denom = dx * ey - dy * ex
    if abs(denom) < 1e-12:
        return None
    t = (ey * rx - ex * ry) / denom
    s = (dy * rx - dx * ry) / denom
    if t >= 0 and 0 <= s <= 1:
        return t
    return None


def ray_polygon_min_distance(ox, oy, dx, dy, ring):
    """Nearest distance along the ray where it enters this polygon, or None if it misses."""
    dists = []
    n = len(ring)
    for i in range(n):
        p1, p2 = ring[i], ring[(i + 1) % n]
        t = ray_segment_intersection(ox, oy, dx, dy, tuple(p1), tuple(p2))
        if t is not None:
            dists.append(t)
    return min(dists) if dists else None


def find_subject_building(features):
    for f in features:
        ring = f["geometry"]["coordinates"][0]
        if point_in_polygon(0, 0, ring):
            return f["properties"].get("osm_id")
    return None


def bearing_to_dxdy(bearing_deg):
    """0 = North (+y), 90 = East (+x), clockwise -- matches compass convention."""
    rad = math.radians(bearing_deg)
    return math.sin(rad), math.cos(rad)


def bearing_label(b):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(b / 22.5) % 16]


def analyze(geojson_path: Path, floor: int, meters_per_storey: float, eye_height_above_floor: float,
            radius_m: float, bearing_center=None, arc_deg=None, step_deg=5):
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = data["features"]
    subject_id = find_subject_building(features)

    eye_height = (floor - 1) * meters_per_storey + eye_height_above_floor

    if bearing_center is not None and arc_deg is not None:
        half = arc_deg / 2
        bearings = [b % 360 for b in range(int(bearing_center - half), int(bearing_center + half) + 1, step_deg)]
    else:
        bearings = list(range(0, 360, step_deg))

    results = []
    for b in bearings:
        dx, dy = bearing_to_dxdy(b)
        confirmed_blocker = None  # (distance, feature)
        uncertain_blocker = None  # (distance, feature) -- unknown height, closer than any confirmed blocker

        for f in features:
            p = f["properties"]
            if p.get("osm_id") == subject_id:
                continue
            ring = f["geometry"]["coordinates"][0]
            dist = ray_polygon_min_distance(0, 0, dx, dy, ring)
            if dist is None or dist > radius_m or dist < 3:  # 3m: ignore near-self noise
                continue
            height = p.get("height_m")
            if height is not None:
                if height > eye_height:
                    if confirmed_blocker is None or dist < confirmed_blocker[0]:
                        confirmed_blocker = (dist, f)
            else:
                # Height-plausibility heuristic (2026-09-10): a small-footprint
                # unknown building is very unlikely to reach a high eye height --
                # if its plausible max is still below eye level, it can't be a
                # real blocker, so skip it entirely rather than flag "uncertain".
                # Large-footprint unknowns (plausible_max is None) stay uncertain
                # as before -- this never asserts a specific real height, only
                # rules a building out when it plausibly CAN'T reach eye level.
                plausible_max = plausible_max_height_m(p.get("footprint_area_m2"), p.get("building_type"))
                if plausible_max is not None and plausible_max <= eye_height:
                    continue
                if uncertain_blocker is None or dist < uncertain_blocker[0]:
                    uncertain_blocker = (dist, f)

        # A confirmed (known, taller-than-eye) blocker ANYWHERE within radius makes this
        # direction "blocked", even if a nearer unknown-height obstruction also exists --
        # the confirmed blocker guarantees the view isn't open regardless of what the
        # closer unknown building turns out to be. Only classify "uncertain" when the
        # whole direction's fate genuinely hinges on an unknown height (no confirmed
        # blocker exists at all within radius). Fixed 2026-09-09: previously a closer
        # unknown obstruction downgraded a definite-blocked direction to "uncertain",
        # which was misleading -- see LESSONS.md.
        if confirmed_blocker:
            dist, f = confirmed_blocker
            p = f["properties"]
            entry = {
                "bearing": b, "compass": bearing_label(b), "status": "blocked",
                "distance_m": round(dist, 1),
                "blocker": {"name": p.get("name") or p.get("addr") or f"osm:{p.get('osm_id')}",
                            "height_m": p.get("height_m"), "osm_id": p.get("osm_id")},
            }
            if uncertain_blocker and uncertain_blocker[0] < confirmed_blocker[0]:
                up = uncertain_blocker[1]["properties"]
                uname = up.get("name") or up.get("addr") or f"osm:{up.get('osm_id')}"
                entry["note"] = f"may be blocked even sooner (~{uncertain_blocker[0]:.0f}m) by {uname} -- its height is unknown"
            results.append(entry)
        elif uncertain_blocker:
            dist, f = uncertain_blocker
            p = f["properties"]
            results.append({
                "bearing": b, "compass": bearing_label(b), "status": "uncertain",
                "distance_m": round(dist, 1),
                "blocker": {"name": p.get("name") or p.get("addr") or f"osm:{p.get('osm_id')}",
                            "height_m": None, "osm_id": p.get("osm_id")},
                "note": "closest obstruction has no known height -- supply it via cache/manual_heights.json to resolve",
            })
        else:
            results.append({"bearing": b, "compass": bearing_label(b), "status": "open", "distance_m": None, "blocker": None})

    return {
        "subject": data.get("subject"),
        "floor": floor,
        "eye_height_m": round(eye_height, 1),
        "meters_per_storey": meters_per_storey,
        "radius_m": radius_m,
        "directions": results,
    }


def summarize(report: dict) -> str:
    lines = []
    dirs = report["directions"]
    blocked = [d for d in dirs if d["status"] == "blocked"]
    open_ = [d for d in dirs if d["status"] == "open"]
    uncertain = [d for d in dirs if d["status"] == "uncertain"]
    lines.append(f"Floor {report['floor']} (eye height {report['eye_height_m']}m), radius {report['radius_m']:.0f}m")
    lines.append(f"  {len(open_)} open, {len(blocked)} blocked, {len(uncertain)} uncertain (of {len(dirs)} directions checked)")
    if open_:
        compasses = sorted(set(d["compass"] for d in open_))
        lines.append(f"  Open: {', '.join(compasses)}")
    if blocked:
        by_blocker = {}
        for d in blocked:
            key = d["blocker"]["name"]
            by_blocker.setdefault(key, []).append(d)
        for name, ds in sorted(by_blocker.items(), key=lambda kv: min(x["distance_m"] for x in kv[1])):
            compasses = sorted(set(d["compass"] for d in ds))
            near = min(x["distance_m"] for x in ds)
            lines.append(f"  Blocked {', '.join(compasses)}: {name} (~{near:.0f}m, {ds[0]['blocker']['height_m']:.0f}m tall)")
    if uncertain:
        compasses = sorted(set(d["compass"] for d in uncertain))
        lines.append(f"  Uncertain (unknown-height obstruction closest): {', '.join(compasses)}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("geojson", help="Path to a buildings geojson from fetch_buildings.py")
    ap.add_argument("--floor", type=int, required=True, help="Floor number (1 = ground)")
    ap.add_argument("--meters-per-storey", type=float, default=DEFAULT_METERS_PER_STOREY)
    ap.add_argument("--eye-height-above-floor", type=float, default=DEFAULT_EYE_HEIGHT_ABOVE_FLOOR)
    ap.add_argument("--radius", type=float, default=None, help="Analysis radius in meters (default: same as fetch radius)")
    ap.add_argument("--bearing", type=float, default=None, help="Center facing direction in degrees (0=N, 90=E) -- omit to check full 360")
    ap.add_argument("--arc", type=float, default=90, help="Arc width in degrees around --bearing (default 90)")
    ap.add_argument("--step", type=int, default=5, help="Degree step between rays (default 5)")
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    radius = args.radius or (data.get("subject", {}).get("radius_m") or 500)

    report = analyze(
        geojson_path, args.floor, args.meters_per_storey, args.eye_height_above_floor,
        radius, args.bearing, args.arc if args.bearing is not None else None, args.step,
    )

    print(summarize(report))

    out_path = geojson_path.parent / f"{geojson_path.stem}_floor{args.floor}_view.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
