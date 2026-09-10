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

# optional: parks + schools/institutions + malls + expressway ramps (run
# after build_island_layers.py finishes, not at the same time -- see file
# header comment for why)
python build_island_parks_schools.py

# optional but recommended: official HDB per-block floor data, corrects/fills
# gaps in OSM's own height tag for HDB blocks specifically (run any time
# after build_island_cache.py -- doesn't touch Overpass at all)
python build_hdb_heights.py

# serve the viewer -- NOT plain `python -m http.server`, see "Local dev
# server" below for why a custom no-cache server is required
python serve.py 8123
# open http://localhost:8123/viewer/index.html
```

Type an address (or postal code) into the viewer, pick a floor, hit Go. That's
it — geocoding, building lookup, and the blocked/open analysis all run live in
the browser once the island cache exists. No further CLI steps needed for
day-to-day use; the CLI scripts below are for the one-time cache builds, for
scripted/batch use, or for the older per-address `.geojson` file workflow the
viewer's "Load a saved file instead" option still supports.

## Data sources

| Data | Source | Used for |
|---|---|---|
| Address → coordinates | OneMap Search API (`onemap.gov.sg`, free, registered key) | Geocoding. Returns SVY21 directly; converted to local XY meters (see "Coordinate system"). Fails on a full "Street, Singapore 123456" string — both CLI and viewer retry with the postal code stripped, then with just the postal code, before giving up. |
| Building footprints + `building:levels` (storey count) | OpenStreetMap, via the Overpass API (`overpass-api.de`, free, no key) | Every building's outline and (where tagged) storey count — the primary height source. Fetched once for the whole island via `build_island_cache.py`, tiled (see below), then cropped locally per address — no live Overpass calls during normal use. |
| Roads, water, MRT/bus, parks, schools | OpenStreetMap, same Overpass pipeline | Context layers only, not part of the blocked/open analysis — `build_island_layers.py` (roads/water/transit) and `build_island_parks_schools.py` (parks/schools/malls/ramps). |
| HDB block floor counts | data.gov.sg's official "HDB Property Information" dataset | Real per-block storey counts (not OSM tags) for HDB blocks specifically — `build_hdb_heights.py` joins it against the building cache by address, corrects/fills gaps in OSM's own tag. |
| Real building heights for named towers | Manual research (Wikipedia, SkyscraperPage, developer/contractor sites), hand-entered | `cache/manual_heights.json` — always wins over OSM/HDB. Real meters preferred over storeys × 3.0m wherever a sourced figure exists (older/heritage towers especially have taller floor-to-floor heights than the flat 3.0m assumption). |

**No open dataset gives real rooftop height for an arbitrary private building
in Singapore** — SLA's actual high-fidelity 3D city model (OneMap3D) is a
paid/gated product, not the free OneMap API used here; URA zoning-control
datasets (Master Plan, SDCP Building Height Control) are either a restricted
~14,300-feature HDB/landmark subset with no height field at all, or a
max-storeys-by-zone-boundary layer that can't be joined 1:1 against an
individual building. OSM's `building:levels` tag, patchy as it is (~50-60%
coverage near a typical address), turned out to be the best available
general-purpose source — see `LESSONS.md` for the dead ends ruled out before
landing on this.

## How "blocked" / "open" / "uncertain" are decided

The core algorithm (`analyze()` in `analyze_view.py`, ported line-for-line
into `analyzeSightlinesJS()` in `viewer/app.js` — **both must be kept in
sync**, a sign mismatch between them caused a real mirror-image rendering bug
once, see `LESSONS.md`):

1. **Eye height** = `(floor − 1) × 3m + 1.5m` (floor 1 = ground level, 3m per
   storey, 1.5m eye level above the floor you're standing on).
2. **72 rays are cast**, one every 5° around the full compass (0° = N, 90° =
   E, clockwise — `bearing_to_dxdy()` / `bearingToDataDir()`), from the
   subject point outward at that eye height, out to the search radius.
3. For each ray, every OTHER building's footprint polygon is tested for
   intersection (`ray_polygon_min_distance()` — a standard ray/line-segment
   vs polygon-edge test, done in 2D since the model is horizontal-only; the
   subject's own footprint is excluded so it can't obstruct itself). Every
   crossing found gets its distance recorded.
4. Along one ray, the **nearest building whose height is KNOWN and exceeds
   eye height** becomes the `confirmed_blocker`. Separately, the **nearest
   building whose height is UNKNOWN** (no OSM tag, no manual override, and
   not ruled out by the height-plausibility heuristic below) becomes the
   `uncertain_blocker`. Both are tracked independently, then reconciled:
   - **Blocked** (red): a `confirmed_blocker` exists anywhere within radius.
     Reports the nearest one. If a nearer `uncertain_blocker` also exists on
     the same ray, a note flags it as a possible even-sooner obstruction —
     but the direction stays "blocked," not downgraded to uncertain, since
     the confirmed blocker guarantees the view isn't open regardless of what
     the closer unknown building turns out to be.
   - **Uncertain** (yellow): no `confirmed_blocker` anywhere on the ray, but
     an `uncertain_blocker` exists. Can't say for sure without knowing its
     real height (add it to `manual_heights.json` to resolve).
   - **Open** (green): neither exists within the search radius.
5. **Height-plausibility heuristic**: an unknown-height building with a
   small footprint (`plausible_max_height_m()`, keyed off footprint area +
   OSM building type) is very unlikely to reach a high eye level — if its
   plausible max height is still below eye level, it's ruled out as an
   obstruction candidate entirely rather than left "uncertain" forever. Only
   applies to small footprints; a large footprint stays genuinely
   ambiguous (could be a real supertall, could be a low-rise podium with a
   big footprint — this class of case needs individual verification, see
   the MBFC podium entry in `LESSONS.md`).
6. `distance_m` on every direction (shown when you click a ray) is the
   distance to the first blocker for blocked/uncertain rays, and the search
   radius itself for open rays (a floor, not a measured distance — nothing
   farther out was ever checked) so every sightline reports a usable number
   instead of `null`.

This is a **horizontal-only** model — a flat line at eye height going
straight out, not an angled line-of-sight to a specific target — and it
assumes **flat terrain** across the search area (fine for Singapore).

### "Ignore obstructions beyond X" toggle

A building far enough away no longer really reads as "blocking" the view —
you can see around/past it, it's background rather than something in your
face. The HUD has a checkbox + distance input for this: when on, any
building beyond that distance stops counting as a blocker at all (even if
it's technically taller than eye level), and the direction falls through to
open/uncertain based on whatever's closer. Off by default (matches the
original always-honest behavior). Same cutoff logic lives in
`analyze_view.py` (`--max-obstruction-distance`) and
`facade_blockage_report.py` for the CLI/CSV paths — see `analyze()`'s
`max_obstruction_dist` param if changing the rule.

## What you see in the viewer

**Buildings**: solid extruded blocks. Blue = known height (real OSM tag, HDB
official data, or a manual override). Grey/translucent = unknown height
(rendered at a flat 6m placeholder so it's still visible, but that's not a
real height — don't read anything into how tall it looks). Orange = the
subject building itself (the one containing the geocoded point), rendered at
least as tall as the floor you're actually checking so the eye-height marker
never floats above its own roof. Click any building for its name and height
in the info panel (bottom-left); an unnamed building shows as `osm:<id>` —
that id is what you'd use in `manual_heights.json`'s `by_osm_id`.

**Rays** (the compass lines from the subject point): one per 5° bearing, 72
total, colored by verdict — green = open, red = blocked, amber = uncertain.
Length = `distance_m` (see above: the real blocker distance for red/amber,
the search radius for green). Click a ray for its exact bearing, compass
label, status, blocker name/height, and distance. Rays use standard GPU
depth-testing (not rendered on top of everything) so a ray visually stops the
instant it hits solid geometry, like a real sightline would — the trade-off,
accepted deliberately, is that an open ray can be hidden behind some
*unrelated* foreground building from an oblique camera angle even though it's
not what that ray is actually about; re-orbit to check if a ray looks oddly
truncated.

**`% blocked per side` panel** (top-right, below the legend): the same
floor × N/E/S/W breakdown as `facade_blockage_report.py`, computed for the
floor currently loaded and shown directly in the viewer — buildings are
treated as sitting in a simple 4-sided box (N = bearing 315-45°, E = 45-135°,
S = 135-225°, W = 225-315°), not their real facade angles. Good enough for a
"which side of the building is worse" summary; use the per-ray click if you
need one specific bearing's exact verdict.

**Compass** (bottom-right, next to the load status): a small rotating N/S
needle so orientation is readable without guessing from the grid — recomputed
every rendered frame from the camera's actual current view (projects the
world-space north direction through the live camera matrices), so it stays
correct through any orbit/pan/zoom rather than assuming a fixed "north is up"
relationship that only held at the default angle.

**Context layers** (roads, water, MRT/bus stops, parks, schools): rendered
for situational awareness only, not part of the blocked/open analysis itself.
Road width comes from OSM's own lane/width tags where present. Water bodies
are OSM *relations* (multipolygons) — outer-ring assembly only (drops
holes/islands within the body), fine for "there's water here," not surveyed
accuracy.

## Rendering technique

Plain three.js (r128, loaded from CDN, no build step) — a static HTML page
plus one JS file, no framework.

- **Buildings**: each footprint polygon becomes a `THREE.Shape`, extruded to
  its height via `THREE.ExtrudeGeometry`, then `rotateX(-90°)` to map
  shape-space (u = data x, v = data y, extrude = height) into the scene's
  world axes.
- **Roads**: a custom ribbon geometry (two parallel offset lines per road
  centerline, width from OSM tags) rather than a flat `THREE.Line`, so roads
  actually read as roads at a normal camera distance instead of one-pixel
  hairlines.
- **Rays**: `THREE.BufferGeometry` line segments (`LineBasicMaterial`,
  default depth-testing — see "What you see" above for why that was chosen
  over a depth-test override after trying both).
- **Interaction**: `THREE.Raycaster` against the actual rendered meshes
  (buildings, rays, transit points) on mouse move (hover highlight) and click
  (info panel) — not a separate hand-written hit-test. The ray-picking
  threshold (`raycaster.params.Line.threshold`, how close your cursor needs
  to be to register a hit on a thin line) scales with the search radius
  (`setRayPickThreshold()`) — a fixed world-space tolerance would otherwise
  subtend a much smaller on-screen target for a long ray far from camera than
  a short one close by, making distant open rays effectively unclickable.
- **Camera / fog scaling**: initial camera distance (`setCameraForRadius()`)
  and fog near/far (`setFogRange()`) both scale proportionally with the
  search radius — a value tuned for the original fixed 500m default silently
  broke (buildings zoomed-in-tight, or faded into fog before ever being
  visible) once radius became user-adjustable. Same class of fix applied
  three times this project (camera, fog, ray-pick threshold) — see
  `LESSONS.md` if adding another radius-dependent constant.
- **Compass**: not a 3D object in the scene — a 2D CSS element rotated every
  animation frame by projecting a world-space north point through the
  camera's real projection matrix and converting the resulting screen-space
  delta into a CSS `rotate()` angle (`updateCompass()`).
- **Classification vs. rendering are two separate code paths** that must
  agree: `analyzeSightlinesJS()` decides blocked/open/uncertain using 2D
  ray-polygon math (fast, no GPU involved), while `drawViewReport()` draws
  the resulting verdict as a 3D line. A bearing → direction-vector sign
  mismatch between the two once caused every ray to render in the mirror-
  image direction from what it was actually classified for — both now share
  one function (`bearingToDataDir()` / `bearing_to_dxdy()`) specifically to
  prevent that class of bug recurring.

## Local dev server

**Always run `python serve.py [port]`, never plain `python -m http.server`.**
Two real bugs shipped from using the plain server, both in `LESSONS.md`:

1. With no `Cache-Control` header at all, the browser applies its own
   heuristic caching and can silently serve a stale copy of `app.js` or a
   `cache/*.json` data file for an entire session (or across sessions) after
   an edit, with no error — this cost real debugging time more than once
   before the cause was found.
2. The fix isn't "just add `Cache-Control: no-store` everywhere" either —
   that forces a full re-transfer on *every single load* with zero reuse,
   which for `cache/sg_buildings_full.geojson` (~89MB) +
   `cache/sg_roads_full.geojson` (~47MB) meant every page load re-downloading
   ~140MB+, and briefly caused the dev server to hang outright (a
   single-threaded server serializing 6 parallel data-file fetches).

`serve.py` gets both right: `ThreadingHTTPServer` so concurrent requests
don't serialize, and `Cache-Control: no-cache, must-revalidate` (not
`no-store`) so the browser always revalidates with the server before
trusting anything cached, but a genuinely-unchanged file comes back as a
tiny `304 Not Modified` instead of a full re-transfer (Python's
`http.server` already answers `If-Modified-Since` correctly, no extra code
needed for that half). A full reload is ~4s once the island cache exists.

## Known data limitations

(See "Data sources" above for the general height-coverage gap and why OSM's
`building:levels` tag ended up as the primary source. This section covers
narrower data-quality gotchas worth knowing if you ever touch the fetch/build
scripts directly.)

- **HDB height-join specifics**: `build_hdb_heights.py` — confirmed
  2026-09-10 run: 10,994 building matches, 2,807 filled a real gap where OSM
  had no `building:levels` tag at all, 675 actually *corrected* a
  disagreeing OSM tag. Address matching is a best-effort text join (street
  abbreviations normalized both ways), not guaranteed 100% — check
  `height_source` on a building (`"hdb_official"` vs `"osm_levels"` vs
  `"manual"`) if you need to know which source a given height came from.
- **OSM has a lot of near-duplicate/overlapping building footprints** for the
  same real building (492 found in one 500m-radius test crop). Both
  `fetch_buildings.py` and the viewer's client-side crop dedupe these
  automatically (cluster by centroid distance <8m + area ratio <2x, keep
  whichever copy has a known height) — but it's worth knowing this exists in
  the raw data if you ever query OSM directly for this project.
- **A multi-tower complex's geocoded address can land inside a low
  podium/base footprint** instead of the actual tower meant (e.g. "One
  Raffles Place" matched a 6-storey base, not either real tower) — the
  subject-detection point-in-polygon check has no way to know a footprint is
  a low base rather than the tower the address nominally refers to. No
  structural fix yet; `facade_blockage_report.py --max-floor N` works around
  it for that report specifically. Same underlying OSM pattern caused the
  MBFC podium-as-obstruction bug (fixed) — see `LESSONS.md` if this comes up
  again for a different complex.

## Architecture

```
fetch_buildings.py       CLI: geocode an address, crop the island building
                          cache (or live-fetch via Overpass if no cache yet),
                          apply manual height overrides, write
                          cache/<slug>.geojson
analyze_view.py           CLI: run the blocked/open/uncertain analysis on a
                          fetched .geojson, write cache/<slug>_floor<N>_view.json
facade_blockage_report.py CLI: floor x side (N/E/S/W) % blocked summary table
                          across every floor of the subject building -- the
                          "boss can read this in 10 seconds" report. Reuses
                          analyze_view.py's analyze() unchanged, just
                          aggregates its output differently. Writes
                          cache/<slug>_facade_blockage.csv
poi_distances.py          CLI: straight-line (not walking) distance from the
                          subject point to the nearest MRT station/entrance,
                          bus stop/interchange, mall, school, and park
fetch_roads.py             CLI: per-address road fetch (superseded by
                          build_island_layers.py's whole-island roads layer,
                          kept for one-off/scripted use)
serve.py                  Local dev server -- ThreadingHTTPServer +
                          Cache-Control: no-cache, must-revalidate. See
                          "Local dev server" above; always use this, never
                          plain `python -m http.server`.
build_island_cache.py      One-time: tile the whole island into a grid of
                          Overpass queries, build cache/sg_buildings_full.geojson
build_island_layers.py     One-time: same tiling approach for roads, MRT/bus
                          points, and water bodies -> cache/sg_roads_full.geojson,
                          sg_transit_full.geojson, sg_water_full.geojson
build_island_parks_schools.py  One-time: same again for parks and educational
                          institutions -> cache/sg_parks_full.geojson,
                          sg_schools_full.geojson. Separate script/tile cache
                          from build_island_layers.py on purpose — added after
                          that build was already mostly done, and running two
                          heavy tiled Overpass fetches at once just compounds
                          the rate-limiting both already have to fight.
viewer/index.html + app.js  The actual tool. three.js scene, live client-side
                          geocode + crop + sightline analysis (ports the same
                          logic as analyze_view.py — keep both in sync if you
                          change the classification rule). Click a building
                          for name/height, click a ray for its verdict. See
                          "What you see in the viewer" / "Rendering
                          technique" above for the full breakdown.
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

Match by OSM id (reliable — click the building in the viewer; an unnamed one
shows as `osm:<id>` in the info panel) or by name (case-insensitive, must
match OSM's `name` tag). Use
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
