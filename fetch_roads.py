"""
Fetch road centerlines around a Singapore address, for visual/orientation
context in the viewer -- roads don't block sightlines, this is purely so the
3D scene doesn't look like buildings floating in a void.

Small per-address radius query, live via Overpass each time (no island-wide
cache needed here -- roads are a much smaller/cheaper pull than buildings,
and unlike the buildings cache this doesn't need to survive repeat lookups
across many addresses).

Usage:
    python fetch_roads.py "55 Newton Road" --radius 500
Writes:
    cache/<slug>_roads.geojson
"""
import argparse
import json
import math
import re
import subprocess
from pathlib import Path

from fetch_buildings import geocode, latlon_to_local_xy, slugify, CACHE_DIR

# wider than motorway/trunk/primary/secondary/tertiary/residential/service alone --
# covers the common classes without pulling footpaths/cycleways
ROAD_CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "unclassified", "service", "living_street",
]


def overpass_roads(lat: float, lon: float, radius_m: float) -> list:
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    south, north = lat - dlat, lat + dlat
    west, east = lon - dlon, lon + dlon

    class_filter = "|".join(ROAD_CLASSES)
    query = (
        f'[out:json][timeout:60];'
        f'(way["highway"~"^({class_filter})$"]({south},{west},{north},{east}););'
        f'out body geom;'
    )
    out_file = CACHE_DIR / "_overpass_roads_raw.json"
    result = subprocess.run(
        [
            "curl", "-s", "-m", "60", "-X", "POST",
            "https://overpass-api.de/api/interpreter",
            "--data-urlencode", f"data={query}",
            "-o", str(out_file),
            "-w", "%{http_code}",
        ],
        capture_output=True, text=True,
    )
    if result.stdout.strip() != "200":
        raise SystemExit(f"Overpass roads request failed, HTTP {result.stdout.strip()}: {result.stderr}")
    data = json.loads(out_file.read_text(encoding="utf-8"))
    return data.get("elements", [])


def build_roads_geojson(elements: list, origin_lat: float, origin_lon: float) -> dict:
    features = []
    for el in elements:
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        coords = [list(latlon_to_local_xy(pt["lat"], pt["lon"], origin_lat, origin_lon)) for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "osm_id": el.get("id"),
                "name": tags.get("name"),
                "highway": tags.get("highway"),
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address", help="Address to geocode, e.g. '55 Newton Road'")
    ap.add_argument("--radius", type=float, default=500)
    args = ap.parse_args()

    print(f"Geocoding: {args.address!r}")
    geo = geocode(args.address)
    print(f"  -> {geo['address']}")

    print(f"Fetching roads within {args.radius:.0f}m via Overpass...")
    elements = overpass_roads(geo["lat"], geo["lon"], args.radius)
    geojson = build_roads_geojson(elements, geo["lat"], geo["lon"])
    print(f"  -> {len(geojson['features'])} road segments")

    slug = slugify(args.address)
    out_path = CACHE_DIR / f"{slug}_roads.geojson"
    out_path.write_text(json.dumps(geojson), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
