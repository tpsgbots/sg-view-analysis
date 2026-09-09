"""
List named buildings with unknown height near an address (or matching a name
substring), to scope a height-research pass. This replaces the throwaway
inline Python scripts written ad-hoc for the CBD tower research sessions --
same query, now a reusable command.

Doesn't do the actual research itself (that needs WebSearch, an agent
capability) -- this just answers "what's missing here" so you know what to
hand a research agent, or check cache/manual_heights.json / hdb_heights.json
coverage directly.

Usage:
    python find_unknown_buildings.py "One Raffles Place" --radius 600
    python find_unknown_buildings.py --name "Tower"          # search by name substring, whole island
    python find_unknown_buildings.py "Suntec City" --radius 500 --min-footprint-area 400
"""
import argparse
import json
import math
from pathlib import Path

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def polygon_area(ring):
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area / 2) * (111_320 ** 2)  # rough deg^2 -> m^2, fine at this scale


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address", nargs="?", help="Address to geocode as the search center (omit with --name for a whole-island name search)")
    ap.add_argument("--radius", type=float, default=500, help="Radius in meters (default 500, ignored without an address)")
    ap.add_argument("--name", help="Only show buildings whose name contains this substring (case-insensitive)")
    ap.add_argument("--min-footprint-area", type=float, default=150, help="Skip buildings smaller than this footprint area in m^2 (default 150 -- filters out small sheds/kiosks)")
    args = ap.parse_args()

    if not args.address and not args.name:
        raise SystemExit("Provide an address, --name, or both")

    island_cache = CACHE_DIR / "sg_buildings_full.geojson"
    if not island_cache.exists():
        raise SystemExit("cache/sg_buildings_full.geojson not found -- run build_island_cache.py first")
    data = json.loads(island_cache.read_text(encoding="utf-8"))

    center = None
    if args.address:
        from fetch_buildings import geocode
        geo = geocode(args.address)
        center = (geo["lat"], geo["lon"])
        print(f"Center: {geo['address']} ({geo['lat']:.6f}, {geo['lon']:.6f})")

    results = []
    for f in data["features"]:
        p = f["properties"]
        if p.get("levels"):
            continue  # already has a height
        name = p.get("name")
        if not name:
            continue
        if args.name and args.name.lower() not in name.lower():
            continue
        ring = f["geometry"]["coordinates"][0]
        area = polygon_area(ring)
        if area < args.min_footprint_area:
            continue
        if center:
            clat = sum(pt[1] for pt in ring) / len(ring)
            clon = sum(pt[0] for pt in ring) / len(ring)
            dist = haversine_m(center[0], center[1], clat, clon)
            if dist > args.radius:
                continue
            results.append((dist, name, area))
        else:
            results.append((None, name, area))

    results.sort(key=lambda r: (r[0] is None, r[0] or 0))
    seen = set()
    print(f"\n{len(results)} unknown-height named buildings found (footprint >= {args.min_footprint_area:.0f}m^2):\n")
    for dist, name, area in results:
        if name in seen:
            continue
        seen.add(name)
        dist_str = f"{dist:.0f}m" if dist is not None else "-"
        print(f"  {dist_str:>7}  {area:>6.0f}m^2  {name}")


if __name__ == "__main__":
    main()
