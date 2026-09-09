# SG View Analysis

Given a Singapore address and a floor number, this tells you which compass
directions have a blocked or open view, by what building, at what distance —
and renders the surrounding area in 3D so you can look around and click
anything for details. Built for property-job use (checking a unit's view
before a listing/client conversation), not a general 3D modeling tool.

## Quick start

```bash
# one-time: build the whole-island building cache (~15-30 min, resumable)
python build_island_cache.py

# optional: same for roads/water/MRT/bus stops context layers
python build_island_layers.py

# serve the viewer
python -m http.server 8123 --directory .
# open http://localhost:8123/viewer/index.html
```

Type an address (or postal code) into the viewer, pick a floor, hit Go. That's
it — geocoding, building lookup, and the blocked/open analysis all run live in
the browser once the island cache exists. No further CLI steps needed for
day-to-day use; the CLI scripts below are for the one-time cache builds, for
scripted/batch use, or for the older per-address `.geojson` file workflow the
viewer's "Load a saved file instead" option still supports.

## How "blocked" / "open" / "uncertain" are decided

- **Eye height** = `(floor − 1) × 3m + 1.5m` (floor 1 = ground level).
- 72 rays are cast, one every 5° around the full compass, from the subject
  point at eye height.
- **Blocked** (red): a building with a *known* height taller than eye level
  sits somewhere on that ray within the search radius. Reports the nearest
  one. If a *shorter, unknown-height* building sits even closer, a note flags
  it as a possible even-sooner obstruction — but the direction is still
  "blocked," not downgraded to uncertain, since the known-tall building
  guarantees it either way.
- **Open** (green): nothing at all — known-tall or unknown-height — sits on
  that ray within the radius.
- **Uncertain** (yellow): no confirmed blocker on that ray, but an
  unknown-height building is closer than the search radius. Can't say for
  sure without knowing its real height (add it to `manual_heights.json`).

This is a **horizontal-only** model — a flat line at eye height going
straight out, not an angled line-of-sight to a specific target — and it
assumes **flat terrain** across the search area (fine for Singapore).

## Known data limitations

- **No open dataset gives real rooftop height for arbitrary private
  buildings in Singapore.** Height comes from OpenStreetMap's
  `building:levels` tag (storey count) × 3m/storey. Coverage is decent in
  built-up areas (~50-60% of buildings near a typical address) but far from
  complete — buildings without the tag show as "unknown height" and can only
  be resolved by adding them to `cache/manual_heights.json`.
- **OSM has a lot of near-duplicate/overlapping building footprints** for the
  same real building (492 found in one 500m-radius test crop). Both
  `fetch_buildings.py` and the viewer's client-side crop dedupe these
  automatically (cluster by centroid distance <8m + area ratio <2x, keep
  whichever copy has a known height) — but it's worth knowing this exists in
  the raw data if you ever query OSM directly for this project.
- **Water bodies** (Marina Bay, Singapore River, etc.) are OSM *relations*
  (multipolygons), not simple shapes — `build_island_layers.py` does basic
  outer-ring assembly (drops holes/islands within the water body) which is
  good enough for "there's water here" context, not surveyed accuracy.
- Geocoding (OneMap's Search API) fails on a full "Street, Singapore 123456"
  string — both the CLI and viewer automatically retry with the postal code
  stripped, then with just the postal code alone, before giving up.

## Architecture

```
fetch_buildings.py       CLI: geocode an address, crop the island building
                          cache (or live-fetch via Overpass if no cache yet),
                          apply manual height overrides, write
                          cache/<slug>.geojson
analyze_view.py           CLI: run the blocked/open/uncertain analysis on a
                          fetched .geojson, write cache/<slug>_floor<N>_view.json
fetch_roads.py             CLI: per-address road fetch (superseded by
                          build_island_layers.py's whole-island roads layer,
                          kept for one-off/scripted use)
build_island_cache.py      One-time: tile the whole island into a grid of
                          Overpass queries, build cache/sg_buildings_full.geojson
build_island_layers.py     One-time: same tiling approach for roads, MRT/bus
                          points, and water bodies -> cache/sg_roads_full.geojson,
                          sg_transit_full.geojson, sg_water_full.geojson
viewer/index.html + app.js  The actual tool. three.js scene, live client-side
                          geocode + crop + sightline analysis (ports the same
                          logic as analyze_view.py — keep both in sync if you
                          change the classification rule), click a facade for
                          its bearing, click a ray for its verdict.
cache/manual_heights.json  Hand-maintained height overrides, by OSM id or
                          building name. Always wins over the OSM tag.
cache/stack_maps/          Reserved for a future per-project unit→facade
                          mapping feature (not built yet — see "Future work").
```

### Why the island-wide caches, and why tiled

A live query to Overpass (the free OSM query API) for the whole island times
out — it's built for small ad-hoc queries, not bulk country extraction — so
both `build_island_cache.py` and `build_island_layers.py` tile Singapore into
a grid of small bbox queries, run sequentially with backoff and mirror
rotation (Overpass will rate-limit / temporarily block an IP that goes too
fast), and merge the results into one static local file. This is a one-time
cost (tens of minutes); after that, the viewer crops from the local file
client-side for every address, with no further network calls to Overpass.

Both builder scripts are **resumable** — re-running skips tiles already
successfully fetched (validated by content, not just file existence) and only
retries what's missing or came back as an error page.

### Coordinate system

Everything works in local XY meters relative to the subject address (not
lat/lon, not SVY21 directly) — `x` = meters east, `y` = meters north. The
viewer's three.js scene maps this to `world X = data x`, `world Y = height`,
`world Z = -data y` (north points toward -Z). See the comment block at the
top of `viewer/app.js` if extending the 3D code.

**If you ever touch the lat/lon → local-meters conversion:** 111,320 is
meters *per degree*, not per radian — do NOT wrap a degree difference in
`math.radians()`/`* Math.PI/180` before that multiply. This exact bug shipped
once (every distance came out ~57x too small) and is documented in
`LESSONS.md`. Test any change against one real, independently-known distance
before trusting it.

## Manual height overrides

Edit `cache/manual_heights.json`:

```json
{
  "by_osm_id": { "172529140": { "levels": 18 } },
  "by_name": { "Some Building Name": { "height_m": 45 } }
}
```

Match by OSM id (reliable, get it by clicking the building's facade in the
viewer) or by name (case-insensitive, must match OSM's `name` tag). Use
`levels` (× the meters-per-storey default, override with
`meters_per_storey` in the same entry) or `height_m` directly.

## Future work (planned, not built)

- **Per-project stack maps**: annotate a building once (click each facade,
  type in the unit/stack numbers on it), save to `cache/stack_maps/`, so a
  future lookup for that building resolves a unit number straight to its
  facade instead of re-clicking. Deferred until the core pipeline (this
  README's contents) was validated against real addresses.

## See also

- `../LESSONS.md` — every bug found and fixed while building this, with root
  causes and the reasoning behind each fix. Worth reading before making
  further changes to the geometry/analysis code, several non-obvious gotchas
  are recorded there (the coordinate bug above is one of ~6).
