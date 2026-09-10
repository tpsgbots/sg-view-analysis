"""
Line-of-sight (straight-line, "as the crow flies") distance from an address
to the nearest instance of each place-of-interest category -- MRT station,
MRT entrance, bus stop, bus interchange, mall, school/institution, park.

Not walking/driving distance -- pure straight-line geodesic distance. Reuses
the whole-island layer caches already built (build_island_layers.py,
build_island_parks_schools.py) -- no new data fetching, this is pure
computation over existing data.

Usage:
    python poi_distances.py "One Raffles Place"
    python poi_distances.py "55 Newton Road" --top 3
Writes:
    nothing -- prints a report to stdout
"""
import argparse
import json
import math
from pathlib import Path

from fetch_buildings import geocode

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def point_to_segment_deg(px, py, ax, ay, bx, by):
    """Nearest point on segment A-B to point P, all in lon/lat degrees.
    Flat-plane approximation (fine at this scale) -- returns (nx, ny)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ax, ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    return ax + t * dx, ay + t * dy


def distance_to_polygon_m(lat, lon, ring):
    """Min haversine distance from (lat,lon) to any edge of a lon/lat ring."""
    best = None
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        nx, ny = point_to_segment_deg(lon, lat, ax, ay, bx, by)
        d = haversine_m(lat, lon, ny, nx)
        if best is None or d < best:
            best = d
    return best


def load_layer(filename):
    path = CACHE_DIR / filename
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def nearest(lat, lon, features, top_n, label_fn, is_polygon):
    scored = []
    for f in features:
        if is_polygon:
            ring = f["geometry"]["coordinates"][0]
            d = distance_to_polygon_m(lat, lon, ring)
        else:
            plon, plat = f["geometry"]["coordinates"]
            d = haversine_m(lat, lon, plat, plon)
        scored.append((d, f))
    scored.sort(key=lambda x: x[0])
    return [(d, label_fn(f)) for d, f in scored[:top_n]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address", help="Address or postal code to geocode")
    ap.add_argument("--top", type=int, default=1, help="How many nearest per category to show (default 1)")
    args = ap.parse_args()

    print(f"Geocoding: {args.address!r}")
    geo = geocode(args.address)
    lat, lon = geo["lat"], geo["lon"]
    print(f"  -> {geo['address']}\n")

    transit = load_layer("sg_transit_full.geojson")
    malls = load_layer("sg_malls_full.geojson")
    schools = load_layer("sg_schools_full.geojson")
    parks = load_layer("sg_parks_full.geojson")

    def fmt(name, d):
        km = d / 1000
        return f"  {name or '(unnamed)'} -- {d:.0f}m ({km:.2f}km)"

    if transit:
        for cat, label in [
            ("mrt_station", "Nearest MRT station"),
            ("mrt_entrance", "Nearest MRT entrance"),
            ("bus_stop", "Nearest bus stop"),
            ("bus_interchange", "Nearest bus interchange"),
        ]:
            feats = [f for f in transit["features"] if f["properties"]["category"] == cat]
            if not feats:
                continue
            results = nearest(lat, lon, feats, args.top, lambda f: f["properties"]["name"], is_polygon=False)
            print(f"{label}:")
            for d, name in results:
                print(fmt(name, d))
            print()

    if malls and malls["features"]:
        print("Nearest mall(s):")
        for d, name in nearest(lat, lon, malls["features"], args.top, lambda f: f["properties"]["name"], is_polygon=False):
            print(fmt(name, d))
        print()

    if schools and schools["features"]:
        print("Nearest school/institution(s):")
        for d, name in nearest(lat, lon, schools["features"], args.top,
                                lambda f: f"{f['properties']['name']} ({f['properties']['category']})", is_polygon=False):
            print(fmt(name, d))
        print()

    if parks and parks["features"]:
        print("Nearest park(s) (distance to boundary, not centroid):")
        for d, name in nearest(lat, lon, parks["features"], args.top, lambda f: f["properties"]["name"], is_polygon=True):
            print(fmt(name, d))
        print()


if __name__ == "__main__":
    main()
