"""
One-time (re-runnable) bulk build of whole-Singapore context extras: parks,
educational institutions, retail malls, and expressway on/off ramps --
visual/orientation context, same purpose as roads/water/transit
(build_island_layers.py). Not obstructions.

Separate script/tile cache from build_island_layers.py deliberately -- added
after that build was already most of the way through its own run, and running
two heavy tiled Overpass fetches concurrently would compound the rate-limiting
both already have to fight (see LESSONS.md). Run this only after
build_island_layers.py has finished.

Educational institutions are fetched as both ways (building/campus footprint,
the common case for schools) and nodes (a POI point, common for smaller
institutions) -- large campuses mapped as OSM relations (e.g. some university
precincts) are out of scope for now, same simplification already made for
water bodies in build_island_layers.py.

Retail malls: a mall's physical building footprint is (almost always) already
in the main buildings cache -- it carries a `building=retail/commercial` tag
alongside `shop=mall`, which is what build_island_cache.py's `way["building"]`
query matches. This script fetches `shop=mall` separately just to get a POINT
marker (mall NAME + location) the viewer can highlight distinctly from a
generic building -- it is not a new footprint/height source.

Expressway ramps/slip roads (`*_link` highway classes -- motorway_link,
trunk_link, etc.) were missing from build_island_layers.py's road fetch,
confirmed 2026-09-10: the main carriageway (`motorway`/`trunk`) was already
covered, but on/off ramps use the separate `_link` tag and need their own
query. Fetched here as roads, output alongside (not merged into) the main
roads file so re-running build_island_layers.py doesn't need to change.

Usage:
    python build_island_parks_schools.py
Writes:
    cache/sg_parks_full.geojson
    cache/sg_schools_full.geojson
    cache/sg_malls_full.geojson
    cache/sg_road_links_full.geojson
    cache/_tiles_parks_schools/ (raw per-tile responses, resumable)
"""
import functools
import json
import subprocess
import time
from pathlib import Path

print = functools.partial(print, flush=True)  # see LESSONS.md 2026-09-09

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
TILE_DIR = CACHE_DIR / "_tiles_parks_schools"
TILE_DIR.mkdir(parents=True, exist_ok=True)

SOUTH, NORTH = 1.14, 1.48
WEST, EAST = 103.59, 104.05
TILE_DEG = 0.06

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

SCHOOL_CLASSES = "school|university|college|kindergarten"
ROAD_LINK_CLASSES = "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link"


def frange(start, stop, step):
    v = start
    while v < stop:
        yield v
        v += step


def build_tiles():
    tiles = []
    for lat in frange(SOUTH, NORTH, TILE_DEG):
        for lon in frange(WEST, EAST, TILE_DEG):
            tiles.append((lat, lon, min(lat + TILE_DEG, NORTH), min(lon + TILE_DEG, EAST)))
    return tiles


def tile_is_valid(tile_path: Path) -> bool:
    if not tile_path.exists():
        return False
    try:
        data = json.loads(tile_path.read_text(encoding="utf-8"))
        return "elements" in data
    except Exception:
        return False


def fetch_tile(south, west, north, east, tile_path: Path, retries_per_mirror=2):
    if tile_is_valid(tile_path):
        return "cached"
    query = (
        f'[out:json][timeout:90];'
        f'('
        f'way["leisure"="park"]({south},{west},{north},{east});'
        f'way["amenity"~"^({SCHOOL_CLASSES})$"]({south},{west},{north},{east});'
        f'node["amenity"~"^({SCHOOL_CLASSES})$"]({south},{west},{north},{east});'
        f'way["shop"="mall"]({south},{west},{north},{east});'
        f'node["shop"="mall"]({south},{west},{north},{east});'
        f'way["highway"~"^({ROAD_LINK_CLASSES})$"]({south},{west},{north},{east});'
        f');'
        f'out body geom;'
    )
    attempt = 0
    for mirror in OVERPASS_MIRRORS:
        for local_attempt in range(1, retries_per_mirror + 1):
            attempt += 1
            result = subprocess.run(
                ["curl", "-s", "-m", "100", "-X", "POST", mirror,
                 "--data-urlencode", f"data={query}", "-o", str(tile_path), "-w", "%{http_code}"],
                capture_output=True, text=True,
            )
            code = result.stdout.strip()
            if code == "200" and tile_is_valid(tile_path):
                return "ok"
            backoff = 15 * local_attempt
            print(f"    attempt {attempt} [{mirror.split('/')[2]}]: HTTP {code}, backing off {backoff}s...")
            time.sleep(backoff)
        print(f"    giving up on {mirror.split('/')[2]}, trying next mirror...")
    return "failed"


def main():
    tiles = build_tiles()
    print(f"{len(tiles)} tiles to fetch ({TILE_DEG} deg squares)")

    consecutive_failures = 0
    for i, (s, w, n, e) in enumerate(tiles, 1):
        tile_path = TILE_DIR / f"tile_{i:03d}.json"
        status = fetch_tile(s, w, n, e, tile_path)
        print(f"[{i}/{len(tiles)}] bbox=({s:.2f},{w:.2f},{n:.2f},{e:.2f}) -> {status}")
        if status == "failed":
            print(f"    WARNING: tile {i} failed after retries, skipping (rerun script later to retry)")
            consecutive_failures += 1
            if consecutive_failures >= 4:
                print(f"    {consecutive_failures} tiles in a row have failed -- pausing 3 minutes.")
                time.sleep(180)
                consecutive_failures = 0
        else:
            consecutive_failures = 0
        if status != "cached":
            time.sleep(6)

    print("\nMerging tiles...")
    parks_by_id, schools_by_id, malls_by_id, links_by_id = {}, {}, {}, {}
    bad_tiles = 0
    for tile_path in sorted(TILE_DIR.glob("tile_*.json")):
        try:
            data = json.loads(tile_path.read_text(encoding="utf-8"))
        except Exception:
            bad_tiles += 1
            continue
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            if el.get("type") == "way" and "geometry" in el and tags.get("leisure") == "park":
                parks_by_id[el["id"]] = el
            elif tags.get("amenity") in ("school", "university", "college", "kindergarten"):
                schools_by_id[(el["type"], el["id"])] = el
            elif tags.get("shop") == "mall":
                malls_by_id[(el["type"], el["id"])] = el
            elif el.get("type") == "way" and "geometry" in el and tags.get("highway", "").endswith("_link"):
                links_by_id[el["id"]] = el
    print(f"parks={len(parks_by_id)} schools={len(schools_by_id)} malls={len(malls_by_id)} "
          f"road_links={len(links_by_id)} ({bad_tiles} unreadable tiles)")

    park_features = []
    for el in parks_by_id.values():
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 3:
            continue
        park_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": el.get("tags", {}).get("name")},
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        })
    (CACHE_DIR / "sg_parks_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": park_features}), encoding="utf-8")
    print(f"Wrote sg_parks_full.geojson: {len(park_features)} parks")

    school_features = []
    for el in schools_by_id.values():
        tags = el.get("tags", {})
        if el["type"] == "node":
            point = [el["lon"], el["lat"]]
        else:
            ring = [[pt["lon"], pt["lat"]] for pt in el.get("geometry", [])]
            if not ring:
                continue
            point = [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
        school_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": tags.get("name"), "category": tags.get("amenity")},
            "geometry": {"type": "Point", "coordinates": point},
        })
    (CACHE_DIR / "sg_schools_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": school_features}), encoding="utf-8")
    print(f"Wrote sg_schools_full.geojson: {len(school_features)} institutions")

    mall_features = []
    for el in malls_by_id.values():
        tags = el.get("tags", {})
        if el["type"] == "node":
            point = [el["lon"], el["lat"]]
        else:
            ring = [[pt["lon"], pt["lat"]] for pt in el.get("geometry", [])]
            if not ring:
                continue
            point = [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
        mall_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": tags.get("name")},
            "geometry": {"type": "Point", "coordinates": point},
        })
    (CACHE_DIR / "sg_malls_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": mall_features}), encoding="utf-8")
    print(f"Wrote sg_malls_full.geojson: {len(mall_features)} malls")

    link_features = []
    for el in links_by_id.values():
        tags = el.get("tags", {})
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        link_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": tags.get("name"), "highway": tags.get("highway")},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    (CACHE_DIR / "sg_road_links_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": link_features}), encoding="utf-8")
    print(f"Wrote sg_road_links_full.geojson: {len(link_features)} ramp/link segments")


if __name__ == "__main__":
    main()
