"""
Fetch building footprints + heights around a Singapore address.

Pipeline:
  1. Geocode the address via OneMap's free Search API -> SVY21 (X, Y) meters.
  2. Get building footprints: crop from the local whole-island cache
     (cache/sg_buildings_full.geojson, built once by build_island_cache.py)
     if it exists; otherwise fall back to a live OSM Overpass bbox query
     (curl, not urllib -- see LESSONS.md 2026-09-09, urllib inconsistently
     times out/rejects Overpass requests that curl handles fine).
  3. Reproject each footprint's lat/lon nodes to local meters relative to the
     subject point (equirectangular approx, fine at this scale).
  4. Estimate height from OSM's building:levels tag (~3m/storey, override
     with --meters-per-storey). cache/manual_heights.json overrides always
     win over the OSM tag -- use it for any building you know the real
     storey count for (from URA REALIS, a listing, or a site plan) that OSM
     doesn't have tagged, or where OSM's own tag looks wrong.

Usage:
    python fetch_buildings.py "55 Newton Road" --radius 500
Writes:
    cache/<slug>.geojson  -- local buildings, SVY21 + local XY meters, heights
"""
import argparse
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"
CACHE_DIR.mkdir(exist_ok=True)

METERS_PER_STOREY = 3.0


def load_env():
    env_path = HERE / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _onemap_search(query: str) -> list:
    q = urllib.parse.urlencode({
        "searchVal": query,
        "returnGeom": "Y",
        "getAddrDetails": "Y",
        "pageNum": "1",
    })
    url = f"https://www.onemap.gov.sg/api/common/elastic/search?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    return data.get("results") or []


def geocode(address: str) -> dict:
    """OneMap Search API -> first result with SVY21 X/Y + lat/lon.

    OneMap's search returns zero results for a "Street, Singapore 123456"
    string (a normal way to type an address) -- confirmed 2026-09-09, see
    LESSONS.md. Fall back to stripping the postal suffix, then to the bare
    postal code alone (most reliable form) before giving up.
    """
    results = _onemap_search(address)
    used_query = address
    if not results:
        cleaned = re.sub(r",?\s*singapore\s*\d{6}\s*$", "", address, flags=re.I).strip()
        if cleaned and cleaned != address:
            results = _onemap_search(cleaned)
            used_query = cleaned
    if not results:
        m = re.search(r"\b\d{6}\b", address)
        if m:
            results = _onemap_search(m.group())
            used_query = m.group()
    if not results:
        raise SystemExit(f"No geocode match for: {address!r}")
    top = results[0]
    return {
        "address": top.get("ADDRESS"),
        "building": top.get("BUILDING"),
        "x": float(top["X"]),
        "y": float(top["Y"]),
        "lat": float(top["LATITUDE"]),
        "lon": float(top["LONGITUDE"]),
        "all_matches": results,
        "used_query": used_query if used_query != address else None,
    }


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def crop_island_cache(island_path: Path, lat: float, lon: float, radius_m: float) -> list:
    """Return OSM-shaped 'elements' cropped from the whole-island cache."""
    data = json.loads(island_path.read_text(encoding="utf-8"))
    elements = []
    for feat in data["features"]:
        ring = feat["geometry"]["coordinates"][0]  # [[lon, lat], ...]
        # cheap pre-filter: centroid distance from subject point
        clat = sum(pt[1] for pt in ring) / len(ring)
        clon = sum(pt[0] for pt in ring) / len(ring)
        if haversine_m(lat, lon, clat, clon) > radius_m + 100:  # +100m buffer for building extent
            continue
        p = feat["properties"]
        elements.append({
            "type": "way",
            "id": p["osm_id"],
            "geometry": [{"lat": pt[1], "lon": pt[0]} for pt in ring],
            "tags": {
                k: v for k, v in {
                    "name": p.get("name"),
                    "building": p.get("building_type"),
                    "building:levels": str(p["levels"]) if p.get("levels") else None,
                }.items() if v
            } | ({"addr:street": p["addr"]} if p.get("addr") else {}),
        })
    return elements


def load_manual_heights() -> dict:
    """Override chain, highest priority first: hand-typed manual_heights.json
    entries, then hdb_heights.json (official HDB per-block floor data, built
    by build_hdb_heights.py -- not present until that script has been run).
    Merged here so callers only deal with one lookup; each entry keeps its
    own 'source' so resolve_manual_height() reports the real origin."""
    path = CACHE_DIR / "manual_heights.json"
    manual = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"by_osm_id": {}, "by_name": {}}
    manual.setdefault("by_osm_id", {})
    manual.setdefault("by_name", {})
    for entry in manual["by_osm_id"].values():
        entry.setdefault("_source", "manual")
    for entry in manual["by_name"].values():
        entry.setdefault("_source", "manual")

    hdb_path = CACHE_DIR / "hdb_heights.json"
    if hdb_path.exists():
        hdb = json.loads(hdb_path.read_text(encoding="utf-8"))
        for osm_id, entry in hdb.get("by_osm_id", {}).items():
            if osm_id not in manual["by_osm_id"]:  # manual always wins if both exist
                manual["by_osm_id"][osm_id] = {**entry, "_source": "hdb_official"}
    return manual


def resolve_manual_height(manual: dict, osm_id, name, default_mps: float):
    entry = None
    if osm_id is not None and str(osm_id) in manual.get("by_osm_id", {}):
        entry = manual["by_osm_id"][str(osm_id)]
    elif name and name.strip().lower() in {k.lower(): k for k in manual.get("by_name", {})}:
        key = {k.lower(): k for k in manual["by_name"]}[name.strip().lower()]
        entry = manual["by_name"][key]
    if not entry:
        return None
    source = entry.get("_source", "manual")
    if "height_m" in entry:
        return {"height_m": entry["height_m"], "levels": entry.get("levels"), "height_source": source}
    if "levels" in entry:
        mps = entry.get("meters_per_storey", default_mps)
        return {"height_m": entry["levels"] * mps, "levels": entry["levels"], "height_source": source}
    return None


def overpass_buildings(lat: float, lon: float, radius_m: float) -> list:
    """Fetch building ways in a bbox around (lat, lon) via curl (see docstring)."""
    # rough bbox in degrees; 1 deg lat ~= 111,320m, 1 deg lon ~= 111,320*cos(lat)
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    south, north = lat - dlat, lat + dlat
    west, east = lon - dlon, lon + dlon

    query = (
        f'[out:json][timeout:60];'
        f'(way["building"]({south},{west},{north},{east}););'
        f'out body geom;'
    )
    out_file = CACHE_DIR / "_overpass_raw.json"
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
    http_code = result.stdout.strip()
    if http_code != "200":
        raise SystemExit(f"Overpass request failed, HTTP {http_code}: {result.stderr}")

    data = json.loads(out_file.read_text(encoding="utf-8"))
    return data.get("elements", [])


def parse_levels(tags: dict):
    """Best-effort numeric storey count from OSM tags."""
    for key in ("building:levels", "building:levels:aboveground"):
        v = tags.get(key)
        if v:
            m = re.search(r"[\d.]+", v)
            if m:
                return float(m.group())
    return None


def latlon_to_local_xy(lat, lon, origin_lat, origin_lon):
    """Equirectangular projection to local meters, origin at subject point.

    111_320 is meters PER DEGREE (not per radian) -- the degree difference
    must stay in degrees when multiplying by it, only origin_lat's cosine
    term needs radians. Bug found 2026-09-09: wrapping (lat-origin_lat) in
    math.radians() before this multiply made every distance ~57x too small
    (180/pi) -- e.g. Novena Square reported ~4m away instead of the real
    ~230m. See LESSONS.md.
    """
    dlat_deg = lat - origin_lat
    dlon_deg = lon - origin_lon
    x = dlon_deg * 111_320 * math.cos(math.radians(origin_lat))
    y = dlat_deg * 111_320
    return x, y


def build_geojson(elements: list, origin_lat: float, origin_lon: float, manual: dict, meters_per_storey: float) -> dict:
    features = []
    for el in elements:
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        name = tags.get("name")
        levels = parse_levels(tags)
        ring = []
        for pt in el["geometry"]:
            x, y = latlon_to_local_xy(pt["lat"], pt["lon"], origin_lat, origin_lon)
            ring.append([x, y])
        if len(ring) < 3:
            continue

        override = resolve_manual_height(manual, el.get("id"), name, meters_per_storey)
        if override:
            height_m, out_levels, source = override["height_m"], override["levels"], override["height_source"]
        elif levels:
            height_m, out_levels, source = levels * meters_per_storey, levels, "osm_levels"
        else:
            height_m, out_levels, source = None, None, None

        _, _, footprint_area = polygon_centroid_area(ring)
        features.append({
            "type": "Feature",
            "properties": {
                "osm_id": el.get("id"),
                "name": name,
                "building_type": tags.get("building"),
                "levels": out_levels,
                "height_m": height_m,
                "height_source": source,
                "footprint_area_m2": round(footprint_area, 1),
                "addr": " ".join(
                    filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])
                ) or None,
            },
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return {"type": "FeatureCollection", "features": dedupe_overlapping_footprints(features)}


def polygon_centroid_area(ring):
    cx = cy = area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area = abs(area / 2)
    denom = 6 * area or 1
    return cx / denom, cy / denom, area


# Height-plausibility heuristic, added 2026-09-10 (Wesley: "height plausibility
# please" -- chosen over a purely-visual fix). A random SMALL-footprint building
# with no height data is extremely unlikely to be a highrise (landed housing,
# shophouses, small retail/kiosks) -- for those, cap the plausible max height
# and stop treating them as "could theoretically block a 100m+ view" the same
# way a genuinely unknown LARGE building (could be anything, stays honestly
# uncertain) is treated. Confirmed need: Revenue House floor 35 (eye height
# 103.5m) showed 63/72 directions "uncertain" purely because of small nearby
# buildings with no height tag, even though a person who actually works there
# confirmed the real view is "VERY VERY unblocked". This does NOT invent a
# specific height for these buildings -- it only says "definitely too short to
# matter at THIS eye height", so it can rule a building out of the way without
# ever asserting what its real height actually is.
LOW_RISE_BUILDING_TYPES = {
    "house", "detached", "semidetached_house", "terrace", "bungalow",
    "garage", "garages", "shed", "hut", "carport", "roof", "ruins",
}


def plausible_max_height_m(footprint_area_m2, building_type, meters_per_storey=3.0):
    """Conservative upper bound on how tall an unknown-height building could
    plausibly be, from footprint area + building type alone. Returns None if
    the footprint is too large to say anything confident (stays uncertain)."""
    if building_type in LOW_RISE_BUILDING_TYPES:
        return 4 * meters_per_storey  # ~12m -- generous cap for landed housing
    if footprint_area_m2 is None:
        return None
    if footprint_area_m2 < 250:
        return 5 * meters_per_storey  # ~15m -- shophouse / small-structure scale
    if footprint_area_m2 < 600:
        return 8 * meters_per_storey  # ~24m -- larger shophouse rows / mid conservation blocks
    return None  # large footprint could genuinely be anything -- stay honest


def dedupe_overlapping_footprints(features: list) -> list:
    """OSM has a lot of near-duplicate/overlapping building footprints for the
    same real building (confirmed 2026-09-09: 492 overlapping pairs in one
    500m-radius crop). Left unmerged these both render and can corrupt the
    sightline analysis. Cluster by centroid distance <8m + area ratio <2x,
    keep one per cluster, preferring whichever has a known height. Mirrors
    viewer/app.js's dedupeOverlappingFootprints() -- keep both in sync.
    """
    items = []
    for f in features:
        cx, cy, area = polygon_centroid_area(f["geometry"]["coordinates"][0])
        items.append((f, cx, cy, area))
    items.sort(key=lambda it: 0 if it[0]["properties"]["height_m"] else 1)

    kept = []
    for f, cx, cy, area in items:
        is_dup = False
        for _, kcx, kcy, karea in kept:
            dist = math.hypot(kcx - cx, kcy - cy)
            ratio = max(karea, area) / (min(karea, area) or 1)
            if dist < 8 and ratio < 2:
                is_dup = True
                break
        if not is_dup:
            kept.append((f, cx, cy, area))
    return [f for f, _, _, _ in kept]


def slugify(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address", help="Address to geocode, e.g. '55 Newton Road'")
    ap.add_argument("--radius", type=float, default=500, help="Radius in meters (default 500)")
    ap.add_argument("--meters-per-storey", type=float, default=METERS_PER_STOREY,
                     help=f"Floor-to-floor height in meters (default {METERS_PER_STOREY})")
    args = ap.parse_args()

    print(f"Geocoding: {args.address!r}")
    geo = geocode(args.address)
    print(f"  -> {geo['address']}" + (f"  [searched as {geo['used_query']!r}]" if geo.get("used_query") else ""))
    print(f"  SVY21: X={geo['x']:.1f} Y={geo['y']:.1f}  |  lat/lon: {geo['lat']:.6f}, {geo['lon']:.6f}")
    if len(geo["all_matches"]) > 1:
        print(f"  ({len(geo['all_matches'])} matches found for this address, using the first: {geo['building']})")

    island_cache = CACHE_DIR / "sg_buildings_full.geojson"
    if island_cache.exists():
        print(f"Cropping buildings within {args.radius:.0f}m from local whole-island cache...")
        elements = crop_island_cache(island_cache, geo["lat"], geo["lon"], args.radius)
    else:
        print(f"No whole-island cache yet -- fetching OSM buildings within {args.radius:.0f}m live via Overpass...")
        elements = overpass_buildings(geo["lat"], geo["lon"], args.radius)
    print(f"  -> {len(elements)} building ways")

    manual = load_manual_heights()
    geojson = build_geojson(elements, geo["lat"], geo["lon"], manual, args.meters_per_storey)
    with_height = sum(1 for f in geojson["features"] if f["properties"]["height_m"])
    manual_count = sum(1 for f in geojson["features"] if f["properties"]["height_source"] == "manual")
    print(f"  -> {len(geojson['features'])} usable polygons, {with_height} with a known storey count"
          + (f" ({manual_count} from manual overrides)" if manual_count else ""))

    geojson["subject"] = {
        "address": geo["address"],
        "lat": geo["lat"],
        "lon": geo["lon"],
        "svy21_x": geo["x"],
        "svy21_y": geo["y"],
        "radius_m": args.radius,
    }

    slug = slugify(args.address)
    out_path = CACHE_DIR / f"{slug}.geojson"
    out_path.write_text(json.dumps(geojson, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")

    missing = [
        f["properties"]["name"] or f["properties"]["addr"] or f"osm:{f['properties']['osm_id']}"
        for f in geojson["features"] if not f["properties"]["height_m"]
    ]
    if missing:
        print(f"\n{len(missing)} buildings have no storey count (mostly landed housing/small structures).")
        print("If one of these matters for your sightline check, supply its storey count manually")
        print("when running analyze_view.py.")


if __name__ == "__main__":
    main()
