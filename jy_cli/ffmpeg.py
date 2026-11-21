"""ffmpeg/ffprobe discovery, capability probing, execution and media probing.

Discovery order follows the machine's two Homebrew builds: ``$JY_FFMPEG`` first,
then ffmpeg-full (libass/freetype), then plain ffmpeg, then PATH.  Capabilities
are probed once per process and cached; a verb that needs a filter/encoder the
preferred binary lacks falls through to the next candidate before giving up.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from .envelope import JyError, tail_lines

FFMPEG_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
)
FFPROBE_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe",
    "/opt/homebrew/bin/ffprobe",
)
KEY_FILTERS = (
    "drawtext",
    "subtitles",
    "ass",
    "zscale",
    "palettegen",
    "paletteuse",
    "concat",
    "xfade",
    "setpts",
    "atempo",
    "acrossfade",
    "blend",
    "colorkey",
    "colorbalance",
    "curves",
    "eq",
    "gblur",
    "lut3d",
    "overlay",
    "unsharp",
)
KEY_ENCODERS = (
    "libx264",
    "libx265",
    "libsvtav1",
    "h264_videotoolbox",
    "hevc_videotoolbox",
    "prores_ks",
    "aac",
    "libmp3lame",
    "flac",
    "pcm_s16le",
)

_FILTER_RE = re.compile(r"^\s*[A-Z.]{2,6}\s+([a-zA-Z0-9_]+)\s")
_ENCODER_RE = re.compile(r"^\s*[A-Z.]{3,8}\s+([a-zA-Z0-9_]+)\s")
_XFADE_TRANSITION_RE = re.compile(r"^\s{5,}([a-z0-9_]+)\s+(-?\d+)\s+\.\.FV")

_caps_cache = {}
_xfade_cache = {}
_ffprobe_cache = {}


class Caps:
    """Parsed output of ``ffmpeg -version/-filters/-encoders`` for one binary."""

    __slots__ = ("version", "configuration", "filters", "encoders")

    def __init__(self, version, configuration, filters, encoders):
        self.version = version
        self.configuration = configuration
        self.filters = filters
        self.encoders = encoders

    def has_library(self, name):
        """True when this build's configure line enabled one external library."""
        return f"--enable-{name}" in (self.configuration or "")


class Toolchain:
    """The ffmpeg/ffprobe pair chosen for one invocation."""

    __slots__ = ("ffmpeg", "ffprobe", "caps", "warnings")

    def __init__(self, ffmpeg, ffprobe, caps, warnings):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.caps = caps
        self.warnings = warnings


def _run(argv):
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def candidate_paths():
    """ffmpeg candidates in priority order, de-duplicated by real path."""
    paths = []
    env = os.environ.get("JY_FFMPEG")
    if env:
        paths.append(env)
    paths.extend(FFMPEG_CANDIDATES)
    which = shutil.which("ffmpeg")
    if which:
        paths.append(which)
    seen = set()
    ordered = []
    for path in paths:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            ordered.append(path)
    return ordered


def caps_of(path):
    """Capabilities of one ffmpeg binary, or None when it cannot run."""
    if path in _caps_cache:
        return _caps_cache[path]
    caps = None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        version = _run([path, "-hide_banner", "-version"])
        if version.returncode == 0:
            filters = _names(_run([path, "-hide_banner", "-filters"]), _FILTER_RE)
            encoders = _names(_run([path, "-hide_banner", "-encoders"]), _ENCODER_RE)
            version_lines = (version.stdout or "").strip().splitlines()
            configuration = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in version_lines
                    if line.startswith("configuration:")
                ),
                "",
            )
            caps = Caps(
                version_lines[0] if version_lines else "",
                configuration,
                filters,
                encoders,
            )
    _caps_cache[path] = caps
    return caps


def xfade_transitions(path):
    """Named xfade transitions advertised by one ffmpeg build (``custom`` excluded)."""
    if path in _xfade_cache:
        return _xfade_cache[path]
    names = []
    completed = _run([path, "-hide_banner", "-h", "filter=xfade"])
    if completed.returncode == 0:
        in_block = False
        for line in (completed.stdout or "").splitlines():
            if not in_block:
                if re.match(r"^\s+transition\s+<int>", line):
                    in_block = True
                continue
            match = _XFADE_TRANSITION_RE.match(line)
            if not match:
                break
            if match.group(1) != "custom":
                names.append(match.group(1))
    _xfade_cache[path] = names
    return names


def _names(completed, pattern):
    names = set()
    if completed.returncode != 0:
        return names
    for line in (completed.stdout or "").splitlines():
        match = pattern.match(line)
        if match:
            names.add(match.group(1))
    return names


def ffprobe_for(ffmpeg_path):
    """A working ffprobe paired with the chosen ffmpeg (env, sibling, then PATH)."""
    candidates = []
    env = os.environ.get("JY_FFPROBE")
    if env:
        candidates.append(env)
    candidates.append(os.path.join(os.path.dirname(ffmpeg_path), "ffprobe"))
    candidates.extend(FFPROBE_CANDIDATES)
    which = shutil.which("ffprobe")
    if which:
        candidates.append(which)
    for path in candidates:
        real = os.path.realpath(path)
        if real not in _ffprobe_cache:
            ok = False
            if os.path.isfile(path) and os.access(path, os.X_OK):
                ok = _run([path, "-hide_banner", "-version"]).returncode == 0
            _ffprobe_cache[real] = ok
        if _ffprobe_cache[real]:
            return path
    return None


def discover(filters=(), encoders=()):
    """Pick the first runnable ffmpeg that provides every requested capability."""
    required_filters = set(filters)
    required_encoders = set(encoders)
    candidates = candidate_paths()
    missing = {}
    any_runnable = False
    for index, path in enumerate(candidates):
        caps = caps_of(path)
        if caps is None:
            missing[path] = "not executable or not runnable"
            continue
        any_runnable = True
        lack_filters = sorted(required_filters - caps.filters)
        lack_encoders = sorted(required_encoders - caps.encoders)
        if lack_filters or lack_encoders:
            missing[path] = {
                "missing_filters": lack_filters,
                "missing_encoders": lack_encoders,
            }
            continue
        ffprobe = ffprobe_for(path)
        if not ffprobe:
            missing[path] = "no working ffprobe"
            continue
        warnings = []
        if index > 0:
            warnings.append(
                f"selected {path} instead of {candidates[0]} "
                f"because it lacks a required capability"
            )
        return Toolchain(path, ffprobe, caps, warnings)
    code = "UNSUPPORTED" if any_runnable else "UNAVAILABLE"
    if any_runnable:
        message = (
            "no ffmpeg candidate provides the required capabilities "
            f"(filters={sorted(required_filters)}, encoders={sorted(required_encoders)})"
        )
    else:
        message = "no runnable ffmpeg found (set JY_FFMPEG or install ffmpeg)"
    raise JyError(
        code,
        message,
        details={
            "candidates": missing,
            "required_filters": sorted(required_filters),
            "required_encoders": sorted(required_encoders),
        },
    )


def with_loglevel(argv, verbose):
    """Raise ffmpeg's log level to info when --verbose asked for the logs."""
    if not verbose:
        return argv
    adjusted = list(argv)
    try:
        adjusted[adjusted.index("-loglevel") + 1] = "info"
    except (ValueError, IndexError):
        pass
    return adjusted


def run_ffmpeg(tool, argv, verbose=False):
    """Execute an argv list; raise INTERNAL with the last stderr lines on failure."""
    completed = _run(with_loglevel(argv, verbose))
    if verbose and completed.stderr:
        sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        raise JyError(
            "INTERNAL",
            f"ffmpeg failed with exit code {completed.returncode}",
            details={
                "returncode": completed.returncode,
                "argv": argv,
                "tail": tail_lines(completed.stderr),
            },
        )
    return completed


def probe_media(tool, path):
    """ffprobe one file into the normalized probe dict used across the CLI."""
    argv = [
        tool.ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    completed = _run(argv)
    if completed.returncode != 0:
        raise JyError(
            "INTERNAL",
            f"ffprobe could not read {path}",
            details={
                "returncode": completed.returncode,
                "argv": argv,
                "tail": tail_lines(completed.stderr),
            },
        )
    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise JyError(
            "INTERNAL",
            f"ffprobe returned invalid JSON for {path}",
            details={"error": str(exc)},
        ) from exc
    return _normalize(path, raw)


WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def is_windows_path(value):
    """True for an absolute Windows path (``C:/...`` or ``C:\\...``): media that lives on a remote PC."""
    return isinstance(value, str) and bool(WINDOWS_PATH.match(value))


def probe_media_remote(host, path, timeout=60.0):
    """ffprobe a file that lives on a Windows PC (``ssh HOST``), same dict as :func:`probe_media`.

    The PowerShell command travels as ``-EncodedCommand`` (UTF-16LE base64), so CJK
    paths survive cmd.exe; the path itself is passed single-quoted and checked first.
    """
    import base64

    if not re.match(r"^[A-Za-z0-9._@-]+$", host or ""):
        raise JyError("VALIDATION", f"--media-host 主机名不合法：{host!r}（填 ~/.ssh/config 里的别名，如 gamingpc）")
    pc_path = str(path).replace("\\", "/")
    if not is_windows_path(pc_path) or "'" in pc_path or "\n" in pc_path:
        raise JyError("VALIDATION", f"远程素材路径必须是 Windows 绝对路径（C:/...），且不含单引号：{path!r}")
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; "
        f"$p='{pc_path}'; if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {{ Write-Output 'JYPROBE_MISSING'; exit 3 }}; "
        "& ffprobe -v error -print_format json -show_format -show_streams $p"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR", host,
            f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise JyError("UNAVAILABLE", f"远程 ffprobe 超时（{host}）：{pc_path}", retriable=True) from exc
    out = (done.stdout or b"").decode("utf-8", "replace").lstrip("\ufeff")
    err = (done.stderr or b"").decode("utf-8", "replace")
    if done.returncode == 255:
        raise JyError("UNAVAILABLE", f"连不上 {host}（ssh 失败：{err.strip()[-200:]}）", retriable=True,
                      details={"host": host})
    if "JYPROBE_MISSING" in out:
        raise JyError("NOT_FOUND", f"{host} 上没有这个素材：{pc_path}", details={"host": host})
    if done.returncode != 0:
        raise JyError("INTERNAL", f"{host} 上的 ffprobe 读不了 {pc_path}（PC 装了 ffmpeg 吗？）",
                      details={"host": host, "returncode": done.returncode, "tail": tail_lines(err)})
    try:
        raw = json.loads(out[out.index("{"):] if "{" in out else "{}")
    except json.JSONDecodeError as exc:
        raise JyError("INTERNAL", f"{host} 上的 ffprobe 输出不是 JSON：{pc_path}") from exc
    probe = _normalize(pc_path, raw, local=False)
    probe["host"] = host
    return probe


def _normalize(path, raw, local=True):
    streams = raw.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    fmt = raw.get("format") or {}

    rotation = _rotation(video)
    width = video.get("width") if video else None
    height = video.get("height") if video else None
    if width and height and abs(rotation) % 180 == 90:
        width, height = height, width

    r_rate = video.get("r_frame_rate") if video else None
    avg_rate = video.get("avg_frame_rate") if video else None
    fps_value = _rate(r_rate) or _rate(avg_rate)

    duration = _to_float(fmt.get("duration")) or _to_float(
        video.get("duration") if video else None
    )
    size = int(fmt.get("size")) if str(fmt.get("size", "")).isdigit() else 0
    if not size and local and os.path.isfile(path):
        size = os.path.getsize(path)

    return {
        "path": os.path.abspath(path) if local else path,
        "size_bytes": size,
        "format_name": fmt.get("format_name"),
        "duration_s": round(duration, 3) if duration else 0.0,
        "bitrate": _to_int(fmt.get("bit_rate")) or _to_int(
            video.get("bit_rate") if video else None
        ),
        "codec": video.get("codec_name") if video else None,
        "width": width,
        "height": height,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "rotation": rotation,
        "fps": {
            "r_frame_rate": r_rate,
            "avg_frame_rate": avg_rate,
            "value": round(fps_value, 3) if fps_value else 0.0,
        },
        "audio_streams": [
            {
                "index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "sample_rate": _to_int(stream.get("sample_rate")),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "bitrate": _to_int(stream.get("bit_rate")),
            }
            for stream in audios
        ],
    }


def _rotation(video):
    if not video:
        return 0
    for side_data in video.get("side_data_list") or ():
        if "rotation" in side_data:
            return int(float(side_data["rotation"]))
    tags = video.get("tags") or {}
    if "rotate" in tags:
        return int(float(tags["rotate"]))
    return 0


def _rate(value):
    if not value or value in ("0/0", "N/A"):
        return 0.0
    try:
        numerator, _, denominator = str(value).partition("/")
        num = float(numerator)
        den = float(denominator) if denominator else 1.0
        return num / den if den else 0.0
    except ValueError:
        return 0.0


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def concat_group(probes):
    """Report whether probes share the stream parameters required for -c copy."""
    fields = ("codec", "width", "height", "pix_fmt")
    mismatches = []
    if not probes:
        return {"compatible": False, "key": None, "mismatches": []}
    first = probes[0]
    for probe in probes[1:]:
        for field in fields:
            if probe.get(field) != first.get(field):
                mismatches.append(
                    {
                        "path": probe["path"],
                        "field": field,
                        "expected": first.get(field),
                        "actual": probe.get(field),
                    }
                )
        expected_fps = first.get("fps", {}).get("r_frame_rate")
        actual_fps = probe.get("fps", {}).get("r_frame_rate")
        if expected_fps != actual_fps:
            mismatches.append(
                {
                    "path": probe["path"],
                    "field": "fps",
                    "expected": expected_fps,
                    "actual": actual_fps,
                }
            )
    compatible = all(probe.get("codec") for probe in probes) and not mismatches
    key = None
    if compatible:
        key = "{codec}/{width}x{height}/{fps}/{pix_fmt}".format(
            codec=first.get("codec"),
            width=first.get("width"),
            height=first.get("height"),
            fps=first.get("fps", {}).get("r_frame_rate"),
            pix_fmt=first.get("pix_fmt"),
        )
    return {"compatible": compatible, "key": key, "mismatches": mismatches}


def probe_streams(tool, path):
    """Raw per-stream facts (index/type/codec/dims/disposition) for one file."""
    argv = [
        tool.ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        path,
    ]
    completed = _run(argv)
    if completed.returncode != 0:
        raise JyError(
            "INTERNAL",
            f"ffprobe could not read {path}",
            details={"returncode": completed.returncode, "tail": tail_lines(completed.stderr)},
        )
    try:
        raw = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise JyError("INTERNAL", f"ffprobe returned invalid JSON for {path}") from exc
    return [
        {
            "index": stream.get("index"),
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "disposition": stream.get("disposition") or {},
        }
        for stream in raw.get("streams") or []
    ]


def output_info(tool, path):
    """Post-run verification: ok=true must mean a real, probeable output file."""
    if not os.path.isfile(path):
        raise JyError(
            "INTERNAL",
            "ffmpeg reported success but the output file is missing",
            details={"path": path},
        )
    probe = probe_media(tool, path)
    if not probe.get("codec") and not probe.get("audio_streams"):
        raise JyError(
            "INTERNAL",
            "output is not a playable media file",
            details={"path": path},
        )
    codec = probe["codec"]
    if not codec:
        codec = probe["audio_streams"][0]["codec_name"]
    return {
        "path": probe["path"],
        "size_bytes": probe["size_bytes"],
        "duration_s": probe["duration_s"] or None,
        "codec": codec,
        "width": probe["width"],
        "height": probe["height"],
        "pix_fmt": probe["pix_fmt"],
        "fps": probe["fps"]["value"] or None,
    }


def escape_filter_path(value):
    """Escape a filesystem path for use as a quoted ffmpeg filter option value."""
    return value.replace("\\", "\\\\").replace("'", "\\'")
