/*
 * Axis convention: world X = data east (x), world Y = height (up),
 * world Z = -data north (y) -- i.e. north points toward -Z.
 * A building's extruded geometry is built in shape-space (u=data x, v=data y,
 * extrude along local Z) then rotated -90deg about X, which maps local Z
 * (extrude/height) -> world Y and local Y (data y/north) -> world -Z.
 * Facade bearing = atan2(nx, ny) in degrees where (nx,ny) is the outward
 * normal in DATA space (x=east,y=north) -- matches analyze_view.py's
 * bearing_to_dxdy() convention (0=N, 90=E, clockwise) so bearings read here
 * plug directly into `python analyze_view.py ... --bearing <n>`.
 */

const COLOR_KNOWN = 0x5b8fd6;
const COLOR_UNKNOWN = 0x4a5468;
const COLOR_SUBJECT = 0xe0a030;
const COLOR_OPEN = 0x3ddc6f;
const COLOR_BLOCKED = 0xe5484d;
const COLOR_UNCERTAIN = 0xe0b030;

let scene, camera, renderer, controls, raycaster, mouse;
let buildingGroup = [];
let rayGroup = [];
let transitGroup = [];
let hovered = null;
let currentData = null;

function init3D() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0e1420);
  scene.fog = new THREE.Fog(0x0e1420, 300, 900); // updated per-load by setFogRange() -- see goToAddress/loadScene

  camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 3000);
  camera.position.set(120, 160, 260);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(window.innerWidth, window.innerHeight);
  document.getElementById('canvas-wrap').appendChild(renderer.domElement);

  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 20, 0);
  controls.update();

  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const sun = new THREE.DirectionalLight(0xffffff, 0.7);
  sun.position.set(200, 400, 150);
  scene.add(sun);

  const grid = new THREE.GridHelper(1200, 60, 0x2c3752, 0x1c2536);
  scene.add(grid);

  raycaster = new THREE.Raycaster();
  raycaster.params.Line.threshold = 2.5; // world meters -- thin lines are hard to hit exactly otherwise
  mouse = new THREE.Vector2();

  window.addEventListener('resize', onResize);
  renderer.domElement.addEventListener('mousemove', onMouseMove);
  renderer.domElement.addEventListener('click', onClick);
  renderer.domElement.addEventListener('mouseleave', () => {
    // without this, a hover highlight can stick if the pointer leaves the
    // canvas without a final mousemove landing on empty space first
    if (hovered) {
      unhighlight(hovered);
      hovered = null;
    }
  });

  animate();
}

// Fog was hardcoded to fade fully out by 900m -- fine for the original 500m
// default radius, but once radius became user-adjustable a 1000m+ search
// would fade real, correctly-loaded buildings (e.g. Marina Bay Sands at
// ~780-950m from MBFC) almost entirely into the background before you'd
// ever see them. Confirmed 2026-09-09: MBS was present in currentData with
// the right position/height, just invisible due to fog. Scale fog range
// with the actual search radius instead of a fixed value.
function setFogRange(radiusM) {
  scene.fog.near = radiusM * 0.5;
  scene.fog.far = radiusM * 1.6;
}

// Same class of bug as fog: the initial camera position was tuned for the
// original fixed 500m default and doesn't scale with a user-chosen radius --
// at 1000m it starts zoomed in tight against adjacent buildings instead of
// showing the whole loaded scene. Scale proportionally to the 500m baseline.
function setCameraForRadius(radiusM) {
  const scale = radiusM / 500;
  camera.position.set(120 * scale, 160 * scale, 260 * scale);
  camera.far = Math.max(3000, radiusM * 4);
  camera.updateProjectionMatrix();
}

// Ray click-picking uses a fixed WORLD-space threshold (see init3D), so the
// same tolerance subtends a much smaller on-screen target for a long ray far
// from camera than a short one close by -- open rays reach all the way to
// radiusM while blocked/uncertain rays usually stop much closer, so open
// rays were effectively unclickable at default radii. Scale the threshold
// with radius so pick tolerance stays proportional to how far rays reach.
function setRayPickThreshold(radiusM) {
  raycaster.params.Line.threshold = Math.max(2.5, radiusM * 0.015);
}

function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

function clearScene() {
  for (const obj of [...scene.children]) {
    if (obj.userData.isSceneContent) scene.remove(obj);
  }
  buildingGroup = [];
  rayGroup = [];
  transitGroup = [];
  hovered = null;
}

function dataToWorld(x, y, z) {
  return new THREE.Vector3(x, z || 0, -y);
}

// Single source of truth for bearing -> data-space direction (0=N, 90=E,
// clockwise), matching analyze_view.py's bearing_to_dxdy() exactly. Used by
// BOTH the classifier (analyzeSightlinesJS) and the renderer (drawViewReport)
// -- these must never be two separately-typed formulas again. A north-south
// sign mismatch between them (drawViewReport had a stray negation) caused
// every ray to render in the mirror-image direction from what it was
// actually classified for, for this whole session, until caught via a real
// mesh-raycast check. See LESSONS.md 2026-09-10.
function bearingToDataDir(bearingDeg) {
  const rad = bearingDeg * Math.PI / 180;
  return [Math.sin(rad), Math.cos(rad)];
}

function buildBuilding(feature, isSubject, subjectMinHeight) {
  const ring = feature.geometry.coordinates[0];
  const p = feature.properties;
  // Unknown-height fallback: a plain 6m default for regular buildings, but the
  // SUBJECT building must render at least as tall as the floor actually being
  // checked -- otherwise the eye-height marker/rays float visibly above the
  // building's own rendered roof whenever checking a floor higher than the old
  // hardcoded 30m fallback (confirmed 2026-09-10, Wesley: "something is
  // terribly wrong" on a screenshot showing exactly this).
  const height = p.height_m || (isSubject ? Math.max(30, subjectMinHeight || 0) : 6);
  const isKnown = !!p.height_m;

  const shape = new THREE.Shape();
  ring.forEach(([x, y], i) => (i === 0 ? shape.moveTo(x, y) : shape.lineTo(x, y)));

  const geo = new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false });
  geo.rotateX(-Math.PI / 2);

  const color = isSubject ? COLOR_SUBJECT : (isKnown ? COLOR_KNOWN : COLOR_UNKNOWN);
  const mat = new THREE.MeshStandardMaterial({
    color, transparent: !isKnown && !isSubject, opacity: isKnown || isSubject ? 1 : 0.55,
  });
  // Simplified 2026-09-10 (Wesley: rays already answer blocked/open, no need
  // to click a wall for bearing) -- the whole solid mesh is now the click
  // target itself (name + height), instead of one extra invisible picking
  // plane per polygon edge. That per-edge-plane approach was also the
  // dominant cause of the RAM blowup at large radii (~6-7 extra three.js
  // objects per building) -- removing it fixes both at once. Bearing
  // computation (outwardNormal/bearingFromNormal) is no longer used for
  // rendering but stays available for analyze_view.py's --bearing flag users.
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData = {
    isSceneContent: true, isBuilding: true,
    buildingName: p.name || p.addr || `osm:${p.osm_id}`,
    heightKnown: isKnown, heightM: p.height_m,
  };
  scene.add(mesh);
  buildingGroup.push(mesh);
}

function buildSubjectMarker(floor, metersPerStorey) {
  const eyeHeight = (floor - 1) * metersPerStorey + 1.5;
  const poleGeo = new THREE.CylinderGeometry(0.4, 0.4, eyeHeight, 8);
  const poleMat = new THREE.MeshStandardMaterial({ color: 0xffdd55 });
  const pole = new THREE.Mesh(poleGeo, poleMat);
  pole.position.set(0, eyeHeight / 2, 0);
  pole.userData.isSceneContent = true;
  scene.add(pole);

  const ballGeo = new THREE.SphereGeometry(1.5, 16, 16);
  const ball = new THREE.Mesh(ballGeo, new THREE.MeshStandardMaterial({ color: 0xffdd55, emissive: 0x553300 }));
  ball.position.set(0, eyeHeight, 0);
  ball.userData.isSceneContent = true;
  scene.add(ball);

  return eyeHeight;
}

function drawViewReport(report) {
  for (const d of report.directions) {
    const color = d.status === 'open' ? COLOR_OPEN : d.status === 'blocked' ? COLOR_BLOCKED : COLOR_UNCERTAIN;
    const dist = d.distance_m || report.radius_m;
    // Bug fixed 2026-09-10 by switching to the shared bearingToDataDir() --
    // see its comment and LESSONS.md for what went wrong when this was a
    // separate, independently-signed inline formula.
    const [dirX, dirY] = bearingToDataDir(d.bearing);
    const x = dirX * dist, y = dirY * dist; // data-space endpoint
    const start = dataToWorld(0, 0, report.eye_height_m);
    const end = dataToWorld(x, y, report.eye_height_m);
    const geo = new THREE.BufferGeometry().setFromPoints([start, end]);
    // REVERTED 2026-09-10 (same session): tried depthTest/depthWrite:false so
    // rays wouldn't vanish behind unrelated nearby buildings from certain
    // camera angles ("cut off at random parts") -- but Wesley then asked for
    // the opposite: a ray should visually stop the instant it hits ANY solid
    // geometry, like a real light beam, not render on/through a building's own
    // surface. Standard depth-testing (the default -- no override here) gives
    // that behavior directly via the GPU's own z-buffer, which is more robust
    // than trusting this file's own 2D ray-polygon math to get the visual
    // termination point exactly right. Trade-off accepted: an OPEN ray can
    // still be hidden behind some unrelated foreground building from an
    // oblique angle -- but that's arguably still correct, since a real
    // sightline beam would really be blocked from THAT camera's view by
    // whatever's actually in front of it.
    const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.7 });
    const line = new THREE.Line(geo, mat);
    line.userData = { isSceneContent: true, isRay: true, report: d, eyeHeightM: report.eye_height_m };
    scene.add(line);
    rayGroup.push(line);
  }
}

function unhighlight(obj) {
  if (obj.userData.isBuilding) obj.material.emissive && obj.material.emissive.setHex(0x000000);
  else if (obj.userData.isRay) obj.material.opacity = 0.7;
  else if (obj.userData.isTransit) obj.scale.set(1, 1, 1);
}

function highlight(obj) {
  if (obj.userData.isBuilding) obj.material.emissive && obj.material.emissive.setHex(0x2a2a10);
  else if (obj.userData.isRay) obj.material.opacity = 1.0;
  else if (obj.userData.isTransit) obj.scale.set(1.6, 1.6, 1.6);
}

function onMouseMove(e) {
  mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
  mouse.y = -(e.clientY / window.innerHeight) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects([...buildingGroup, ...rayGroup, ...transitGroup]);
  if (hovered && (!hits.length || hits[0].object !== hovered)) {
    unhighlight(hovered);
    hovered = null;
  }
  if (hits.length) {
    hovered = hits[0].object;
    highlight(hovered);
  }
}

function onClick() {
  if (!hovered) return;
  const info = document.getElementById('info');
  info.style.display = 'block';

  if (hovered.userData.isBuilding) {
    const d = hovered.userData;
    info.innerHTML = `
      <b>${d.buildingName}</b><br>
      Height: ${d.heightKnown ? d.heightM.toFixed(0) + 'm' : 'unknown -- add to manual_heights.json'}
    `;
  } else if (hovered.userData.isRay) {
    const d = hovered.userData.report;
    const eyeH = hovered.userData.eyeHeightM;
    const statusColor = d.status === 'open' ? '#3ddc6f' : d.status === 'blocked' ? '#e5484d' : '#e0b030';
    let html = `
      <b>Bearing ${d.bearing}&deg;</b> (${d.compass})<br>
      Eye height: ${eyeH.toFixed(1)}m<br>
      Status: <b style="color:${statusColor}">${d.status.toUpperCase()}</b>
    `;
    if (d.blocker) {
      html += `<br>${d.status === 'uncertain' ? 'Nearest obstruction' : 'Blocked by'}: ${d.blocker.name} at ~${d.distance_m.toFixed(0)}m`;
      if (d.blocker.height_m) html += ` (${d.blocker.height_m.toFixed(0)}m tall, ${(d.blocker.height_m - eyeH).toFixed(0)}m above your eye level)`;
    }
    if (d.note) html += `<br><span style="color:#e0b030">${d.note}</span>`;
    info.innerHTML = html;
  } else if (hovered.userData.isTransit) {
    const d = hovered.userData;
    const label = {
      mrt_station: 'MRT Station', mrt_entrance: 'MRT Entrance', bus_stop: 'Bus Stop', bus_interchange: 'Bus Interchange',
      school: 'School', university: 'University', college: 'College', kindergarten: 'Kindergarten',
    }[d.category] || d.category;
    info.innerHTML = `<b>${d.name || label}</b><br>${label}`;
  }
}

async function loadScene(file, floor) {
  const status = document.getElementById('status');
  status.textContent = 'Loading...';
  clearScene();

  let data;
  try {
    const res = await fetch(`../cache/${file}.geojson`, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`${res.status}`);
    data = await res.json();
  } catch (err) {
    status.textContent = `Failed to load cache/${file}.geojson (${err.message}) -- run fetch_buildings.py first`;
    return;
  }
  currentData = data;

  const subj = data.subject || {};
  document.getElementById('address-line').textContent = subj.address || file;
  setFogRange(subj.radius_m || 500);
  setCameraForRadius(subj.radius_m || 500);
  setRayPickThreshold(subj.radius_m || 500);

  // find subject building: whichever footprint contains the origin
  let subjectId = null;
  for (const f of data.features) {
    if (pointInPolygon(0, 0, f.geometry.coordinates[0])) { subjectId = f.properties.osm_id; break; }
  }

  const metersPerStorey = 3.0;
  const subjectMinHeight = (floor - 1) * metersPerStorey + 1.5 + 5; // eye height + a small margin above the marker
  for (const f of data.features) {
    buildBuilding(f, f.properties.osm_id === subjectId, subjectMinHeight);
  }

  buildSubjectMarker(floor, metersPerStorey);

  try {
    const res = await fetch(`../cache/${file}_floor${floor}_view.json`, { cache: 'no-cache' });
    if (res.ok) {
      const report = await res.json();
      drawViewReport(report);
      renderSideBreakdown(report);
      status.textContent = `${data.features.length} buildings | view report loaded (floor ${floor})`;
    } else {
      status.textContent = `${data.features.length} buildings | no view report yet for floor ${floor} (run analyze_view.py)`;
    }
  } catch (e) {
    status.textContent = `${data.features.length} buildings`;
  }
}

function pointInPolygon(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    const intersect = ((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

// ---------------------------------------------------------------------------
// Live client-side pipeline: geocode -> crop island cache -> reproject ->
// analyze sightlines -> render. Mirrors fetch_buildings.py / analyze_view.py
// exactly (same formulas) so results match the CLI path bit-for-bit.
// ---------------------------------------------------------------------------

let islandCachePromise = null;
let manualHeightsPromise = null;

function loadIslandCache() {
  if (!islandCachePromise) {
    islandCachePromise = fetch('../cache/sg_buildings_full.geojson', { cache: 'no-cache' }).then(r => {
      if (!r.ok) throw new Error(`island cache HTTP ${r.status}`);
      return r.json();
    });
  }
  return islandCachePromise;
}

// Override chain, highest priority first: hand-typed manual_heights.json,
// then hdb_heights.json (official HDB per-block floor data, built by
// build_hdb_heights.py -- may not exist yet). Merged so callers only deal
// with one lookup; each entry keeps its own _source for correct labeling.
// Mirrors fetch_buildings.py's load_manual_heights() -- keep both in sync.
function loadManualHeights() {
  if (!manualHeightsPromise) {
    manualHeightsPromise = Promise.all([
      fetch('../cache/manual_heights.json', { cache: 'no-cache' }).then(r => r.ok ? r.json() : { by_osm_id: {}, by_name: {} }).catch(() => ({ by_osm_id: {}, by_name: {} })),
      fetch('../cache/hdb_heights.json', { cache: 'no-cache' }).then(r => r.ok ? r.json() : null).catch(() => null),
    ]).then(([manual, hdb]) => {
      manual.by_osm_id = manual.by_osm_id || {};
      manual.by_name = manual.by_name || {};
      for (const entry of Object.values(manual.by_osm_id)) entry._source = entry._source || 'manual';
      for (const entry of Object.values(manual.by_name)) entry._source = entry._source || 'manual';
      if (hdb) {
        for (const [osmId, entry] of Object.entries(hdb.by_osm_id || {})) {
          if (!(osmId in manual.by_osm_id)) manual.by_osm_id[osmId] = { ...entry, _source: 'hdb_official' };
        }
      }
      return manual;
    });
  }
  return manualHeightsPromise;
}

async function onemapSearch(query) {
  const url = `https://www.onemap.gov.sg/api/common/elastic/search?${new URLSearchParams({
    searchVal: query, returnGeom: 'Y', getAddrDetails: 'Y', pageNum: '1',
  })}`;
  const res = await fetch(url);
  const data = await res.json();
  return data.results || [];
}

// OneMap's search chokes on a full "Street Name, Singapore 123456"-style
// string (a very normal way to type an address, e.g. copied from Google
// Maps) -- confirmed 2026-09-09: "One Raffles Pl, Singapore 048616" returns
// zero results while "One Raffles Place" alone resolves fine. A bare 6-digit
// postal code alone ("048616") also resolves correctly and is the most
// reliable form (unambiguous, no name-matching needed) -- try, in order:
// (1) the raw input, (2) with ", Singapore <postal>" stripped, (3) just the
// postal code if one appears anywhere in the input.
async function geocodeAddress(address) {
  let results = await onemapSearch(address);
  let usedQuery = address;
  if (!results.length) {
    const cleaned = address.replace(/,?\s*singapore\s*\d{6}\s*$/i, '').trim();
    if (cleaned && cleaned !== address) {
      results = await onemapSearch(cleaned);
      usedQuery = cleaned;
    }
  }
  if (!results.length) {
    const postalMatch = address.match(/\b\d{6}\b/);
    if (postalMatch) {
      results = await onemapSearch(postalMatch[0]);
      usedQuery = postalMatch[0];
    }
  }
  if (!results.length) throw new Error(`No geocode match for "${address}"`);
  const top = results[0];
  return {
    address: top.ADDRESS, building: top.BUILDING,
    lat: parseFloat(top.LATITUDE), lon: parseFloat(top.LONGITUDE),
    matchCount: results.length,
    usedQuery: usedQuery !== address ? usedQuery : null,
  };
}

function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const p1 = lat1 * Math.PI / 180, p2 = lat2 * Math.PI / 180;
  const dp = (lat2 - lat1) * Math.PI / 180, dl = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// Same convention as fetch_buildings.py's latlon_to_local_xy (post-fix,
// 2026-09-09): 111_320 is meters PER DEGREE, do not wrap the degree
// difference in a radians conversion before this multiply.
function latlonToLocalXY(lat, lon, originLat, originLon) {
  const dlatDeg = lat - originLat, dlonDeg = lon - originLon;
  const x = dlonDeg * 111320 * Math.cos(originLat * Math.PI / 180);
  const y = dlatDeg * 111320;
  return [x, y];
}

function resolveManualHeight(manual, osmId, name, defaultMps) {
  let entry = null;
  if (osmId != null && manual.by_osm_id && manual.by_osm_id[String(osmId)]) entry = manual.by_osm_id[String(osmId)];
  else if (name && manual.by_name) {
    const key = Object.keys(manual.by_name).find(k => k.toLowerCase() === name.trim().toLowerCase());
    if (key) entry = manual.by_name[key];
  }
  if (!entry) return null;
  const source = entry._source || 'manual';
  if ('height_m' in entry) return { height_m: entry.height_m, levels: entry.levels ?? null, height_source: source };
  if ('levels' in entry) {
    const mps = entry.meters_per_storey ?? defaultMps;
    return { height_m: entry.levels * mps, levels: entry.levels, height_source: source };
  }
  return null;
}

// Real-world-ish lane widths and a lighter/brighter surface for bigger
// roads, closer to how a real map renders a road hierarchy instead of a
// uniform 1px wireframe line (that's what this replaced -- confirmed with
// Wesley 2026-09-10 that it read as unconvincing).
const ROAD_WIDTH_M = {
  motorway: 22, trunk: 18, primary: 15, secondary: 12,
  tertiary: 9, residential: 6, unclassified: 6, service: 3.5, living_street: 5,
};
const ROAD_COLOR = {
  motorway: 0xd7dbe3, trunk: 0xc9ced8, primary: 0xb8bfcc,
  secondary: 0xa6adba, tertiary: 0x969daa, residential: 0x7d8494,
  unclassified: 0x7d8494, service: 0x656c7a, living_street: 0x7d8494,
};

// Builds a flat ribbon (2 triangles per segment) along a road centerline,
// width/color keyed by OSM highway class. Segments aren't mitered at joints
// (small gaps/overlaps at sharp turns) -- an accepted simplification, real
// map renderers mostly do the same rather than computing proper joint caps.
function buildRoadRibbon(localPts, highwayClass) {
  if (localPts.length < 2) return null;
  const width = ROAD_WIDTH_M[highwayClass] || 5;
  const color = ROAD_COLOR[highwayClass] || 0x7d8494;
  const positions = [];
  for (let i = 0; i < localPts.length - 1; i++) {
    const [x1, y1] = localPts[i], [x2, y2] = localPts[i + 1];
    const dx = x2 - x1, dy = y2 - y1;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -(dy / len) * (width / 2), ny = (dx / len) * (width / 2);
    // two triangles forming the segment's rectangle, in world space directly
    const a = dataToWorld(x1 + nx, y1 + ny, 0.12);
    const b = dataToWorld(x2 + nx, y2 + ny, 0.12);
    const c = dataToWorld(x2 - nx, y2 - ny, 0.12);
    const d = dataToWorld(x1 - nx, y1 - ny, 0.12);
    positions.push(
      a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z,
      a.x, a.y, a.z, c.x, c.y, c.z, d.x, d.y, d.z,
    );
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  const mat = new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide });
  return new THREE.Mesh(geo, mat);
}

// Shoelace-formula centroid + area, in local XY meters.
function polygonCentroidArea(ring) {
  let cx = 0, cy = 0, area = 0;
  for (let i = 0; i < ring.length; i++) {
    const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % ring.length];
    const cross = x1 * y2 - x2 * y1;
    area += cross; cx += (x1 + x2) * cross; cy += (y1 + y2) * cross;
  }
  area = Math.abs(area / 2);
  return { cx: cx / (6 * area || 1), cy: cy / (6 * area || 1), area };
}

// OSM has a lot of near-duplicate/overlapping building footprints for the
// same real building (confirmed 2026-09-09: 492 overlapping pairs in one
// 500m-radius crop, e.g. two "Ascott Raffles Place"-area outlines 2.7m
// apart with mismatched heights). Left unmerged, these both render (one
// opaque, one translucent-unknown) creating a visible ghost-wedge artifact
// where they overlap, AND can corrupt the sightline analysis -- a duplicate
// unknown-height "ghost" footprint sitting right in front of the real
// building can register as its own separate "uncertain" obstruction.
// Cluster footprints whose centroids are within 8m and whose areas are
// within 2x of each other; keep one per cluster, preferring whichever has a
// known height (a real duplicate of a shorter/podium building can otherwise
// mask a taller known one).
function dedupeOverlappingFootprints(features) {
  const withGeom = features.map(f => ({ f, ...polygonCentroidArea(f.geometry.coordinates[0]) }));
  withGeom.sort((a, b) => (b.f.properties.height_m ? 1 : 0) - (a.f.properties.height_m ? 1 : 0));
  const kept = [];
  for (const item of withGeom) {
    const dup = kept.find(k => {
      const dist = Math.hypot(k.cx - item.cx, k.cy - item.cy);
      const ratio = Math.max(k.area, item.area) / (Math.min(k.area, item.area) || 1);
      return dist < 8 && ratio < 2;
    });
    if (!dup) kept.push(item);
  }
  return kept.map(k => k.f);
}

function cropAndReproject(islandData, lat, lon, radiusM, manual, metersPerStorey) {
  const features = [];
  for (const feat of islandData.features) {
    const ring = feat.geometry.coordinates[0];
    let clat = 0, clon = 0;
    for (const p of ring) { clon += p[0]; clat += p[1]; }
    clat /= ring.length; clon /= ring.length;
    if (haversineM(lat, lon, clat, clon) > radiusM + 100) continue;

    const p = feat.properties;
    const override = resolveManualHeight(manual, p.osm_id, p.name, metersPerStorey);
    let heightM = null, levels = null, heightSource = null;
    if (override) { heightM = override.height_m; levels = override.levels; heightSource = override.height_source; }
    else if (p.levels) { heightM = p.levels * metersPerStorey; levels = p.levels; heightSource = 'osm_levels'; }

    const localRing = ring.map(([plon, plat]) => latlonToLocalXY(plat, plon, lat, lon));
    const footprintArea = polygonCentroidArea(localRing).area;
    features.push({
      type: 'Feature',
      properties: { osm_id: p.osm_id, name: p.name, building_type: p.building_type, levels, height_m: heightM, height_source: heightSource, footprint_area_m2: footprintArea, addr: p.addr },
      geometry: { type: 'Polygon', coordinates: [localRing] },
    });
  }
  return { type: 'FeatureCollection', features: dedupeOverlappingFootprints(features) };
}

// Height-plausibility heuristic, added 2026-09-10 -- mirrors fetch_buildings.py's
// plausible_max_height_m(), keep both in sync. See that file for the full
// rationale comment; short version: a small-footprint unknown building is very
// unlikely to be a highrise, so cap what it could plausibly be instead of
// treating it as "could theoretically block a 100m+ view".
const LOW_RISE_BUILDING_TYPES = new Set([
  'house', 'detached', 'semidetached_house', 'terrace', 'bungalow',
  'garage', 'garages', 'shed', 'hut', 'carport', 'roof', 'ruins',
]);

function plausibleMaxHeightM(footprintAreaM2, buildingType, metersPerStorey = 3.0) {
  if (LOW_RISE_BUILDING_TYPES.has(buildingType)) return 4 * metersPerStorey;
  if (footprintAreaM2 == null) return null;
  if (footprintAreaM2 < 250) return 5 * metersPerStorey;
  if (footprintAreaM2 < 600) return 8 * metersPerStorey;
  return null;
}

// ---- sightline analysis, ported from analyze_view.py (same math/thresholds) ----

function raySegmentIntersectionJS(ox, oy, dx, dy, x1, y1, x2, y2) {
  const ex = x2 - x1, ey = y2 - y1;
  const rx = x1 - ox, ry = y1 - oy;
  const denom = dx * ey - dy * ex;
  if (Math.abs(denom) < 1e-12) return null;
  const t = (ey * rx - ex * ry) / denom;
  const s = (dy * rx - dx * ry) / denom;
  if (t >= 0 && s >= 0 && s <= 1) return t;
  return null;
}

function rayPolygonMinDistanceJS(ox, oy, dx, dy, ring) {
  let min = null;
  for (let i = 0; i < ring.length; i++) {
    const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % ring.length];
    const t = raySegmentIntersectionJS(ox, oy, dx, dy, x1, y1, x2, y2);
    if (t !== null && (min === null || t < min)) min = t;
  }
  return min;
}

function bearingLabelJS(b) {
  const dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  return dirs[Math.round(b / 22.5) % 16];
}

function analyzeSightlinesJS(features, subjectId, floor, metersPerStorey, eyeHeightAboveFloor, radiusM, step, maxObstructionDist) {
  const eyeHeight = (floor - 1) * metersPerStorey + eyeHeightAboveFloor;
  const directions = [];
  for (let b = 0; b < 360; b += step) {
    const [dx, dy] = bearingToDataDir(b);
    let confirmed = null, uncertain = null;
    for (const f of features) {
      if (f.properties.osm_id === subjectId) continue;
      const dist = rayPolygonMinDistanceJS(0, 0, dx, dy, f.geometry.coordinates[0]);
      if (dist === null || dist > radiusM || dist < 3) continue;
      // "Background noise" cutoff (mirrors analyze_view.py's Python version exactly) --
      // a building far enough away no longer reads as blocking the view even if it's
      // technically taller than eye height; skip it as an obstruction candidate entirely.
      if (maxObstructionDist != null && dist > maxObstructionDist) continue;
      const h = f.properties.height_m;
      if (h != null) {
        if (h > eyeHeight && (!confirmed || dist < confirmed.dist)) confirmed = { dist, f };
      } else {
        // Height-plausibility heuristic (2026-09-10): see plausibleMaxHeightM() --
        // rules out small-footprint unknowns that plausibly can't reach eye height,
        // mirrors fetch_buildings.py/analyze_view.py's Python version exactly.
        const plausibleMax = plausibleMaxHeightM(f.properties.footprint_area_m2, f.properties.building_type);
        if (plausibleMax !== null && plausibleMax <= eyeHeight) continue;
        if (!uncertain || dist < uncertain.dist) uncertain = { dist, f };
      }
    }
    // A confirmed blocker anywhere within radius makes this direction "blocked" even if a
    // nearer unknown-height obstruction also exists -- see analyze_view.py, fixed 2026-09-09.
    if (confirmed) {
      const p = confirmed.f.properties;
      const entry = { bearing: b, compass: bearingLabelJS(b), status: 'blocked', distance_m: confirmed.dist,
        blocker: { name: p.name || p.addr || `osm:${p.osm_id}`, height_m: p.height_m, osm_id: p.osm_id } };
      if (uncertain && uncertain.dist < confirmed.dist) {
        const up = uncertain.f.properties;
        entry.note = `may be blocked even sooner (~${uncertain.dist.toFixed(0)}m) by ${up.name || up.addr || `osm:${up.osm_id}`} -- its height is unknown`;
      }
      directions.push(entry);
    } else if (uncertain) {
      const p = uncertain.f.properties;
      directions.push({ bearing: b, compass: bearingLabelJS(b), status: 'uncertain', distance_m: uncertain.dist,
        blocker: { name: p.name || p.addr || `osm:${p.osm_id}`, height_m: null, osm_id: p.osm_id } });
    } else {
      // Open -- impute radiusM as distance_m (a floor, not a measured distance) instead of
      // null, so every sightline reports a usable distance-before-blocked number.
      directions.push({ bearing: b, compass: bearingLabelJS(b), status: 'open', distance_m: radiusM, blocker: null });
    }
  }
  return { floor, eye_height_m: eyeHeight, meters_per_storey: metersPerStorey, radius_m: radiusM, directions };
}

// "Background noise" cutoff toggle: when checked, a building beyond the
// distance typed in #max-obstruction-input no longer counts as blocking the
// view (see analyzeSightlinesJS's maxObstructionDist param) -- it's still
// rendered, just excluded as an obstruction candidate. Returns null (no
// cutoff, matches the old always-on behavior) when the toggle is off.
function getMaxObstructionDistFromUI() {
  const enabled = document.getElementById('max-obstruction-toggle')?.checked;
  if (!enabled) return null;
  const val = parseFloat(document.getElementById('max-obstruction-input')?.value);
  return Number.isFinite(val) && val > 0 ? val : null;
}

async function goToAddress(address, floor, radiusM) {
  const status = document.getElementById('status');
  status.textContent = 'Geocoding...';
  clearScene();
  document.getElementById('info').style.display = 'none';

  let geo;
  try {
    geo = await geocodeAddress(address);
  } catch (err) {
    status.textContent = `Geocode failed: ${err.message}`;
    return;
  }
  document.getElementById('address-line').textContent = geo.address +
    (geo.matchCount > 1 ? ` (1 of ${geo.matchCount} matches)` : '') +
    (geo.usedQuery ? ` [searched as "${geo.usedQuery}"]` : '');

  status.textContent = 'Loading island building cache (first time only, ~30MB)...';
  let island, manual;
  try {
    [island, manual] = await Promise.all([loadIslandCache(), loadManualHeights()]);
  } catch (err) {
    status.textContent = `Failed to load building cache: ${err.message} -- run build_island_cache.py first`;
    return;
  }

  status.textContent = 'Cropping + analyzing...';
  const metersPerStorey = 3.0;
  const data = cropAndReproject(island, geo.lat, geo.lon, radiusM, manual, metersPerStorey);
  data.subject = { address: geo.address, lat: geo.lat, lon: geo.lon, radius_m: radiusM };
  currentData = data;
  setFogRange(radiusM);
  setCameraForRadius(radiusM);
  setRayPickThreshold(radiusM);

  let subjectId = null;
  for (const f of data.features) {
    if (pointInPolygon(0, 0, f.geometry.coordinates[0])) { subjectId = f.properties.osm_id; break; }
  }
  const subjectMinHeight = (floor - 1) * metersPerStorey + 1.5 + 5;
  for (const f of data.features) buildBuilding(f, f.properties.osm_id === subjectId, subjectMinHeight);
  buildSubjectMarker(floor, metersPerStorey);

  const maxObstructionDist = getMaxObstructionDistFromUI();
  const report = analyzeSightlinesJS(data.features, subjectId, floor, metersPerStorey, 1.5, radiusM, 5, maxObstructionDist);
  drawViewReport(report);

  const blocked = report.directions.filter(d => d.status === 'blocked').length;
  const open = report.directions.filter(d => d.status === 'open').length;
  status.textContent = `${data.features.length} buildings | ${open} open / ${blocked} blocked / ${report.directions.length - open - blocked} uncertain (floor ${floor})`;
  renderSideBreakdown(report);

  loadContextLayers(geo.lat, geo.lon, radiusM);
}

// Same 4-side simplification as facade_blockage_report.py's side_breakdown()
// -- N/E/S/W as 90deg bearing quadrants, not the building's true facade
// angles. Keep both in sync if this logic changes.
const SIDES_JS = {
  N: b => b >= 315 || b < 45,
  E: b => b >= 45 && b < 135,
  S: b => b >= 135 && b < 225,
  W: b => b >= 225 && b < 315,
};

function renderSideBreakdown(report) {
  const el = document.getElementById('side-breakdown');
  if (!el) return;
  const rows = Object.entries(SIDES_JS).map(([side, inRange]) => {
    const bucket = report.directions.filter(d => inRange(d.bearing));
    const n = bucket.length || 1;
    const blockedPct = Math.round(100 * bucket.filter(d => d.status === 'blocked').length / n);
    const uncertainPct = Math.round(100 * bucket.filter(d => d.status === 'uncertain').length / n);
    const openPct = 100 - blockedPct - uncertainPct;
    return `<tr><td>${side}</td><td>${blockedPct}%</td><td>${uncertainPct}%</td><td>${openPct}%</td></tr>`;
  }).join('');
  el.innerHTML = `
    <div style="margin-bottom:4px;color:#8b96ad">% blocked per side, floor ${report.floor}</div>
    <table style="width:100%;border-collapse:collapse;font-size:11px">
      <tr style="color:#8b96ad"><th style="text-align:left">Side</th><th>Blocked</th><th>Uncertain</th><th>Open</th></tr>
      ${rows}
    </table>
  `;
}

// ---- context layers: roads, water, transit -- purely visual/orientation,
// never obstructions. Cropped client-side from whole-island caches built by
// build_island_layers.py, same pattern as the buildings island cache. Any
// layer whose cache file doesn't exist yet is silently skipped (optional).

let roadsCachePromise = null, waterCachePromise = null, transitCachePromise = null;
let parksCachePromise = null, schoolsCachePromise = null;

function loadLayerCache(promiseVar, filename) {
  return fetch(`../cache/${filename}`, { cache: 'no-cache' }).then(r => {
    if (!r.ok) throw new Error(`${filename} not built yet (${r.status})`);
    return r.json();
  });
}

async function loadContextLayers(lat, lon, radiusM) {
  if (!roadsCachePromise) roadsCachePromise = loadLayerCache(null, 'sg_roads_full.geojson').catch(() => null);
  if (!waterCachePromise) waterCachePromise = loadLayerCache(null, 'sg_water_full.geojson').catch(() => null);
  if (!transitCachePromise) transitCachePromise = loadLayerCache(null, 'sg_transit_full.geojson').catch(() => null);
  if (!parksCachePromise) parksCachePromise = loadLayerCache(null, 'sg_parks_full.geojson').catch(() => null);
  if (!schoolsCachePromise) schoolsCachePromise = loadLayerCache(null, 'sg_schools_full.geojson').catch(() => null);

  const [roads, water, transit, parks, schools] = await Promise.all([
    roadsCachePromise, waterCachePromise, transitCachePromise, parksCachePromise, schoolsCachePromise,
  ]);

  if (roads) {
    for (const f of roads.features) {
      const ring = f.geometry.coordinates;
      const clat = ring.reduce((s, p) => s + p[1], 0) / ring.length;
      const clon = ring.reduce((s, p) => s + p[0], 0) / ring.length;
      if (haversineM(lat, lon, clat, clon) > radiusM + 100) continue;
      const localPts = ring.map(([plon, plat]) => latlonToLocalXY(plat, plon, lat, lon));
      const mesh = buildRoadRibbon(localPts, f.properties.highway);
      if (mesh) { mesh.userData.isSceneContent = true; scene.add(mesh); }
    }
  }

  if (water) {
    for (const f of water.features) {
      const ring = f.geometry.coordinates[0];
      const clat = ring.reduce((s, p) => s + p[1], 0) / ring.length;
      const clon = ring.reduce((s, p) => s + p[0], 0) / ring.length;
      // water bodies can be huge (Marina Bay) -- centroid-in-radius is an
      // approximation, a big body whose centroid falls just outside a small
      // radius will be missed even if part of it is visible. Acceptable for
      // orientation context, not worth precise polygon clipping here.
      if (haversineM(lat, lon, clat, clon) > radiusM + 400) continue;
      const localRing = ring.map(([plon, plat]) => latlonToLocalXY(plat, plon, lat, lon));
      const shape = new THREE.Shape();
      localRing.forEach(([x, y], i) => (i === 0 ? shape.moveTo(x, y) : shape.lineTo(x, y)));
      const geo = new THREE.ShapeGeometry(shape);
      geo.rotateX(-Math.PI / 2);
      const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
        color: 0x1d5a8f, transparent: true, opacity: 0.55, side: THREE.DoubleSide, depthWrite: false,
      }));
      mesh.position.y = 0.05;
      mesh.userData.isSceneContent = true;
      scene.add(mesh);
    }
  }

  if (transit) {
    const markerColor = { mrt_station: 0xa970e0, mrt_entrance: 0xa970e0, bus_stop: 0x30c9d6, bus_interchange: 0x30c9d6 };
    const markerSize = { mrt_station: 3.5, mrt_entrance: 1.8, bus_stop: 1.2, bus_interchange: 2.2 };
    for (const f of transit.features) {
      const [plon, plat] = f.geometry.coordinates;
      if (haversineM(lat, lon, plat, plon) > radiusM + 50) continue;
      const [x, y] = latlonToLocalXY(plat, plon, lat, lon);
      const cat = f.properties.category;
      const geo = new THREE.SphereGeometry(markerSize[cat] || 1.5, 10, 10);
      const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: markerColor[cat] || 0xffffff }));
      mesh.position.copy(dataToWorld(x, y, (markerSize[cat] || 1.5) + 1));
      mesh.userData = { isSceneContent: true, isTransit: true, name: f.properties.name, category: cat };
      scene.add(mesh);
      transitGroup.push(mesh);
    }
  }

  if (parks) {
    for (const f of parks.features) {
      const ring = f.geometry.coordinates[0];
      const clat = ring.reduce((s, p) => s + p[1], 0) / ring.length;
      const clon = ring.reduce((s, p) => s + p[0], 0) / ring.length;
      if (haversineM(lat, lon, clat, clon) > radiusM + 100) continue;
      const localRing = ring.map(([plon, plat]) => latlonToLocalXY(plat, plon, lat, lon));
      const shape = new THREE.Shape();
      localRing.forEach(([x, y], i) => (i === 0 ? shape.moveTo(x, y) : shape.lineTo(x, y)));
      const geo = new THREE.ShapeGeometry(shape);
      geo.rotateX(-Math.PI / 2);
      const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
        color: 0x2f7d4a, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthWrite: false,
      }));
      mesh.position.y = 0.04;
      mesh.userData.isSceneContent = true;
      scene.add(mesh);
    }
  }

  if (schools) {
    const schoolColor = 0xe89b3c;
    for (const f of schools.features) {
      const [plon, plat] = f.geometry.coordinates;
      if (haversineM(lat, lon, plat, plon) > radiusM + 50) continue;
      const [x, y] = latlonToLocalXY(plat, plon, lat, lon);
      const geo = new THREE.ConeGeometry(2.2, 4, 4);
      const mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: schoolColor }));
      mesh.position.copy(dataToWorld(x, y, 3));
      mesh.userData = { isSceneContent: true, isTransit: true, name: f.properties.name, category: f.properties.category };
      scene.add(mesh);
      transitGroup.push(mesh);
    }
  }
}

document.getElementById('go-btn').addEventListener('click', () => {
  const address = document.getElementById('address-input').value.trim();
  const floor = parseInt(document.getElementById('floor-input').value, 10) || 1;
  const radius = parseFloat(document.getElementById('radius-input').value) || 500;
  if (address) goToAddress(address, floor, radius);
});

document.getElementById('load-file-btn').addEventListener('click', () => {
  const file = document.getElementById('file-input').value.trim();
  const floor = parseInt(document.getElementById('floor-input').value, 10) || 1;
  if (file) loadScene(file, floor);
});

init3D();
goToAddress(document.getElementById('address-input').value, parseInt(document.getElementById('floor-input').value, 10), parseFloat(document.getElementById('radius-input').value));
