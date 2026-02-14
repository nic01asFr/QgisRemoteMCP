"""
BigQgisMCP — MJPEG Stream Server
═══════════════════════════════════════════════════════════════════

Captures the X11 display and streams it as MJPEG.
Useful for lightweight live preview without full VNC.

Runs on port 8081.
"""

import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DISPLAY = ":99"
PORT = 8081
FRAME_RATE = 2  # FPS (low to save bandwidth)


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path != "/stream":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            while True:
                # Capture current display using ffmpeg (single frame)
                proc = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-f", "x11grab",
                        "-video_size", "960x540",
                        "-i", DISPLAY,
                        "-frames:v", "1",
                        "-q:v", "8",
                        "-f", "image2",
                        "-vcodec", "mjpeg",
                        "pipe:1"
                    ],
                    capture_output=True,
                    timeout=5,
                )

                if proc.returncode == 0 and proc.stdout:
                    frame = proc.stdout
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()

                time.sleep(1.0 / FRAME_RATE)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass  # Suppress logs


if __name__ == "__main__":
    print(f"[Stream Server] Starting MJPEG stream on :{PORT}/stream")
    server = HTTPServer(("0.0.0.0", PORT), StreamHandler)
    server.serve_forever()
