"""Shared FFmpeg analysis and frame extraction for the Shotline commands."""

from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import dataclass
from mmap import ACCESS_READ, mmap
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
import json
import math
import shutil
import subprocess

DEFAULT_THRESHOLD = 10.0
DEFAULT_MIN_SHOT = 0.16
CONTEXT_SECONDS = 0.13
REFERENCE_WIDTH = 32
REFERENCE_HEIGHT = 18
LUMA_BYTES = REFERENCE_WIDTH * REFERENCE_HEIGHT
REFERENCE_BYTES = LUMA_BYTES * 3 // 2
VIDEO_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,flv,mpeg,mpegts,ogg,asf,mxf"
INPUT_OPTIONS = ["-protocol_whitelist", "file", "-format_whitelist", VIDEO_FORMATS]


class VideoError(Exception):
    """An error that can be shown to the person analyzing a video."""


@dataclass(frozen=True)
class Shot:
    number: int
    start_frame: int
    end_frame: int
    frame: int
    start: float
    end: float
    screenshot_time: float

    @property
    def filename(self):
        return f"shot-{self.number:06d}.jpg"

    def details(self):
        return {
            "number": self.number,
            "start": self.start,
            "end": self.end,
            "screenshot_time": self.screenshot_time,
        }


@dataclass(frozen=True)
class Timeline:
    duration: float
    times: list[float]
    shots: list[Shot]
    pixels: bytes


def run_media(tool, arguments, **kwargs):
    executable = shutil.which(tool)
    if not executable:
        raise VideoError(f"{tool} is required. Install FFmpeg and make sure it is on your PATH.")
    try:
        result = subprocess.run(
            [executable, *map(str, arguments)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
        )
    except subprocess.TimeoutExpired as error:
        raise VideoError("The analysis ran out of processing time.") from error
    if result.returncode:
        lines = result.stderr.decode(errors="replace").strip().splitlines()
        raise VideoError(lines[-1] if lines else f"{tool} exited with code {result.returncode}.")
    return result.stdout


def ffmpeg(arguments, **kwargs):
    return run_media(
        "ffmpeg", ["-hide_banner", "-nostdin", "-v", "error", "-xerror", *arguments], **kwargs
    )


def probe_video(video, timeout=None):
    try:
        payload = run_media("ffprobe", [
            "-v", "error", *INPUT_OPTIONS, "-select_streams", "V:0", "-show_entries",
            "stream=duration,avg_frame_rate:format=duration", "-of", "json", video,
        ], timeout=timeout)
    except VideoError as error:
        raise VideoError(f"Could not read this video. {error}") from error
    information = json.loads(payload)
    if not information.get("streams"):
        raise VideoError("This file does not contain a video track.")
    return information["streams"][0], information.get("format", {})


def positive_duration(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def end_time(stream, container, times):
    # A single video-track duration is preferable to a container with extra audio.
    duration = positive_duration(stream.get("duration"))
    if duration is not None and duration > times[-1]:
        return duration

    recent = times[-31:]
    steps = [later - earlier for earlier, later in zip(recent, recent[1:]) if later > earlier]
    rate = stream.get("avg_frame_rate", "0/1")
    numerator, denominator = map(int, rate.split("/"))
    frame_duration = median(steps) if steps else (denominator / numerator if numerator else 1 / 30)
    estimate = times[-1] + frame_duration
    duration = positive_duration(container.get("duration"))
    if duration is not None and times[-1] < duration <= estimate + 2 * frame_duration:
        return duration
    return estimate


def read_frame_scores(log):
    times, scores, motion = [], [], []
    for line in log:
        if line.startswith("frame:"):
            times.append(int(line.split()[1].split(":")[1]) / 1_000_000)
        elif line.startswith("lavfi.scd.score="):
            scores.append(float(line.partition("=")[2]))
        elif line.startswith("lavfi.scd.mafd="):
            motion.append(float(line.partition("=")[2]))
    return times, scores, motion


def same_view(reference, first, second):
    first *= REFERENCE_BYTES
    second *= REFERENCE_BYTES
    a = reference[first:first + REFERENCE_BYTES]
    b = reference[second:second + REFERENCE_BYTES]
    y_a, y_b = a[:LUMA_BYTES], b[:LUMA_BYTES]
    mean_a, mean_b = sum(y_a) / LUMA_BYTES, sum(y_b) / LUMA_BYTES
    if abs(mean_a - mean_b) > 10:
        return False
    chroma_size = LUMA_BYTES // 4
    for start in (LUMA_BYTES, LUMA_BYTES + chroma_size):
        end = start + chroma_size
        if abs(sum(a[start:end]) - sum(b[start:end])) > 8 * chroma_size:
            return False
    centered_a = [value - mean_a for value in y_a]
    centered_b = [value - mean_b for value in y_b]
    energy = sum(value * value for value in centered_a) * sum(value * value for value in centered_b)
    if energy == 0:
        return False
    covariance = sum(left * right for left, right in zip(centered_a, centered_b))
    return covariance >= 0.9 * math.sqrt(energy)


def is_cut(frame, times, score, motion, reference):
    # Strong discontinuities are left to FFmpeg. Borderline spikes need context
    # because repeated video frames can make ordinary motion appear abrupt.
    if score >= 20:
        return True
    time = times[frame]
    first = bisect_left(times, time - CONTEXT_SECONDS)
    last = bisect_left(times, time + CONTEXT_SECONDS)
    before = max(motion[first:frame], default=0.0)
    after = max(motion[frame + 1:last], default=0.0)
    jump = motion[frame]
    if min(before, after) >= 0.30 * jump and before + after >= 1.15 * jump:
        return False
    if max(before, after) < 0.55 * jump:
        return True

    # A brief object passing in front of the camera can hide and then reveal
    # the same shot. Check surrounding frames for the view returning.
    step = max(1, (last - first) // 10)
    return not any(
        same_view(reference, left, right)
        for left in range(frame - 1, first - 1, -step)
        for right in range(frame, last, step)
    )


def inspect_video(
    video, threshold=DEFAULT_THRESHOLD, min_shot=DEFAULT_MIN_SHOT, with_colors=False,
    *, max_duration=None, time_remaining=lambda: None,
):
    if not 0 < threshold <= 100:
        raise VideoError("Cut sensitivity threshold must be greater than 0 and at most 100.")
    if not math.isfinite(min_shot) or min_shot < 0:
        raise VideoError("Minimum shot length must be zero or greater.")
    video = Path(video).resolve()
    stream, container = probe_video(video, time_remaining())
    max_duration_message = f"Choose a video that's {max_duration // 60} minutes or shorter." if max_duration else ""
    if max_duration is not None and (positive_duration(stream.get("duration")) or 0) > max_duration:
        raise VideoError(max_duration_message)
    detection = (
        "[0:V:0]settb=AVTB,setpts=PTS-STARTPTS,scale=192:108:flags=area,format=yuv444p,"
        f"scdet=t={threshold},metadata=print:file=frames.log"
    )
    reference = f"scale={REFERENCE_WIDTH}:{REFERENCE_HEIGHT}:flags=area,format=yuv420p[reference]"
    if with_colors:
        filters = (
            f"{detection},split=2[context][palette];[context]{reference};"
            "[palette]format=rgb24,scale=1:1:flags=area,format=rgb24[color]"
        )
    else:
        filters = f"{detection},{reference}"
    output = ["-map", "[reference]", "-fps_mode", "passthrough", "-c:v", "rawvideo", "-f", "rawvideo", "reference.yuv"]
    if with_colors:
        output += ["-map", "[color]", "-fps_mode", "passthrough", "-c:v", "rawvideo", "-f", "rawvideo", "-"]

    cuts = [0]
    with TemporaryDirectory(prefix="shotline-") as temporary:
        folder = Path(temporary)
        # Decode slightly past the limit so videos with missing or inaccurate metadata can be rejected.
        bound = ["-t", max_duration + 1] if max_duration is not None else []
        pixels = ffmpeg(
            [*INPUT_OPTIONS, *bound, "-i", video, "-filter_complex", filters, *output],
            cwd=folder, timeout=time_remaining(),
        )
        with (folder / "frames.log").open() as log:
            times, scores, motion = read_frame_scores(log)
        if not times:
            raise VideoError("No video frames could be decoded.")
        if len(scores) != len(times) or len(motion) != len(times):
            raise VideoError("FFmpeg did not return a cut score for every frame.")
        duration = end_time(stream, container, times)
        if max_duration is not None and duration > max_duration:
            raise VideoError(max_duration_message)
        with (folder / "reference.yuv").open("rb") as frames:
            if (folder / "reference.yuv").stat().st_size != len(times) * REFERENCE_BYTES:
                raise VideoError("FFmpeg did not return a reference image for every frame.")
            with mmap(frames.fileno(), 0, access=ACCESS_READ) as reference_frames:
                for frame, score in enumerate(scores[1:], 1):
                    timestamp = times[frame]
                    if score < threshold or timestamp <= times[frame - 1]:
                        continue
                    if timestamp - times[cuts[-1]] >= min_shot:
                        if is_cut(frame, times, score, motion, reference_frames):
                            cuts.append(frame)

    if with_colors and len(pixels) != len(times) * 3:
        raise VideoError("FFmpeg did not return a color for every decoded frame.")

    shots = []
    for start_frame, end_frame in zip(cuts, [*cuts[1:], len(times)]):
        start = times[start_frame]
        end = times[end_frame] if end_frame < len(times) else duration
        midpoint = (start + end) / 2
        frame = min(end_frame - 1, bisect_left(times, midpoint, start_frame, end_frame))
        if frame > start_frame and midpoint - times[frame - 1] <= times[frame] - midpoint:
            frame -= 1
        shots.append(Shot(len(shots) + 1, start_frame, end_frame, frame, start, end, times[frame]))
    return Timeline(duration, times, shots, pixels)


def next_frame_expression(shots, first, last):
    # selected_n is the number of frames already chosen by the select filter.
    # A balanced expression finds the next requested frame in logarithmic time.
    if last - first == 1:
        return str(shots[first].frame)
    midpoint = (first + last) // 2
    left = next_frame_expression(shots, first, midpoint)
    right = next_frame_expression(shots, midpoint, last)
    return f"if(lt(selected_n,{midpoint}),{left},{right})"


def extract_frames(video, timeline, folder, thumbnails=False, *, timeout=None):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if thumbnails:
        (folder / "thumbs").mkdir(exist_ok=True)
    expression = next_frame_expression(timeline.shots, 0, len(timeline.shots))
    selection = (
        f"[0:V:0]select='eq(n,{expression})',"
        "scale=w='max(2,trunc(iw*sar/2)*2)':h=ih:flags=lanczos,setsar=1"
    )
    if thumbnails:
        graph = (
            f"{selection},split=2[full][small];"
            "[small]scale=600:338:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:flags=lanczos[thumb]"
        )
    else:
        graph = f"{selection}[full]"

    with TemporaryDirectory(prefix="shotline-") as temporary:
        graph_file = Path(temporary) / "frames.ffgraph"
        graph_file.write_text(graph, encoding="utf-8")
        args = [
            *INPUT_OPTIONS, "-i", Path(video).resolve(), "-filter_complex_script", graph_file,
            "-map", "[full]", "-fps_mode", "passthrough", "-q:v", "2",
            "-frames:v", len(timeline.shots), folder / "shot-%06d.jpg",
        ]
        if thumbnails:
            args += [
                "-map", "[thumb]", "-fps_mode", "passthrough", "-q:v", "3", "-frames:v", len(timeline.shots),
                folder / "thumbs" / "shot-%06d.jpg",
            ]
        ffmpeg(["-y", *args], timeout=timeout)

    if not (folder / timeline.shots[-1].filename).is_file():
        raise VideoError("FFmpeg did not produce a screenshot for every shot.")
    if thumbnails and not (folder / "thumbs" / timeline.shots[-1].filename).is_file():
        raise VideoError("FFmpeg did not produce a thumbnail for every shot.")


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def command_arguments(parser, default_output):
    parser.add_argument("video", type=Path, help="Path to a local video")
    parser.add_argument("-o", "--output", type=Path, default=Path(default_output), help="New output directory")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Cut score (0–100). Lower finds more cuts.")
    parser.add_argument("--min-shot", type=float, default=DEFAULT_MIN_SHOT, help="Minimum gap between cuts, in seconds.")
    return parser.parse_args()


@contextmanager
def output_directory(output):
    try:
        output.mkdir(parents=True)
    except FileExistsError as error:
        raise VideoError(f"Output already exists. Choose a new directory: {output}") from error
    try:
        yield output
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
