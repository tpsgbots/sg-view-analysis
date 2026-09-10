// ══════════════════════════════════════════════════════════════
// sg-view-analysis-data-bot — Cloudflare Worker
// Serves the sg-view-analysis island-wide data cache (buildings, roads,
// water, transit, parks/schools, manual/HDB heights) straight out of its
// own R2 bucket (sg-view-analysis-data) as plain file GETs, so the viewer
// can fetch cache/<name> from this Worker's URL exactly like it fetches
// cache/<name> from the local dev server -- same relative path, just a
// different origin.
//
// Deliberately its own bucket + Worker, not folded into the existing
// thunderprint-gallery-bot / webstorebucket1 setup used for print sample
// images -- keeps this project's data fully separate from ThunderPrintSG's.
//
// Usage:
//   GET .../cache/sg_buildings_full.geojson
//   GET .../cache/manual_heights.json
//
// Caching: "no-cache, must-revalidate" + ETag-based conditional GET, not
// "no-store" -- same reasoning as serve.py's local dev server (see
// LESSONS.md, 2026-09-10): always revalidate with R2 so a re-upload after
// rerunning the build scripts is never silently stale, but an unchanged
// file gets a cheap 304 instead of re-transferring the full ~89MB building
// cache on every load.
// ══════════════════════════════════════════════════════════════

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders() });
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405, headers: corsHeaders() });
    }

    const url = new URL(request.url);
    const key = url.pathname.replace(/^\/+/, ''); // "/cache/foo.json" -> "cache/foo.json"

    if (!key) {
      return json({ error: 'No object key in path' }, 400);
    }

    // Manual ETag comparison rather than R2's get(key, {onlyIf}) -- that
    // conditional-get option threw a runtime exception (CF error 1101) in
    // testing, root cause not chased further since this is simple and
    // reliable: a cheap head() to check the etag, then only pay for a full
    // get() (and only then stream ~89MB back to the client) when it
    // actually changed.
    const ifNoneMatch = request.headers.get('If-None-Match');
    const head = await env.DATA_BUCKET.head(key);
    if (head === null) {
      return json({ error: 'Not found', key }, 404);
    }
    if (ifNoneMatch && ifNoneMatch === head.httpEtag) {
      return new Response(null, { status: 304, headers: { ETag: head.httpEtag, ...corsHeaders() } });
    }

    const object = await env.DATA_BUCKET.get(key);
    if (object === null) {
      return json({ error: 'Not found', key }, 404);
    }

    const headers = new Headers(corsHeaders());
    object.writeHttpMetadata(headers);
    headers.set('ETag', object.httpEtag);
    headers.set('Cache-Control', 'no-cache, must-revalidate');
    if (!headers.get('Content-Type')) headers.set('Content-Type', guessContentType(key));

    return new Response(object.body, { headers });
  },
};

function guessContentType(key) {
  if (key.endsWith('.geojson') || key.endsWith('.json')) return 'application/json';
  return 'application/octet-stream';
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
    'Access-Control-Allow-Headers': 'If-None-Match, Content-Type',
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders() },
  });
}
