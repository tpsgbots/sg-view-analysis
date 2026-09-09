"""
One-time (re-runnable) bulk build of a whole-Singapore OSM building cache.

A single Overpass query for the whole island (even just a COUNT) times out --
confirmed 2026-09-09, see LESSONS.md. Overpass is meant for small ad-hoc
queries, not bulk country extraction, so this tiles the island into a grid of
small bbox queries (cheap, no polygon-clip cost) run sequentially, dedupes
building ways that appear in more than one tile (shared boundary nodes), and
merges everything into one local GeoJSON cache.

Output stays in lat/lon (not local XY) since it's a general-purpose base
cache -- fetch_buildings.py reprojects to local meters per-address at query
time.

Usage:
    python build_island_cache.py
Writes:
    cache/sg_buildings_full.geojson
    cache/_tiles/ (raw per-tile responses, kept for resuming a partial run)
"""
import functools
import json
import subprocess
import sys
import time
from pathlib import Path

print = functools.partial(print, flush=True)  # background/redirected stdout buffers otherwise -- see LESSONS.md 2026-09-09

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
TILE_DIR = CACHE_DIR / "_tiles"
TILE_DIR.mkdir(parents=True, exist_ok=True)

# Singapore mainland + immediate islands, generous bbox
SOUTH, NORTH = 1.14, 1.48
WEST, EAST = 103.59, 104.05
TILE_DEG = 0.06  # ~6.6km squares -- small enough to avoid Overpass timeouts

# Rotate mirrors on failure -- overpass-api.de flat-out refused connections
# (HTTP 000) for the northern half of the island in the first run, 2026-09-09,
# and a same-server retry with backoff alone made zero progress.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


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
    """A previously-saved tile only counts as done if it's real Overpass JSON,
    not an HTML error page (rate-limit/busy responses land here with HTTP 200
    written to -o in some cases, or a stale file from a prior failed attempt)."""
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
        f'(way["building"]({south},{west},{north},{east}););'
        f'out body geom;'
    )
    attempt = 0
    for mirror in OVERPASS_MIRRORS:
        for local_attempt in range(1, retries_per_mirror + 1):
            attempt += 1
            result = subprocess.run(
                [
                    "curl", "-s", "-m", "100", "-X", "POST", mirror,
                    "--data-urlencode", f"data={query}",
                    "-o", str(tile_path),
                    "-w", "%{http_code}",
                ],
                capture_output=True, text=True,
            )
            code = result.stdout.strip()
            if code == "200" and tile_is_valid(tile_path):
                return "ok" if mirror == OVERPASS_MIRRORS[0] else f"ok (via {mirror.split('/')[2]})"
            backoff = 15 * local_attempt  # 15s, 30s -- Overpass rate-limits (429) escalate to a full block (000/504) if hit too fast, confirmed 2026-09-09
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
                print(f"    {consecutive_failures} tiles in a row have failed -- likely still rate-limited/blocked. "
                      f"Pausing 3 minutes before continuing instead of burning through the rest of the run.")
                time.sleep(180)
                consecutive_failures = 0
        else:
            consecutive_failures = 0
        if status != "cached":
            time.sleep(6)  # be politer to the shared public Overpass instance than the 1s that got us rate-limited

    print("\nMerging tiles...")
    by_id = {}
    bad_tiles = 0
    for tile_path in sorted(TILE_DIR.glob("tile_*.json")):
        try:
            data = json.loads(tile_path.read_text(encoding="utf-8"))
        except Exception:
            bad_tiles += 1
            continue
        for el in data.get("elements", []):
            if el.get("type") == "way" and "geometry" in el:
                by_id[el["id"]] = el

    print(f"{len(by_id)} unique building ways across {len(list(TILE_DIR.glob('tile_*.json')))} tiles ({bad_tiles} unreadable)")

    features = []
    for el in by_id.values():
        tags = el.get("tags", {})
        ring = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(ring) < 3:
            continue
        levels = None
        for key in ("building:levels", "building:levels:aboveground"):
            v = tags.get(key)
            if v:
                import re
                m = re.search(r"[\d.]+", v)
                if m:
                    levels = float(m.group())
                    break
        features.append({
            "type": "Feature",
            "properties": {
                "osm_id": el["id"],
                "name": tags.get("name"),
                "building_type": tags.get("building"),
                "levels": levels,
                "height_m": (levels * 3.0) if levels else None,
                "addr": " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])) or None,
            },
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })

    out = {"type": "FeatureCollection", "features": features}
    out_path = CACHE_DIR / "sg_buildings_full.geojson"
    out_path.write_text(json.dumps(out), encoding="utf-8")
    with_height = sum(1 for f in features if f["properties"]["height_m"])
    print(f"\nWrote {out_path}")
    print(f"{len(features)} total buildings, {with_height} ({100*with_height/len(features):.0f}%) with a known storey count")


if __name__ == "__main__":
    main()
