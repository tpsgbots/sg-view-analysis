"""
Local dev server for sg-view-analysis that sends Cache-Control: no-store on
every response -- fixes a recurring class of bug this session where the
browser served a stale cached copy of a data file (manual_heights.json) or
even the app's own code (app.js, index.html) after an edit, with no error,
just silently wrong/old behavior. Plain `python -m http.server` sets no
cache headers at all, so the browser falls back to its own heuristics,
which is what kept causing this.

Usage:
    python serve.py [port]
"""
import sys
import functools
import socketserver
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = Path(__file__).parent


class NoCacheHandler(SimpleHTTPRequestHandler):
    # "no-cache, must-revalidate" (NOT "no-store") -- forces the browser to always
    # ask the server before trusting its cached copy (fixes the original silent-
    # staleness bug), but still lets it reuse the cached body via a conditional GET
    # when nothing changed. SimpleHTTPRequestHandler already answers If-Modified-Since
    # with 304 Not Modified for an unchanged file, so an unchanged large data file
    # (sg_buildings_full.geojson is ~89MB, not the ~30MB originally assumed) costs a
    # tiny 304 round-trip instead of a full re-transfer on every single page load --
    # "no-store" was correctness-safe but forced a full re-download every time
    # regardless of whether anything actually changed, which is what made every
    # load slow once it was applied to these big, rarely-changing data files too.
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    # Plain HTTPServer handles one connection at a time -- the viewer fires
    # 6+ parallel fetch()es for the big island-cache geojson files on every
    # load (no-store means every load re-downloads them fresh), and a
    # single-threaded server serialized/stalled on that. Threading fixes it.
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    # Serve THIS script's own directory regardless of the process's actual
    # working directory when launched.
    handler = functools.partial(NoCacheHandler, directory=str(HERE))
    server = ThreadingHTTPServer(("", port), handler)
    print(f"Serving sg-view-analysis on http://localhost:{port} (no-cache headers on every response)")
    print(f"Open http://localhost:{port}/viewer/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
