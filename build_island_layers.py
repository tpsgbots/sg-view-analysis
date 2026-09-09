"""
One-time (re-runnable) bulk build of whole-Singapore OSM context layers:
roads, transit points (MRT stations/entrances, bus stops/interchanges), and
water bodies -- for visual/orientation context in the viewer, not obstruction
analysis (none of these block sightlines).

Same tiling approach as build_island_cache.py (a single whole-island Overpass
query times out even for transit points alone, confirmed 2026-09-09, despite
that data being far sparser than buildings) -- roads + simple water ways +
transit nodes are fetched together in ONE combined query per tile to keep the
total request count down. Water BODIES as multipolygon relations (e.g. Marina
Bay is a `relation[natural=water]`, not a simple way) are sparse enough
island-wide to fetch in a single untiled query instead, with basic outer-ring
assembly (inner/hole rings are dropped -- good enough for "there's water
here" context, not surveyed accuracy).

Usage:
    python build_island_layers.py
Writes:
    cache/sg_roads_full.geojson
    cache/sg_transit_full.geojson
    cache/sg_water_full.geojson
    cache/_tiles_layers/ (raw per-tile responses, resumable)
"""
import functools
import json
import re
import subprocess
import time
from pathlib import Path

print = functools.partial(print, flush=True)  # see LESSONS.md 2026-09-09 -- redirected stdout buffers otherwise

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
TILE_DIR = CACHE_DIR / "_tiles_layers"
TILE_DIR.mkdir(parents=True, exist_ok=True)

SOUTH, NORTH = 1.14, 1.48
WEST, EAST = 103.59, 104.05
TILE_DEG = 0.06

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

ROAD_CLASSES = "motorway|trunk|primary|secondary|tertiary|residential|unclassified|service|living_street"


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


def overpass_request(query: str, out_path: Path, retries_per_mirror=2) -> str:
    attempt = 0
    for mirror in OVERPASS_MIRRORS:
        for local_attempt in range(1, retries_per_mirror + 1):
            attempt += 1
            result = subprocess.run(
                ["curl", "-s", "-m", "100", "-X", "POST", mirror,
                 "--data-urlencode", f"data={query}", "-o", str(out_path), "-w", "%{http_code}"],
                capture_output=True, text=True,
            )
            code = result.stdout.strip()
            if code == "200" and tile_is_valid(out_path):
                return "ok"
            backoff = 15 * local_attempt
            print(f"    attempt {attempt} [{mirror.split('/')[2]}]: HTTP {code}, backing off {backoff}s...")
            time.sleep(backoff)
        print(f"    giving up on {mirror.split('/')[2]}, trying next mirror...")
    return "failed"


def fetch_tile(south, west, north, east, tile_path: Path):
    if tile_is_valid(tile_path):
        return "cached"
    query = (
        f'[out:json][timeout:90];'
        f'('
        f'way["highway"~"^({ROAD_CLASSES})$"]({south},{west},{north},{east});'
        f'way["natural"="water"]({south},{west},{north},{east});'
        f'way["water"]({south},{west},{north},{east});'
        f'node["railway"="station"]({south},{west},{north},{east});'
        f'node["railway"="subway_entrance"]({south},{west},{north},{east});'
        f'node["highway"="bus_stop"]({south},{west},{north},{east});'
        f'node["amenity"="bus_station"]({south},{west},{north},{east});'
        f'way["amenity"="bus_station"]({south},{west},{north},{east});'
        f');'
        f'out body geom;'
    )
    return overpass_request(query, tile_path)


def fetch_water_relations():
    """Marina Bay etc. are relations (multipolygons), not simple ways --
    sparse enough island-wide to fetch in one shot rather than tiling."""
    out_path = CACHE_DIR / "_water_relations_raw.json"
    query = (
        f'[out:json][timeout:120];'
        f'(relation["natural"="water"]({SOUTH},{WEST},{NORTH},{EAST});'
        f'relation["water"]({SOUTH},{WEST},{NORTH},{EAST}););'
        f'out geom;'
    )
    status = overpass_request(query, out_path, retries_per_mirror=3)
    if status != "ok":
        print("  WARNING: water relations fetch failed, water layer will only have simple-way features")
        return []
    data = json.loads(out_path.read_text(encoding="utf-8"))
    return data.get("elements", [])


def assemble_relation_outer_ring(rel: dict):
    """Join outer-role member ways end-to-end into one ring. Drops inner
    (hole) rings and gives up (returns None) if the outer ways don't close
    into a single loop -- good enough for "there's water here" context."""
    outer_segments = [
        [(pt["lon"], pt["lat"]) for pt in m["geometry"]]
        for m in rel.get("members", [])
        if m.get("role") == "outer" and "geometry" in m
    ]
    if not outer_segments:
        return None
    ring = list(outer_segments.pop(0))
    guard = 0
    while outer_segments and guard < 200:
        guard += 1
        progressed = False
        for i, seg in enumerate(outer_segments):
            if seg[0] == ring[-1]:
                ring.extend(seg[1:]); outer_segments.pop(i); progressed = True; break
            if seg[-1] == ring[-1]:
                ring.extend(list(reversed(seg))[1:]); outer_segments.pop(i); progressed = True; break
            if seg[0] == ring[0]:
                ring = list(reversed(seg))[:-1] + ring; outer_segments.pop(i); progressed = True; break
            if seg[-1] == ring[0]:
                ring = seg[:-1] + ring; outer_segments.pop(i); progressed = True; break
        if not progressed:
            break
    if len(ring) < 3:
        return None
    return ring


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
    roads_by_id, water_ways_by_id, transit_by_id = {}, {}, {}
    bad_tiles = 0
    for tile_path in sorted(TILE_DIR.glob("tile_*.json")):
        try:
            data = json.loads(tile_path.read_text(encoding="utf-8"))
        except Exception:
            bad_tiles += 1
            continue
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            if el.get("type") == "way" and "geometry" in el and tags.get("highway"):
                roads_by_id[el["id"]] = el
            elif el.get("type") == "way" and "geometry" in el and (tags.get("natural") == "water" or tags.get("water")):
                water_ways_by_id[el["id"]] = el
            elif el.get("type") in ("node", "way") and (
                tags.get("railway") in ("station", "subway_entrance")
                or tags.get("highway") == "bus_stop"
                or tags.get("amenity") == "bus_station"
            ):
                transit_by_id[(el["type"], el["id"])] = el
    print(f"roads={len(roads_by_id)} water_ways={len(water_ways_by_id)} transit={len(transit_by_id)} ({bad_tiles} unreadable tiles)")

    # --- roads ---
    road_features = []
    for el in roads_by_id.values():
        tags = el.get("tags", {})
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        road_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": tags.get("name"), "highway": tags.get("highway")},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    (CACHE_DIR / "sg_roads_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": road_features}), encoding="utf-8")
    print(f"Wrote sg_roads_full.geojson: {len(road_features)} segments")

    # --- transit ---
    transit_features = []
    for el in transit_by_id.values():
        tags = el.get("tags", {})
        category = ("mrt_station" if tags.get("railway") == "station"
                    else "mrt_entrance" if tags.get("railway") == "subway_entrance"
                    else "bus_stop" if tags.get("highway") == "bus_stop"
                    else "bus_interchange")
        if el["type"] == "node":
            point = [el["lon"], el["lat"]]
        else:
            ring = [[pt["lon"], pt["lat"]] for pt in el.get("geometry", [])]
            if not ring:
                continue
            point = [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
        transit_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": tags.get("name"), "category": category},
            "geometry": {"type": "Point", "coordinates": point},
        })
    (CACHE_DIR / "sg_transit_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": transit_features}), encoding="utf-8")
    print(f"Wrote sg_transit_full.geojson: {len(transit_features)} points")

    # --- water: simple ways + relation-assembled outer rings ---
    print("\nFetching water relations (Marina Bay etc, untiled)...")
    water_relations = fetch_water_relations()
    water_features = []
    for el in water_ways_by_id.values():
        tags = el.get("tags", {})
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 3:
            continue
        water_features.append({
            "type": "Feature",
            "properties": {"osm_id": el["id"], "name": tags.get("name")},
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        })
    relation_ok = 0
    for rel in water_relations:
        ring = assemble_relation_outer_ring(rel)
        if ring:
            water_features.append({
                "type": "Feature",
                "properties": {"osm_id": rel["id"], "name": rel.get("tags", {}).get("name")},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            })
            relation_ok += 1
    (CACHE_DIR / "sg_water_full.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": water_features}), encoding="utf-8")
    print(f"Wrote sg_water_full.geojson: {len(water_features)} bodies "
          f"({len(water_ways_by_id)} simple ways + {relation_ok}/{len(water_relations)} assembled relations)")


if __name__ == "__main__":
    main()
