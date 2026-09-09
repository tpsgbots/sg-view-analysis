"""
Join HDB's official "HDB Property Information" dataset (data.gov.sg,
d_17f5382f26140b1fdae0ba2ef6239d2f -- real ACTUAL floor count per block,
`max_floor_lvl`, not zoning speculation) against the OSM building cache by
address, to fill height gaps and correct OSM's own building:levels tag where
they disagree for HDB blocks specifically.

Confirmed 2026-09-10: 10,994 buildings in the island cache match an HDB
record by (block number, normalized street name); 2,807 of those had NO
height data at all beforehand (pure OSM `building:levels` coverage gap this
fills), the rest confirm/can correct what OSM already had.

This does NOT touch Overpass/OSM at all -- separate API, no rate-limit
interaction with build_island_cache.py / build_island_layers.py.

Street-name matching needs normalization since the two sources abbreviate
differently (HDB: "CLEMENTI WEST ST 1", OSM addr:street: "Clementi West
Street 1") -- see normalize_street() for the abbreviation table. This is a
best-effort text join, not guaranteed 100% -- unmatched HDB blocks are simply
not applied, not treated as an error.

Usage:
    python build_hdb_heights.py
Writes:
    cache/hdb_heights.json -- {"by_osm_id": {"<id>": {"levels": N, "source": "hdb_official"}}}
Reads:
    cache/sg_buildings_full.geojson (must exist -- run build_island_cache.py first)
"""
import csv
import json
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE_DIR = HERE / "cache"

HDB_DATASET_ID = "d_17f5382f26140b1fdae0ba2ef6239d2f"

STREET_ABBREV = {
    "RD": "ROAD", "AVE": "AVENUE", "AV": "AVENUE", "ST": "STREET", "DR": "DRIVE",
    "CRES": "CRESCENT", "LOR": "LORONG", "JLN": "JALAN", "BLVD": "BOULEVARD",
    "TER": "TERRACE", "CL": "CLOSE", "PK": "PARK", "PL": "PLACE", "WLK": "WALK",
    "GDNS": "GARDENS", "HTS": "HEIGHTS", "CTRL": "CENTRAL", "NTH": "NORTH",
    "STH": "SOUTH", "UPP": "UPPER", "SQ": "SQUARE", "GRN": "GREEN", "GDN": "GARDEN",
    "BT": "BUKIT",
}


def normalize_street(s: str) -> str:
    s = s.upper().strip()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(STREET_ABBREV.get(tok, tok) for tok in s.split())


def download_hdb_csv() -> Path:
    out_path = CACHE_DIR / "_hdb_property_info.csv"
    req = urllib.request.Request(
        f"https://api-open.data.gov.sg/v1/public/api/datasets/{HDB_DATASET_ID}/poll-download",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        url = json.load(r)["data"]["url"]
    urllib.request.urlretrieve(url, out_path)
    return out_path


def load_hdb_lookup(csv_path: Path) -> dict:
    lookup = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("max_floor_lvl"):
                continue
            key = (row["blk_no"].strip().upper(), normalize_street(row["street"]))
            lookup[key] = int(row["max_floor_lvl"])
    return lookup


def main():
    island_cache = CACHE_DIR / "sg_buildings_full.geojson"
    if not island_cache.exists():
        raise SystemExit("cache/sg_buildings_full.geojson not found -- run build_island_cache.py first")

    print("Downloading HDB Property Information dataset...")
    csv_path = download_hdb_csv()
    hdb = load_hdb_lookup(csv_path)
    print(f"  -> {len(hdb)} HDB blocks with a real floor count")

    print("Joining against the building cache by address...")
    data = json.loads(island_cache.read_text(encoding="utf-8"))
    by_osm_id = {}
    matched, filled_gap, corrected = 0, 0, 0
    for f in data["features"]:
        addr = f["properties"].get("addr")
        if not addr:
            continue
        m = re.match(r"^(\S+)\s+(.+)$", addr)
        if not m:
            continue
        key = (m.group(1).upper(), normalize_street(m.group(2)))
        levels = hdb.get(key)
        if not levels:
            continue
        matched += 1
        existing = f["properties"].get("levels")
        if not existing:
            filled_gap += 1
        elif existing != levels:
            corrected += 1
        by_osm_id[str(f["properties"]["osm_id"])] = {"levels": levels, "source": "hdb_official"}

    out = {"by_osm_id": by_osm_id}
    (CACHE_DIR / "hdb_heights.json").write_text(json.dumps(out), encoding="utf-8")
    print(f"Wrote cache/hdb_heights.json: {matched} matched buildings "
          f"({filled_gap} filled a real gap, {corrected} corrected a disagreeing OSM tag)")


if __name__ == "__main__":
    main()
