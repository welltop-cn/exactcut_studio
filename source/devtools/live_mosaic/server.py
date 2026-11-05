"""Loopback-only live mosaic experiment; serves one explicitly supplied video."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import json
import subprocess
from fractions import Fraction
from functools import lru_cache
from threading import BoundedSemaphore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18884)
    args = parser.parse_args()
    video = args.video.resolve(strict=True)
    html = Path(__file__).with_name('index.html')
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=r_frame_rate,avg_frame_rate,nb_frames,duration', '-of', 'json', str(video)], timeout=15))['streams'][0]
    fps = float(Fraction(probe['avg_frame_rate']))
    metadata = {'fps': fps, 'frame_count': int(probe['nb_frames']), 'duration': float(probe['duration'])}
    frames = BoundedSemaphore(2)

    @lru_cache(maxsize=96)
    def frame_image(index):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', f'select=eq(n\\,{index}),scale=960:-2', '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-threads', '1', 'pipe:1'], timeout=20)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path in {'/', '/timeline.js', '/metadata'} or re.fullmatch(r'/frame/[0-9]+\.jpg', path):
                content_type = 'text/html; charset=utf-8'
                if path == '/metadata':
                    body = json.dumps(metadata).encode()
                    content_type = 'application/json'
                elif path == '/timeline.js':
                    body = html.with_name('timeline.js').read_bytes()
                    content_type = 'text/javascript; charset=utf-8'
                elif path.startswith('/frame/'):
                    index = int(path[7:-4])
                    if not 0 <= index < metadata['frame_count']:
                        self.send_error(416)
                        return
                    if not frames.acquire(blocking=False):
                        self.send_error(503)
                        return
                    try:
                        body = frame_image(index)
                        content_type = 'image/jpeg'
                    except (subprocess.SubprocessError, OSError):
                        self.send_error(503)
                        return
                    finally:
                        frames.release()
                else:
                    body = html.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if path != '/sample.mp4':
                self.send_error(404)
                return
            size = video.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get('Range')
            if requested:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                if not match or not any(match.groups()):
                    self.send_error(416)
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.end_headers()
                    return
            self.send_response(206 if requested else 200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if requested:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            try:
                with video.open('rb') as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(remaining, 262144))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Live mosaic experiment: http://localhost:{args.port}/', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
