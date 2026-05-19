"""Local tile viewer — serves tiles from a directory and opens a Leaflet map."""

import http.server
import json
import os
import socketserver
import threading
import webbrowser
from pathlib import Path


_VIEWER_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>hillgen viewer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body { margin: 0; padding: 0; }
  #map { width: 100vw; height: 100vh; background: #111; }
</style>
</head>
<body>
<div id="map"></div>
<script>
  const map = L.map('map').setView([__CENTER_LAT__, __CENTER_LON__], __ZOOM__);

  // Dark basemap
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png', {
    attribution: '&copy; OSM &copy; CARTO',
    maxZoom: 20,
  }).addTo(map);

  // Hillshade overlay
  L.tileLayer('http://localhost:__PORT__/tiles/{z}/{x}/{y}.png', {
    maxZoom: 20,
    opacity: 1.0,
    tms: false,
  }).addTo(map);
</script>
</body>
</html>"""


def serve_tiles(tiles_dir: Path, port: int = 9999, open_browser: bool = True):
    """Serve a tile directory and open a Leaflet viewer.

    Args:
        tiles_dir: Directory containing {z}/{x}/{y}.png tiles
        port: HTTP port (default 9999)
        open_browser: Whether to open the browser automatically
    """
    tiles_dir = Path(tiles_dir).resolve()

    if not tiles_dir.exists():
        raise FileNotFoundError(f"Tile directory not found: {tiles_dir}")

    # Detect center and zoom from tile directory
    center_lat, center_lon, zoom = _detect_center(tiles_dir)

    # Build HTML with substituted values
    html = _VIEWER_HTML
    html = html.replace("__CENTER_LAT__", str(center_lat))
    html = html.replace("__CENTER_LON__", str(center_lon))
    html = html.replace("__ZOOM__", str(zoom))
    html = html.replace("__PORT__", str(port))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tiles_dir.parent), **kwargs)

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())
                return

            # Serve tiles from the tiles subdirectory
            if self.path.startswith("/tiles/"):
                tile_path = tiles_dir / self.path[7:]  # strip /tiles/
                if tile_path.exists():
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(tile_path.read_bytes())
                else:
                    self.send_response(404)
                    self.end_headers()
                return

            super().do_GET()

        def log_message(self, format, *args):
            pass  # suppress access logs

    with socketserver.TCPServer(("0.0.0.0", port), Handler) as httpd:
        url = f"http://localhost:{port}/"
        print(f"Serving tiles from {tiles_dir}")
        print(f"Viewer: {url}")
        print("Press Ctrl+C to stop")

        if open_browser:
            webbrowser.open(url)

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def _detect_center(tiles_dir: Path):
    """Detect approximate center and zoom from tile directory structure."""
    # Find available zoom levels
    zooms = sorted(
        int(d.name) for d in tiles_dir.iterdir()
        if d.is_dir() and d.name.isdigit()
    )

    if not zooms:
        return 46.2, -122.18, 12  # fallback to St Helens

    mid_zoom = zooms[len(zooms) // 2]

    # Get x/y range at mid zoom
    zoom_dir = tiles_dir / str(mid_zoom)
    xs = sorted(int(d.name) for d in zoom_dir.iterdir() if d.is_dir() and d.name.isdigit())
    if not xs:
        return 46.2, -122.18, mid_zoom

    mid_x = xs[len(xs) // 2]
    x_dir = zoom_dir / str(mid_x)
    ys = sorted(
        int(f.stem) for f in x_dir.iterdir()
        if f.is_file() and f.stem.isdigit()
    )
    if not ys:
        return 46.2, -122.18, mid_zoom

    mid_y = ys[len(ys) // 2]

    # Convert tile coordinates to lat/lon (XYZ scheme)
    import math
    n = 2 ** mid_zoom
    lon = mid_x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * mid_y / n))))

    return round(lat, 4), round(lon, 4), mid_zoom
