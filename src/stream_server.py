"""
QgisRemoteMCP — MJPEG Stream Server
═══════════════════════════════════════════════════════════════════

Captures the X11 display and streams it as MJPEG.
Runs on port 8081. Includes CORS for MCP Apps embedding.
"""

import os
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

DISPLAY = os.environ.get("DISPLAY", ":99")
PORT = 8081
FRAME_RATE = 4  # FPS

# Parse display resolution from env (e.g. "1920x1080x24")
_res = os.environ.get("QGIS_RESOLUTION", "1920x1080x24").split("x")
CAPTURE_W = _res[0]  # "1920"
CAPTURE_H = _res[1]  # "1080"


class StreamHandler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path == "/frame":
            # Single JPEG frame (for fallback polling)
            frame = self._capture_frame()
            if frame:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.send_header("Cache-Control", "no-cache, no-store")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(frame)
            else:
                self.send_response(500)
                self.end_headers()
            return

        if self.path != "/stream":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()

        try:
            while True:
                frame = self._capture_frame()
                if frame:
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(1.0 / FRAME_RATE)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _capture_frame(self):
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "x11grab",
                    "-video_size", f"{CAPTURE_W}x{CAPTURE_H}",
                    "-i", DISPLAY,
                    "-vf", "scale=1280:720",
                    "-frames:v", "1",
                    "-q:v", "5",
                    "-f", "mjpeg",
                    "pipe:1"
                ],
                capture_output=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except subprocess.TimeoutExpired:
            pass
        return None

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"[Stream Server] Starting MJPEG stream on :{PORT}/stream")
    print(f"[Stream Server] Single frame: :{PORT}/frame")
    server = HTTPServer(("0.0.0.0", PORT), StreamHandler)
    server.serve_forever()
