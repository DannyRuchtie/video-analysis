#!/usr/bin/env python3
"""Run the Shotline interface on this Mac."""

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic, time
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading

from color_timeline import average_colors, color_hex, write_color_strip
from import_limits import (
    AdmissionError, ImportLimits, FREE_IMPORTS, JOB_SECONDS, MAX_PUBLIC_BYTES, MAX_PUBLIC_DURATION, WINDOW_SECONDS,
)
from scene_screenshots import screenshot_manifest
from video import DEFAULT_THRESHOLD, VideoError, extract_frames, inspect_video, write_json

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
BUNDLED_EXAMPLE = ROOT / "examples" / "paperman"
DATA = ROOT / ".shotline"
RESULTS = DATA / "results"
WORK = DATA / "work"
YOUTUBE_TOOL = DATA / "youtube" / "bin" / "yt-dlp"
MAX_UPLOAD = 16 * 1024 ** 3
HOST = "127.0.0.1"
DEFAULT_VIDEO = "https://www.youtube.com/watch?v=XrqSF2OOz_M"
JOB_ID = r"(default|[a-f0-9]{32})"
JOB_ROUTE = re.compile(rf"/api/jobs/{JOB_ID}")
MEDIA_NAME = r"(?:manifest\.json|color-strip\.png|(?:thumbs/)?shot-\d{6,}\.jpg)"
RESULT_FILE = re.compile(rf"/results/{JOB_ID}/({MEDIA_NAME})")
YOUTUBE_ID = re.compile(r"[a-zA-Z0-9_-]{11}")
TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".jpg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4",
}


def youtube_video_url(value):
    if not isinstance(value, str):
        raise VideoError("Enter a YouTube video URL.")
    try:
        url = urlsplit(value.strip())
        host = url.netloc.lower()
        parts = url.path.strip("/").split("/")
        if url.scheme not in {"https", "http"}:
            raise ValueError
        if host == "youtu.be" and len(parts) == 1:
            video_id = parts[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parts == ["watch"]:
                video_id = parse_qs(url.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
                video_id = parts[1]
            else:
                raise ValueError
        else:
            raise ValueError
        if not YOUTUBE_ID.fullmatch(video_id):
            raise ValueError
    except ValueError as error:
        raise VideoError("Enter a link to a single YouTube video.") from error
    return f"https://www.youtube.com/watch?v={video_id}"


def download_youtube(url, folder, timeout=1800, public=False):
    executable = str(YOUTUBE_TOOL) if YOUTUBE_TOOL.is_file() else shutil.which("yt-dlp")
    if not executable:
        raise VideoError("YouTube import requires yt-dlp. See the Shotline README for setup.")

    duration_filter = f" & duration <= {MAX_PUBLIC_DURATION}" if public else ""
    max_size = str(MAX_PUBLIC_BYTES) if public else "16G"
    formats = (
        f"bv[height<=1080][protocol=https][filesize<={max_size}]/"
        f"b[height<=1080][protocol=https][filesize<={max_size}]"
        if public else "bv[height<=1080]/b[height<=1080]/bv/b"
    )
    arguments = [
        "--ignore-config", "--no-plugin-dirs", "--no-cache-dir", "--no-playlist",
        "--no-progress", "--no-simulate", "--socket-timeout", "20", "--retries", "2",
        "--match-filter", "!is_live" + duration_filter, "--max-filesize", max_size,
        "-f", formats, "--js-runtimes", "node",
        "-o", "source.%(ext)s", "--print", "after_move:filepath",
        "--print", "after_move:title", "--", url,
    ]
    with subprocess.Popen(
        [executable, *arguments], text=True, errors="replace", cwd=folder, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise VideoError("The YouTube download timed out.") from error
    if process.returncode:
        errors = stderr.strip().splitlines()
        message = errors[-1].removeprefix("ERROR: ") if errors else "The download failed."
        raise VideoError(f"YouTube import failed. {message}")

    if public and not stdout.strip():
        raise VideoError("Choose a downloadable YouTube video that's 10 minutes or shorter and under 250 MB.")
    filename, _, title = stdout.strip().partition("\n")
    video = (folder / filename).resolve()
    if video.parent != folder.resolve() or not video.name.startswith("source.") or not video.is_file():
        raise VideoError("YouTube did not return a downloadable video.")
    if public and video.stat().st_size > MAX_PUBLIC_BYTES:
        raise VideoError("Choose a video under 250 MB.")
    return video, title.strip() or "YouTube video"


def analyze(
    video, destination, title, source, threshold=DEFAULT_THRESHOLD, progress=lambda _stage: None, url=None,
    *, max_duration=None, time_remaining=lambda: None,
):
    progress("detecting")
    timeline = inspect_video(
        video, threshold, with_colors=True, max_duration=max_duration, time_remaining=time_remaining,
    )
    colors = average_colors(timeline)
    progress("capturing")
    extract_frames(video, timeline, destination, thumbnails=True, timeout=time_remaining())
    write_color_strip(destination / "color-strip.png", timeline, colors)
    write_json(destination / "manifest.json", {
        "title": title,
        "source": source,
        "url": url,
        "threshold": threshold,
        "duration": timeline.duration,
        "shots": [
            {**shot, "thumbnail": f"thumbs/{shot['image']}", "color": color_hex(rgb), "rgb": rgb}
            for shot, rgb in zip(screenshot_manifest(timeline), colors)
        ],
    })


class ShotlineServer(ThreadingHTTPServer):
    def __init__(self, port, public_origin=None, access_code=None):
        super().__init__((HOST, port), Handler)
        self.public_origin = public_origin
        self.results = DATA / "public-results" if public_origin else RESULTS
        self.work = DATA / "public-work" if public_origin else WORK
        for directory in (self.results, self.work, RESULTS):
            directory.mkdir(parents=True, exist_ok=True)
        self.limits = ImportLimits(DATA / "import-quota.sqlite3", access_code) if public_origin else None
        self.jobs = {}
        self.job_lock = threading.Lock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")

    def update(self, job, **details):
        with self.job_lock:
            self.jobs[job] = details

    def process(self, job, workspace, source, name, threshold):
        public = self.limits is not None
        deadline = monotonic() + JOB_SECONDS

        def progress(stage):
            self.update(job, status=stage)

        def time_remaining():
            if not public:
                return None
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise VideoError("The analysis ran out of processing time.")
            return remaining

        try:
            if public:
                self.prune_results()
            if source == "youtube":
                progress("downloading")
                video, title = download_youtube(name, workspace, time_remaining() or 1800, public)
                if job == "default":
                    title = "Paperman"
            else:
                video, title = workspace / "upload", Path(name).stem or "Untitled video"
            destination = workspace / "result"
            analyze(
                video, destination, title, source, threshold, progress, name if source == "youtube" else None,
                max_duration=MAX_PUBLIC_DURATION if public else None, time_remaining=time_remaining,
            )
            time_remaining()
            destination.rename(self.result_path(job))
            with self.job_lock:
                self.jobs.pop(job, None)
        except VideoError as error:
            self.update(job, status="error", error=str(error))
        except Exception:
            logging.exception("Analysis failed for job %s", job)
            self.update(job, status="error", error="Analysis failed unexpectedly. See the server log.")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            if public:
                self.limits.slots.release()

    def enqueue(self, job, workspace, source, name, threshold):
        self.update(job, status="queued")
        self.worker.submit(self.process, job, workspace, source, name, threshold)

    def prepare_default(self):
        if self.job_state("default") is None:
            if self.limits:
                self.limits.slots.acquire()
            self.enqueue("default", Path(mkdtemp(dir=self.work)), "youtube", DEFAULT_VIDEO, DEFAULT_THRESHOLD)

    def result_path(self, job):
        if job != "default":
            return self.results / job
        if (BUNDLED_EXAMPLE / "manifest.json").is_file():
            return BUNDLED_EXAMPLE
        return RESULTS / "default"

    def expired(self, job):
        if not self.limits or job == "default":
            return False
        try:
            return self.result_path(job).stat().st_mtime <= time() - WINDOW_SECONDS
        except FileNotFoundError:
            return False

    def prune_results(self):
        for directory in self.results.iterdir():
            if self.expired(directory.name):
                shutil.rmtree(directory, ignore_errors=True)

    def job_state(self, job):
        with self.job_lock:
            active = self.jobs.get(job)
        if active is not None:
            return active
        if not self.expired(job) and (self.result_path(job) / "manifest.json").is_file():
            return {"status": "ready", "manifest": f"/results/{job}/manifest.json"}
        return None


class Handler(BaseHTTPRequestHandler):
    def valid_host(self):
        host = self.headers.get("Host")
        if self.server.public_origin:
            return host == urlsplit(self.server.public_origin).netloc
        port = self.server.server_port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def headers_for(self, code, content_type, length, cache="no-store", retry_after=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()

    def json_response(self, code, data, retry_after=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.headers_for(code, TYPES[".json"], len(body), retry_after=retry_after)
        if self.command != "HEAD":
            self.wfile.write(body)

    def serve_file(self, path, cache="no-cache"):
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            self.json_response(404, {"error": "File not found."})
            return
        with stream:
            self.headers_for(200, TYPES[path.suffix], os.fstat(stream.fileno()).st_size, cache)
            if self.command != "HEAD":
                shutil.copyfileobj(stream, self.wfile)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.valid_host():
            self.json_response(403, {"error": "Open Video analysis through its configured address."})
            return
        path = urlsplit(self.path).path
        if path in {"/", "/style.css", "/app.js", "/gallery.js"}:
            self.serve_file(WEB / ("index.html" if path == "/" else path[1:]))
        elif path == "/api/settings":
            public = self.server.limits is not None
            default = self.server.job_state("default")
            self.json_response(200, {
                "protected": public, "max_upload": MAX_PUBLIC_BYTES if public else MAX_UPLOAD,
                "free_imports": FREE_IMPORTS if public else None,
                "has_default": default is not None and default["status"] != "error",
            })
        elif match := JOB_ROUTE.fullmatch(path):
            state = self.server.job_state(match[1])
            self.json_response(200 if state else 404, state or {"error": "Analysis not found."})
        elif match := RESULT_FILE.fullmatch(path):
            if self.server.expired(match[1]):
                self.json_response(404, {"error": "This analysis has expired."})
            else:
                self.serve_file(self.server.result_path(match[1]) / match[2], "private, max-age=3600")
        else:
            self.json_response(404, {"error": "Page not found."})

    def do_POST(self):
        parsed = urlsplit(self.path)
        origin = self.headers.get("Origin")
        expected_origin = self.server.public_origin or ("http://" + (self.headers.get("Host") or ""))
        if not self.valid_host() or self.headers.get("X-Shotline") != "1" or origin not in (None, expected_origin):
            self.json_response(403, {"error": "Open Video analysis through its configured address."})
            return
        if parsed.path not in {"/api/upload", "/api/youtube"}:
            self.json_response(404, {"error": "Page not found."})
            return

        uploaded = parsed.path == "/api/upload"
        workspace = None
        claimed = False
        job = uuid4().hex
        limits = self.server.limits
        try:
            length = int(self.headers.get("Content-Length", "0"))
            max_upload = MAX_PUBLIC_BYTES if limits else MAX_UPLOAD
            if not 0 < length <= (max_upload if uploaded else 8192):
                size = "250 MB" if limits else "16 GB"
                raise AdmissionError(413, f"Select a video file up to {size}." if uploaded else "Enter a YouTube video URL.")
            threshold = float(parse_qs(parsed.query).get("threshold", [DEFAULT_THRESHOLD])[0])
            if not 0 < threshold <= 100:
                raise VideoError("Cut sensitivity threshold must be greater than 0 and at most 100.")

            self.connection.settimeout(60)
            if uploaded:
                if self.headers.get_content_type() != "application/octet-stream":
                    raise VideoError("Send the video as a binary file.")
                name = unquote(self.headers.get("X-Filename", "Video"))[:250]
            else:
                payload = json.loads(self.rfile.read(length))
                name = youtube_video_url(payload.get("url") if isinstance(payload, dict) else None)

            if limits:
                limits.reserve(job, self.headers.get("X-Real-IP"), self.headers.get("X-Access-Code"))
                claimed = True
            workspace = Path(mkdtemp(dir=self.server.work))
            if uploaded:
                with (workspace / "upload").open("wb") as output:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise VideoError("The video upload was interrupted.")
                        output.write(chunk)
                        remaining -= len(chunk)
            self.server.enqueue(job, workspace, "upload" if uploaded else "youtube", name, threshold)
            claimed = False
            workspace = None
            self.json_response(202, {"id": job})
        except AdmissionError as error:
            self.json_response(error.status, {"error": str(error)}, retry_after=error.retry_after)
        except VideoError as error:
            self.json_response(400, {"error": str(error)})
        except (ValueError, UnicodeError):
            self.json_response(400, {"error": "The analysis request could not be read."})
        except OSError as error:
            logging.warning("Could not receive a video: %s", error)
            try:
                self.json_response(500, {"error": "The server could not receive the video. Check disk space and try again."})
            except OSError:
                pass
        finally:
            if workspace:
                shutil.rmtree(workspace, ignore_errors=True)
            if claimed:
                limits.cancel(job)


def load_access_code():
    configured = os.environ.pop("SHOTLINE_ACCESS_CODE", None)
    if configured is not None:
        return configured
    try:
        return (DATA / "access-code").read_text(encoding="ascii").rstrip("\r\n")
    except FileNotFoundError:
        return ""


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--paperman", action="store_true", help="Download and cache Paperman if the bundled example is missing")
    parser.add_argument("--public-origin", help="HTTPS origin served by the local reverse proxy")
    args = parser.parse_args()
    access_code = None
    if args.public_origin:
        origin = urlsplit(args.public_origin)
        if origin.scheme != "https" or not origin.hostname or origin.username or origin.path not in ("", "/") or origin.query or origin.fragment:
            parser.error("--public-origin must be an HTTPS origin without a path")
        args.public_origin = f"https://{origin.netloc}"
        try:
            access_code = load_access_code()
        except (OSError, UnicodeError):
            parser.error("Could not read the private code file at .shotline/access-code")
        if not re.fullmatch(r"[\x21-\x7e]{24,128}", access_code):
            parser.error("Set a private code in SHOTLINE_ACCESS_CODE or .shotline/access-code using 24–128 printable ASCII characters without spaces")

    try:
        with ShotlineServer(args.port, args.public_origin, access_code) as server:
            if server.limits:
                server.prune_results()
            if args.paperman:
                server.prepare_default()
            print(f"Shotline is running at http://{HOST}:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nShotline stopped.")


if __name__ == "__main__":
    main()
