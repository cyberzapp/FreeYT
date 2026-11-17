from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


HOST = "127.0.0.1"
PORT = 8765
APP_DIR = Path(__file__).resolve().parent
INDEX_FILE = APP_DIR / "index.html"
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
EXAMPLE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
QUALITY_SORTS = {
    "best": ["ext:mp4:m4a"],
    "1080": ["res:1080", "ext:mp4:m4a"],
    "720": ["res:720", "ext:mp4:m4a"],
    "480": ["res:480", "ext:mp4:m4a"],
}
VIDEO_EXTENSIONS = {".webm", ".mp4"}
PROCESS_MODES = {"convert", "enhance", "both"}
PROCESS_RESOLUTIONS = {"original": None, "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160}
PROCESS_BITRATES = {"compact": 8_000_000, "balanced": 18_000_000, "master": 35_000_000}
MAX_PROCESSING_UPLOAD = 8 * 1024 * 1024 * 1024
ACTIVE_JOB_STATES = {"queued", "uploading", "downloading", "processing"}
MAX_JOB_HISTORY = 50
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

job_lock = threading.Lock()
jobs: dict[str, dict] = {}
output_files: dict[str, Path] = {}


def default_folder() -> str:
    """Return the user's configured Downloads folder without hardcoding machine paths."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw_path, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        expanded = os.path.expandvars(str(raw_path)).strip()
        if expanded:
            return str(Path(expanded))
    except OSError:
        pass
    return str(Path.home() / "Downloads")


def health() -> dict:
    return {
        "yt_dlp": yt_dlp is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "default_url": EXAMPLE_URL,
        "default_folder": default_folder(),
    }


def valid_youtube_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and hostname in ALLOWED_HOSTS
            and parsed.username is None
            and parsed.password is None
            and bool(parsed.path.strip("/"))
        )
    except ValueError:
        return False


def validate_output_dir(folder_value: str) -> Path:
    """Validate and create a local absolute destination directory."""
    if not isinstance(folder_value, str) or not folder_value.strip():
        raise ValueError("Choose a folder to save the video.")
    output_dir = Path(folder_value.strip()).expanduser()
    if not output_dir.is_absolute() or str(output_dir).startswith("\\\\"):
        raise ValueError("Choose an absolute folder on this computer, such as your Downloads folder.")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError("The selected output path is not a folder.")
    return output_dir


def clean_error_detail(error: object, fallback: str) -> str:
    """Normalize an exception message to a single clean, ANSI-free line."""
    raw = ANSI_ESCAPE_RE.sub("", str(error)).strip()
    if not raw:
        return fallback
    return raw.splitlines()[0].strip()[:300] or fallback


def create_job(kind: str, state: str, message: str) -> str | None:
    """Create a new job if no active job of the same kind is already running."""
    with job_lock:
        if any(job["kind"] == kind and job["state"] in ACTIVE_JOB_STATES for job in jobs.values()):
            return None
        if len(jobs) >= MAX_JOB_HISTORY:
            for stale_id in [
                jid for jid, job in jobs.items() if job["state"] not in ACTIVE_JOB_STATES
            ][: len(jobs) - MAX_JOB_HISTORY + 1]:
                jobs.pop(stale_id, None)
                output_files.pop(stale_id, None)
        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "state": state,
            "progress": 0,
            "speed": None,
            "eta": None,
            "message": message,
        }
        return job_id


def update_job(job_id: str, **values) -> None:
    with job_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def get_job(job_id: str) -> dict | None:
    with job_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def download_video(job_id: str, video_url: str, output_dir: Path, quality: str) -> None:
    use_ffmpeg = shutil.which("ffmpeg") is not None
    selected_format = "bestvideo+bestaudio/best" if use_ffmpeg else "best"
    saved_name: str | None = None

    def progress_hook(info: dict) -> None:
        nonlocal saved_name
        status = info.get("status")
        info_dict = info.get("info_dict") or {}
        if info.get("filename"):
            saved_name = Path(info["filename"]).name
        if status == "downloading":
            total = info.get("total_bytes") or info.get("total_bytes_estimate")
            downloaded = info.get("downloaded_bytes", 0)
            percent = min(100.0, round(downloaded * 100 / total, 1)) if total else None
            speed = info.get("speed")
            eta = info.get("eta")
            has_video = info_dict.get("vcodec") not in (None, "none")
            has_audio = info_dict.get("acodec") not in (None, "none")
            if has_video and not has_audio:
                label = "Downloading video stream…"
            elif has_audio and not has_video:
                label = "Downloading audio stream…"
            else:
                label = "Downloading video…"
            update_job(
                job_id,
                state="downloading",
                progress=percent,
                speed=f"{speed / 1_000_000:.1f} MB/s" if speed else None,
                eta=f"{int(eta)} sec" if eta is not None else None,
                message=label,
            )
        elif status == "finished":
            update_job(
                job_id,
                state="processing",
                progress=100,
                speed=None,
                eta=None,
                message="Finalizing the video file…",
            )

    def postprocessor_hook(info: dict) -> None:
        nonlocal saved_name
        filepath = (info.get("info_dict") or {}).get("filepath")
        if filepath:
            saved_name = Path(filepath).name
        if info.get("status") == "started":
            update_job(
                job_id,
                state="processing",
                progress=100,
                speed=None,
                eta=None,
                message="Merging video and audio…",
            )

    options = {
        "format": selected_format,
        "format_sort": QUALITY_SORTS[quality],
        "outtmpl": str(output_dir / "%(title).200B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "retries": 3,
        "fragment_retries": 3,
    }

    try:
        update_job(job_id, state="downloading", message="Connecting to YouTube…")
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([video_url])
        location_note = f"Saved {saved_name} in {output_dir}" if saved_name else f"Saved in {output_dir}"
        update_job(
            job_id,
            state="done",
            progress=100,
            speed=None,
            eta=None,
            message=f"Download complete. {location_note}",
        )
    except Exception as error:
        detail = clean_error_detail(error, "Unknown download error.")
        detail_lower = detail.lower()
        if "sign in to confirm your age" in detail_lower or "age-restricted" in detail_lower:
            detail = "YouTube requires sign-in or age verification for this video. This app does not access account cookies; try a video available without sign-in."
        elif "sign in to confirm you're not a bot" in detail_lower or "cookies" in detail_lower:
            detail = "YouTube requested browser verification for this request. Try again shortly or try a different public video."
        elif "ffmpeg" in detail_lower or "ffprobe" in detail_lower:
            detail = "Install FFmpeg and add it to PATH to download the selected quality. Then restart this app."
        update_job(
            job_id,
            state="error",
            speed=None,
            eta=None,
            message=detail,
        )


def _parse_probe_duration(metadata: dict, video_stream: dict) -> float:
    for candidate in (metadata.get("format", {}).get("duration"), video_stream.get("duration")):
        try:
            value = float(candidate)
            if math.isfinite(value) and value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 0.0


def _drain_stream(stream, lines: list[str]) -> None:
    try:
        for raw_line in stream:
            cleaned = raw_line.strip()
            if cleaned:
                lines.append(cleaned)
                if len(lines) > 10:
                    del lines[:-10]
    finally:
        stream.close()


def process_video(job_id: str, source_path: Path, temp_dir: Path, output_dir: Path, source_name: str, settings: dict) -> None:
    output_path: Path | None = None
    process: subprocess.Popen | None = None
    try:
        update_job(job_id, state="processing", progress=0, message="Inspecting the video…")
        try:
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,duration:format=duration", "-of", "json", str(source_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as error:
            detail = clean_error_detail(error.stderr or error.stdout, "Could not inspect this video file.")
            raise ValueError(detail) from error

        metadata = json.loads(probe.stdout)
        streams = metadata.get("streams") or []
        if not streams:
            raise ValueError("No video stream was found in this file.")
        video_stream = streams[0]
        input_width = int(video_stream.get("width") or 0)
        input_height = int(video_stream.get("height") or 0)
        duration = _parse_probe_duration(metadata, video_stream)
        if input_width < 1 or input_height < 1:
            raise ValueError("Could not read the video's dimensions.")

        mode = settings["mode"]
        resolution = settings["resolution"]
        target_res = None if mode == "convert" or resolution == "original" else PROCESS_RESOLUTIONS[resolution]
        short_edge = min(input_width, input_height)
        output_label = resolution if target_res is not None else f"{short_edge}p"
        source_extension = Path(source_name).suffix.lower()
        output_container = "mp4" if mode in {"convert", "both"} else (
            source_extension[1:] if settings["container"] == "keep" else "mp4"
        )
        filename_base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(source_name).stem).strip(" .") or "video"
        if mode == "convert":
            suffix = "converted"
        elif mode == "enhance":
            suffix = f"enhanced_{output_label}"
        else:
            suffix = f"converted_enhanced_{output_label}"
        output_path = output_dir / f"{filename_base}_{suffix}.{output_container}"
        version = 2
        while output_path.exists():
            output_path = output_dir / f"{filename_base}_{suffix}_{version}.{output_container}"
            version += 1

        filters = []
        if mode != "convert":
            if target_res is not None:
                if input_width >= input_height:
                    if target_res != input_height:
                        filters.append(f"scale=-2:{target_res}:flags=lanczos")
                elif target_res != input_width:
                    filters.append(f"scale={target_res}:-2:flags=lanczos")
            if settings["denoise"]:
                filters.append("hqdn3d=1.2:1.2:4.5:4.5")
            contrast = 1 + settings["contrast"] * 0.0045
            saturation = 1 + settings["vibrance"] * 0.0055
            brightness = settings["contrast"] * 0.0006
            if settings["contrast"] or settings["vibrance"]:
                filters.append(f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}:brightness={brightness:.3f}")
            if settings["sharpen"]:
                amount = settings["sharpen"] / 100 * 1.35
                filters.append(f"unsharp=5:5:{amount:.2f}:5:5:0")

        # libx264 with yuv420p requires even width and height.
        if output_container == "mp4" and not any(f.startswith("scale=") for f in filters):
            if input_width % 2 != 0 or input_height % 2 != 0:
                filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

        bitrate = PROCESS_BITRATES[settings["quality"]]
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(source_path),
            "-map", "0:v:0", "-map", "0:a?",
        ]
        if filters:
            command.extend(["-vf", ",".join(filters)])
        if output_container == "mp4":
            command.extend(["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-b:v", str(bitrate)])
            command.extend(["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
        else:
            command.extend(["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4", "-b:v", str(bitrate)])
            command.extend(["-c:a", "libopus", "-b:a", "160k"])
        command.extend(["-progress", "pipe:1", "-nostats", str(output_path)])

        action = "Converting to MP4" if mode == "convert" else "Upscaling and enhancing" if mode == "enhance" else "Converting and enhancing"
        update_job(job_id, message=f"{action}…", output_path=str(output_dir), output_name=output_path.name)
        started = time.monotonic()
        error_lines: list[str] = []
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        stderr_thread = threading.Thread(target=_drain_stream, args=(process.stderr, error_lines), daemon=True)
        stderr_thread.start()

        for line in process.stdout:
            key, separator, value = line.strip().partition("=")
            if not separator or key not in {"out_time_ms", "out_time_us"}:
                continue
            raw_us = value.strip()
            if not raw_us.isdigit():
                continue
            micros = int(raw_us)
            if duration > 0:
                percent = max(0.0, min(99.0, round(micros / 1_000_000 / duration * 100, 1)))
                elapsed = max(0.1, time.monotonic() - started)
                eta = max(0, int(elapsed * (100 - percent) / max(percent, 0.1)))
                update_job(job_id, progress=percent, eta=f"{eta} sec", message=f"{action}…")
            else:
                update_job(job_id, message=f"{action} ({micros // 1_000_000}s processed)…")

        return_code = process.wait()
        stderr_thread.join(timeout=2)
        if return_code:
            raise RuntimeError(" ".join(error_lines)[-300:] or "FFmpeg could not process this video.")

        with job_lock:
            output_files[job_id] = output_path
        update_job(
            job_id,
            state="done",
            progress=100,
            eta=None,
            message=f"Finished. Saved {output_path.name} in {output_dir}",
            output_path=str(output_dir),
            output_name=output_path.name,
        )
    except Exception as error:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if output_path and output_path.exists():
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
        detail = clean_error_detail(error, "Unknown processing error.")
        update_job(job_id, state="error", eta=None, message=detail)
    finally:
        if process:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


class LocalHTTPServer(ThreadingHTTPServer):
    """HTTP server that binds exclusively to the local port even on Windows."""

    allow_reuse_address = False


class DownloaderHandler(BaseHTTPRequestHandler):
    server_version = "FreeYT"
    sys_version = ""

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def _send(self, status: int, payload: dict | bytes, content_type: str = "application/json; charset=utf-8") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _local_request(self) -> bool:
        host_header = self.headers.get("Host", "")
        try:
            host = urlsplit(f"http://{host_header}")
            if host.hostname not in {"127.0.0.1", "localhost"} or host.port != PORT:
                return False
            origin = self.headers.get("Origin")
            if origin:
                origin_parts = urlsplit(origin)
                if origin_parts.scheme != "http" or origin_parts.netloc.lower() != host_header.lower():
                    return False
            return True
        except ValueError:
            return False

    def do_GET(self) -> None:
        if not self._local_request():
            self._send(403, {"error": "Local requests only."})
            return

        parsed = urlsplit(self.path)
        if parsed.path == "/api/health":
            self._send(200, health())
            return
        if parsed.path == "/api/status":
            requested_id = parse_qs(parsed.query).get("id", [""])[0]
            snapshot = get_job(requested_id)
            self._send(200 if snapshot else 404, snapshot or {"error": "Job status not found."})
            return
        if parsed.path == "/api/output":
            self._serve_output(parse_qs(parsed.query).get("id", [""])[0])
            return
        if parsed.path in {"/", "/index.html"}:
            try:
                self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, {"error": "The website files could not be read."})
            return
        self._send(404, {"error": "Page not found."})

    def do_POST(self) -> None:
        if not self._local_request():
            self._send(403, {"error": "Local requests only."})
            return
        request_path = urlsplit(self.path).path
        if request_path == "/api/process":
            self._process_video_request()
            return
        if request_path != "/api/download":
            self._send(404, {"error": "Page not found."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 1 or content_length > 16_384:
                raise ValueError("Request is empty or too large.")
            request = json.loads(self.rfile.read(content_length))
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "Enter valid download details."})
            return

        video_url = request.get("url", "") if isinstance(request, dict) else ""
        folder_value = request.get("folder", "") if isinstance(request, dict) else ""
        quality = request.get("quality", "best") if isinstance(request, dict) else "best"

        if not isinstance(video_url, str) or not valid_youtube_url(video_url):
            self._send(400, {"error": "Paste a valid YouTube video link using https://youtube.com or https://youtu.be."})
            return
        if quality not in QUALITY_SORTS:
            self._send(400, {"error": "Choose one of the listed quality options."})
            return
        if yt_dlp is None:
            self._send(503, {"error": "yt-dlp is not installed yet. Close this window and run start.bat again with an internet connection."})
            return

        try:
            output_dir = validate_output_dir(folder_value)
        except (OSError, ValueError) as error:
            self._send(400, {"error": str(error) or "The output folder could not be opened."})
            return

        job_id = create_job("download", "queued", "Preparing your download…")
        if job_id is None:
            self._send(409, {"error": "A download is already in progress. Wait for it to finish."})
            return

        threading.Thread(
            target=download_video,
            args=(job_id, video_url.strip(), output_dir, quality),
            daemon=True,
        ).start()
        self._send(202, {"id": job_id})

    def _process_video_request(self) -> None:
        params = parse_qs(urlsplit(self.path).query)
        filename = params.get("filename", [""])[0].strip()
        folder_value = params.get("folder", [""])[0].strip()
        mode = params.get("mode", ["both"])[0]
        resolution = params.get("resolution", ["1080p"])[0]
        container = params.get("container", ["keep"])[0]
        quality = params.get("quality", ["balanced"])[0]
        try:
            settings = {
                "mode": mode,
                "resolution": resolution,
                "container": container,
                "quality": quality,
                "sharpen": int(params.get("sharpen", ["45"])[0]),
                "contrast": int(params.get("contrast", ["30"])[0]),
                "vibrance": int(params.get("vibrance", ["20"])[0]),
                "denoise": params.get("denoise", ["1"])[0] in {"1", "true"},
            }
        except ValueError:
            self._send(400, {"error": "One of the video processing settings is invalid."})
            return

        extension = Path(filename).suffix.lower()
        if not filename or len(filename) > 255 or extension not in VIDEO_EXTENSIONS:
            self._send(400, {"error": "Choose a WEBM or MP4 video file."})
            return
        if mode not in PROCESS_MODES or resolution not in PROCESS_RESOLUTIONS or container not in {"keep", "mp4"} or quality not in PROCESS_BITRATES:
            self._send(400, {"error": "Choose a valid processing mode, format, resolution, and quality."})
            return
        if any(not 0 <= settings[name] <= 100 for name in ("sharpen", "contrast", "vibrance")):
            self._send(400, {"error": "Filter levels must be between 0 and 100."})
            return
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self._send(503, {"error": "FFmpeg and ffprobe are required. Install FFmpeg, add it to PATH, then restart this app."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 1 or content_length > MAX_PROCESSING_UPLOAD:
                raise ValueError("Choose a video smaller than 8 GB.")
            output_dir = validate_output_dir(folder_value)
        except (OSError, ValueError) as error:
            self._send(400, {"error": str(error) or "The output folder could not be opened."})
            return

        job_id = create_job("process", "uploading", "Sending the video to the local processor…")
        if job_id is None:
            self._send(409, {"error": "Another local video process is already in progress."})
            return

        temp_dir = Path(tempfile.mkdtemp(prefix="freeyt-"))
        source_path = temp_dir / f"source{extension}"
        try:
            remaining = content_length
            received = 0
            with source_path.open("wb") as source_file:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("The video upload ended unexpectedly.")
                    source_file.write(chunk)
                    received += len(chunk)
                    remaining -= len(chunk)
                    update_job(job_id, progress=round(received * 100 / content_length, 1))
        except OSError as error:
            shutil.rmtree(temp_dir, ignore_errors=True)
            update_job(job_id, state="error", message=clean_error_detail(error, "The video could not be uploaded."))
            self._send(400, {"error": str(error) or "The video could not be uploaded."})
            return

        update_job(job_id, state="queued", progress=0, message="Video received. Preparing local processing…")
        threading.Thread(
            target=process_video,
            args=(job_id, source_path, temp_dir, output_dir, filename, settings),
            daemon=True,
        ).start()
        self._send(202, {"id": job_id})

    def _serve_output(self, job_id: str) -> None:
        with job_lock:
            output_path = output_files.get(job_id)
        if not output_path or not output_path.is_file():
            self._send(404, {"error": "Processed video is no longer available."})
            return

        file_size = output_path.stat().st_size
        start = 0
        end = file_size - 1
        status = 200
        range_header = self.headers.get("Range", "")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), end) if match.group(2) else end
            else:
                suffix_length = int(match.group(2))
                start = max(0, file_size - suffix_length)
            if start > end or start >= file_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206

        chunk_count = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(output_path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(chunk_count))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(output_path.name)}")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        try:
            with output_path.open("rb") as output_file:
                output_file.seek(start)
                remaining = chunk_count
                while remaining:
                    chunk = output_file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    try:
        server = LocalHTTPServer((HOST, PORT), DownloaderHandler)
    except OSError as error:
        print(f"Could not start the local website: {error}")
        input("Press Enter to close…")
        return

    print(f"FreeYT is ready at http://{HOST}:{PORT}")
    print("Only your computer can connect. Close this window to stop the app.")
    if yt_dlp is None:
        print("yt-dlp is not installed. Run start.bat with an internet connection to install it.")
    elif shutil.which("ffmpeg") is None:
        print("FFmpeg is not installed. Downloads will use available single-file video formats.")
    try:
        import webbrowser

        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
