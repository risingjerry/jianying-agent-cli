"""The jy verbs: doctor, probe, trim, concat, speed, extract-audio, frame,
export, gif, subtitle, transition, text, montage, filter, key, beautify.

Each handler returns ``(outcome, data)``; failures raise :class:`JyError`.
No handler prints to stdout — the envelope emitter in ``cli`` owns that.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import effects, exportspec, ffmpeg, jianying
from .envelope import JyError

# Honesty notes: these verbs approximate 剪映 looks with global ffmpeg filters
# and must never be described as face-aware / smart-matte behaviour.
BEAUTIFY_NOTE = (
    "global soft-skin look (blur blend + brightness + unsharp), not face detection: "
    "it smooths the whole frame. True 美颜/磨皮 needs the 剪映 (VESDK) engine."
)
KEY_NOTE = (
    "chroma key (色度抠图) on one colour only; 智能抠像/自定义抠像/抠像描边 "
    "need the 剪映 (VESDK) engine."
)


class Context:
    """Per-invocation state: chosen toolchain, warnings, verbosity."""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.tool = None
        self.warnings = []

    @property
    def ffmpeg_path(self):
        return self.tool.ffmpeg if self.tool else None

    def require(self, filters=(), encoders=()):
        tool = ffmpeg.discover(filters=filters, encoders=encoders)
        self.tool = tool
        for warning in tool.warnings:
            if warning not in self.warnings:
                self.warnings.append(warning)
        return tool


def doctor(ctx, args):
    """Report both installed builds, their capabilities and the selected pair."""
    rows = []
    matrix = {name: [] for name in ffmpeg.KEY_FILTERS + ffmpeg.KEY_ENCODERS}
    for path in ffmpeg.candidate_paths():
        caps = ffmpeg.caps_of(path)
        row = {
            "path": path,
            "exists": os.path.isfile(path),
            "runnable": caps is not None,
            "version": caps.version if caps else None,
            "ffprobe": ffmpeg.ffprobe_for(path) if caps else None,
            "missing_filters": [],
            "missing_encoders": [],
        }
        if caps:
            row["missing_filters"] = [
                name for name in ffmpeg.KEY_FILTERS if name not in caps.filters
            ]
            row["missing_encoders"] = [
                name for name in ffmpeg.KEY_ENCODERS if name not in caps.encoders
            ]
            for name in ffmpeg.KEY_FILTERS:
                if name in caps.filters:
                    matrix[name].append(path)
            for name in ffmpeg.KEY_ENCODERS:
                if name in caps.encoders:
                    matrix[name].append(path)
        rows.append(row)

    selected = {"ffmpeg": None, "ffprobe": None}
    try:
        tool = ffmpeg.discover()
        ctx.tool = tool
        ctx.warnings.extend(tool.warnings)
        selected = {"ffmpeg": tool.ffmpeg, "ffprobe": tool.ffprobe}
    except JyError as exc:
        ctx.warnings.append(exc.message)

    effects_report = {
        name: {"available": bool(matrix.get(name)), "candidates": matrix.get(name) or []}
        for name in (
            "ass",
            "subtitles",
            "lut3d",
            "unsharp",
            "colorkey",
            "blend",
            "gblur",
            "xfade",
            "acrossfade",
        )
    }
    libass = [
        path
        for path in ffmpeg.candidate_paths()
        if ffmpeg.caps_of(path) and ffmpeg.caps_of(path).has_library("libass")
    ]
    effects_report["libass"] = {"available": bool(libass), "candidates": libass}
    if selected["ffmpeg"]:
        transitions = ffmpeg.xfade_transitions(selected["ffmpeg"])
        effects_report["xfade"]["transition_count"] = len(transitions)
        effects_report["xfade"]["transitions"] = transitions

    # Lazy imports: these modules import commands, so importing them at module
    # level would be circular.
    from . import focus as focus_module, jobs as jobs_module
    from .verbs import asr as asr_verbs
    from .verbs import grade as grade_verbs
    from .verbs import sfx as sfx_verbs

    hdr_report = {"tonemap": None}
    if ctx.tool:
        hdr_report["tonemap"] = grade_verbs.tonemap_supported(ctx.tool)

    data = {
        "selected": selected,
        "env": {
            "JY_FFMPEG": os.environ.get("JY_FFMPEG"),
            "JY_FFPROBE": os.environ.get("JY_FFPROBE"),
        },
        "candidates": rows,
        "capability_matrix": {name: paths for name, paths in matrix.items() if paths},
        "key_filters": list(ffmpeg.KEY_FILTERS),
        "key_encoders": list(ffmpeg.KEY_ENCODERS),
        "effects": effects_report,
        "hdr": hdr_report,
        "focus": focus_module.state(),
        "jobs": jobs_module.jobs_state(),
        "asr": asr_verbs.state(),
        "sfx": sfx_verbs.doctor_block(),
        "registry": getattr(ctx, "registry", {}),
        "entitlements": {
            "tier_readable_offline": False,
            "tiers": ["VIP", "SVIP"],
            "reason": "剪映的会员档位（VIP / SVIP）只在服务端；本机没有任何可读缓存，"
            "jy 也不会去猜。",
            "checked": [
                "Config/vipRemindViewedInfo.ini（只是 UI「已看过提醒」标记，不含档位）",
                "login cookie / 账号上下文（只用于 accountScope，不含档位）",
            ],
            "hint": "在剪映里看该素材的角标或导出提示，确认它需要 VIP 还是 SVIP；"
            "被拒时 jy 会点名具体素材 id / 中文名 + native_error，但档位本身只能看剪映界面。",
            "blocked_error_codes": [
                "film_export_entitlement_denied",
                "film_export_entitlement_bypass_forbidden",
                "film_export_entitlement_or_preflight_denied",
            ],
        },
        "jianying": {
            **jianying.app_state(),
            # three reactions, three remedies: open the app / restart it / go ahead
            "bridge_state": jianying.bridge_state(timeout=5.0),
        },
    }
    return "readonly", data


def probe(ctx, args):
    """Probe one or more files and report whether they are concat-copy compatible."""
    tool = ctx.require()
    probes = [_probe(tool, _resolve_input(path)) for path in args.files]
    data = {"files": probes, "concat": ffmpeg.concat_group(probes)}
    return "readonly", data


def trim(ctx, args):
    """Cut a time range; re-encode by default, --copy for stream copy."""
    tool = ctx.require()
    source_path = _resolve_input(args.input)
    source = _probe(tool, source_path)
    _require_video(source)
    source_duration = source["duration_s"] or 0.0
    start = float(args.start)
    if start < 0:
        raise JyError("VALIDATION", "--start must be >= 0")
    if source_duration and start >= source_duration:
        raise JyError(
            "VALIDATION",
            f"--start {_sec(start)}s is beyond the source duration {_sec(source_duration)}s",
        )
    if args.end is not None:
        if args.end <= start:
            raise JyError("VALIDATION", "--end must be greater than --start")
        duration = args.end - start
    elif args.duration is not None:
        if args.duration <= 0:
            raise JyError("VALIDATION", "--duration must be > 0")
        duration = args.duration
    else:
        duration = source_duration - start if source_duration else None
    if source_duration and duration and start + duration > source_duration + 0.05:
        ctx.warnings.append("trim range extends past the source; ffmpeg stops at EOF")

    output = _checked_output(args.output, args.force)
    argv = [
        tool.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        _sec(start),
        "-i",
        str(source_path),
    ]
    if duration:
        argv.extend(["-t", _sec(duration)])
    if args.copy:
        argv.extend(["-c", "copy"])
        ctx.warnings.append(
            "stream copy starts at the nearest keyframe; use re-encode for frame-accurate cuts"
        )
    else:
        argv.extend(_encode_defaults())
    argv.extend(_movflags(str(output)))
    argv.extend(["-y", str(output)])
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "start_s": round(start, 3),
        "duration_s": round(duration, 3) if duration else None,
        "copy": bool(args.copy),
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def concat(ctx, args):
    """Join clips: stream-copy when identical, else re-encode normalised to input 0."""
    tool = ctx.require()
    if len(args.inputs) < 2:
        raise JyError("VALIDATION", "concat needs at least two inputs")
    paths = [_resolve_input(path) for path in args.inputs]
    probes = [_probe(tool, path) for path in paths]
    for source in probes:
        _require_video(source)
    group = ffmpeg.concat_group(probes)

    mode = args.mode
    if mode == "auto":
        mode = "copy" if group["compatible"] else "reencode"
    if mode == "copy" and not group["compatible"]:
        raise JyError(
            "VALIDATION",
            "--mode copy requires matching codec/width/height/fps/pixel format",
            details={"concat": group},
        )

    output = _checked_output(args.output, args.force)
    if mode == "copy":
        with tempfile.TemporaryDirectory(prefix="jy-concat-") as tmp_dir:
            list_path = os.path.join(tmp_dir, "inputs.txt")
            _write_concat_list(list_path, paths)
            argv = [
                tool.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
            ]
            argv.extend(_movflags(str(output)))
            argv.extend(["-y", str(output)])
            info = _run(tool, argv, output, ctx.verbose)
    else:
        argv = _concat_reencode_argv(tool, probes, paths, output, ctx.warnings)
        info = _run(tool, argv, output, ctx.verbose)

    data = {
        "inputs": [str(path) for path in paths],
        "mode": mode,
        "auto": args.mode == "auto",
        "concat": group,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def speed(ctx, args):
    """Change playback speed with setpts plus a chained atempo audio filter."""
    factor = float(args.factor)
    if factor <= 0:
        raise JyError("VALIDATION", "--factor must be > 0")
    tool = ctx.require()
    source = _probe(tool, _resolve_input(args.input))
    _require_video(source)
    output = _checked_output(args.output, args.force)

    argv = [
        tool.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(_resolve_input(args.input)),
        "-vf",
        f"setpts=PTS/{_sec(factor)}",
    ]
    if source["audio_streams"] and not args.no_audio:
        argv.extend(["-af", _atempo_chain(factor)])
    else:
        argv.append("-an")
    argv.extend(_encode_defaults())
    argv.extend(_movflags(str(output)))
    argv.extend(["-y", str(output)])
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "factor": factor,
        "audio": bool(source["audio_streams"] and not args.no_audio),
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def extract_audio(ctx, args):
    """Pull the first audio stream out as m4a/mp3/wav/flac."""
    suffix = os.path.splitext(args.output)[1].lower()
    formats = {
        ".m4a": ("aac", "m4a", True),
        ".mp3": ("libmp3lame", "mp3", True),
        ".wav": ("pcm_s16le", "wav", False),
        ".flac": ("flac", "flac", False),
    }
    if suffix not in formats:
        raise JyError(
            "UNSUPPORTED",
            f"audio container {suffix or '(none)'!r} not supported; use "
            + ", ".join(formats),
        )
    encoder, label, lossy = formats[suffix]
    tool = ctx.require(encoders=[encoder])
    source = _probe(tool, _resolve_input(args.input))
    if not source["audio_streams"]:
        raise JyError("UNSUPPORTED", f"{source['path']} has no audio stream")
    output = _checked_output(args.output, args.force)

    argv = [
        tool.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(_resolve_input(args.input)),
        "-vn",
        "-c:a",
        encoder,
    ]
    if lossy:
        argv.extend(["-b:a", args.bitrate or "192k"])
    argv.extend(["-y", str(output)])
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "format": label,
        "encoder": encoder,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def frame(ctx, args):
    """Extract one still at a timestamp (seconds) or percentage (e.g. 10%)."""
    suffix = os.path.splitext(args.output)[1].lower()
    if suffix not in (".jpg", ".jpeg", ".png"):
        raise JyError(
            "UNSUPPORTED",
            f"frame image format {suffix or '(none)'!r} not supported; use .jpg or .png",
        )
    tool = ctx.require()
    source = _probe(tool, _resolve_input(args.input))
    _require_video(source)
    at_seconds = _parse_at(args.at, source["duration_s"] or 0.0)
    output = _checked_output(args.output, args.force)

    argv = [
        tool.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        _sec(at_seconds),
        "-i",
        str(_resolve_input(args.input)),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(output),
    ]
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "at": args.at,
        "at_s": round(at_seconds, 3),
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def export(ctx, args):
    """The main verb: resolution/fit/fps/codec/rate/audio/bitdepth/faststart."""
    warnings = exportspec.validate(args)
    encoder, _family, _codec = exportspec.required_encoder(args)
    tool = ctx.require(encoders=[encoder])
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    ctx.warnings.extend(warnings)

    if args.dry_run:
        output = Path(args.output).expanduser().resolve()
        argv, planned, extra = exportspec.build_argv(
            tool, args, source, str(input_path), str(output)
        )
        ctx.warnings.extend(extra)
        data = {
            "input": _input_summary(source),
            "output_path": str(output),
            "planned": planned,
            "ffmpeg_argv": ffmpeg.with_loglevel(argv, ctx.verbose),
        }
        return "preview", data

    output = _checked_output(args.output, args.force)
    argv, planned, extra = exportspec.build_argv(
        tool, args, source, str(input_path), str(output)
    )
    ctx.warnings.extend(extra)
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "params": planned,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def gif(ctx, args):
    """Palette-optimised GIF from a time range."""
    if args.width < 16:
        raise JyError("VALIDATION", "--width must be >= 16")
    if not 1 <= args.gif_fps <= 60:
        raise JyError("VALIDATION", "--fps must be between 1 and 60")
    tool = ctx.require()
    source_path = _resolve_input(args.input)
    source = _probe(tool, source_path)
    _require_video(source)
    start = float(args.start)
    if start < 0:
        raise JyError("VALIDATION", "--start must be >= 0")
    if source["duration_s"] and start >= source["duration_s"]:
        raise JyError(
            "VALIDATION",
            f"--start {_sec(start)}s is beyond the source duration",
        )
    duration = args.duration
    if duration is None and source["duration_s"]:
        duration = source["duration_s"] - start
    if duration is not None and duration <= 0:
        raise JyError("VALIDATION", "--duration must be > 0")
    suffix = os.path.splitext(args.output)[1].lower()
    if suffix != ".gif":
        raise JyError("VALIDATION", "gif output must end with .gif")
    output = _checked_output(args.output, args.force)

    video_filter = (
        f"fps={args.gif_fps},scale={args.width}:-1:flags=lanczos,"
        "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
    )
    argv = [
        tool.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        _sec(start),
        "-i",
        str(source_path),
    ]
    if duration:
        argv.extend(["-t", _sec(duration)])
    argv.extend(["-vf", video_filter, "-loop", "0", "-y", str(output)])
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "start_s": round(start, 3),
        "duration_s": round(duration, 3) if duration else None,
        "width": args.width,
        "fps": args.gif_fps,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def _export_srt(ctx, args, subtitle_path, target_value):
    """Write SRT without rendering video (convert .ass through ffmpeg)."""
    target = Path(target_value).expanduser()
    if target.exists() and not args.force:
        raise JyError(
            "VALIDATION",
            f"output already exists: {target} (pass --force to overwrite)",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    is_ass = subtitle_path.suffix.lower() in (".ass", ".ssa")
    wrapped = False
    if is_ass:
        tool = ctx.require()
        with tempfile.TemporaryDirectory(prefix="jy-srt-") as tmp_dir:
            staged = Path(tmp_dir) / "converted.srt"
            argv = [
                tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(subtitle_path.resolve()), "-f", "srt", "-y", str(staged),
            ]
            ffmpeg.run_ffmpeg(tool, argv, verbose=ctx.verbose)
            if not staged.is_file() or staged.stat().st_size == 0:
                raise JyError(
                    "INTERNAL", "ffmpeg produced no SRT from the ASS input",
                    details={"argv": argv},
                )
            text = staged.read_text(encoding="utf-8", errors="replace")
    else:
        try:
            text = subtitle_path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise JyError(
                "UNSUPPORTED",
                f"{subtitle_path.name} is not UTF-8; re-save it as UTF-8 before exporting",
                details={"error": str(exc)},
            ) from exc
        if getattr(args, "max_chars_per_line", None):
            rewrapped = effects.wrap_srt(text, args.max_chars_per_line)
            if rewrapped is None:
                ctx.warnings.append(
                    f"no SRT cues found in {subtitle_path.name}; --max-chars-per-line ignored"
                )
            else:
                text = rewrapped
                wrapped = True
    target.write_text(text, encoding="utf-8")
    cues = sum(1 for line in text.splitlines() if "-->" in line)
    data = {
        "srt": str(subtitle_path.resolve()),
        "converted_from": subtitle_path.suffix.lower().lstrip("."),
        "wrapped": wrapped,
        "cues": cues,
        "output": {
            "path": str(target.resolve()),
            "size_bytes": target.stat().st_size,
            "lines": len(text.splitlines()),
        },
    }
    return "executed", data


def subtitle(ctx, args):
    """Burn an SRT/ASS file in with libass; never silently drop subtitles.

    --style presets are libass style overrides (force_style), so they work for
    both .srt and .ass input. --max-chars-per-line re-wraps .srt text before the
    burn; .ass input is passed through untouched.
    """
    subtitle_path = Path(args.srt).expanduser()
    if not subtitle_path.is_file():
        raise JyError("NOT_FOUND", f"subtitle file not found: {args.srt}")
    export_target = getattr(args, "export_srt", None)
    if export_target:
        if getattr(args, "output", None):
            raise JyError(
                "VALIDATION", "--export-srt writes a subtitle file; drop -o (no burn)"
            )
        return _export_srt(ctx, args, subtitle_path, export_target)
    if not getattr(args, "output", None):
        raise JyError(
            "VALIDATION", "either -o/--output (burn into video) or --export-srt PATH is required"
        )
    if not getattr(args, "input", None):
        raise JyError("VALIDATION", "burning subtitles needs an input video")
    if args.font_size <= 0:
        raise JyError("VALIDATION", "--font-size must be > 0")
    if args.margin_v < 0:
        raise JyError("VALIDATION", "--margin-v must be >= 0")
    if args.max_chars_per_line is not None and args.max_chars_per_line < 4:
        raise JyError("VALIDATION", "--max-chars-per-line must be >= 4")
    _require_video_container(args.output)

    tool = ctx.require(filters=["subtitles"])
    source = _probe(tool, _resolve_input(args.input))
    _require_video(source)
    output = _checked_output(args.output, args.force)

    is_ass = subtitle_path.suffix.lower() in (".ass", ".ssa")
    font_name = args.font_name or effects.DEFAULT_FONT
    _warn_missing_font(ctx, font_name)
    style = [
        f"FontName={_escape_style(font_name)}",
        f"FontSize={args.font_size}",
        f"MarginV={args.margin_v}",
    ]
    preset = effects.SUBTITLE_STYLES.get(args.style, "")
    if preset:
        style.extend(preset.split(","))
    if args.style == "karaoke":
        ctx.warnings.append(
            "the karaoke preset is a static style (bright fill); per-word "
            "highlighting needs word-level timing, which SRT does not carry"
        )
    if args.position:
        style.append(f"Alignment={effects.ANCHOR_ALIGNMENT[args.position]}")

    wrapped = False
    with tempfile.TemporaryDirectory(prefix="jy-subtitle-") as tmp_dir:
        burn_path = subtitle_path.resolve()
        if args.max_chars_per_line:
            if is_ass:
                ctx.warnings.append(
                    "--max-chars-per-line only applies to .srt input; ignored for "
                    f"{subtitle_path.suffix}"
                )
            else:
                try:
                    text = subtitle_path.read_bytes().decode("utf-8-sig")
                except (OSError, UnicodeDecodeError):
                    text = None
                    ctx.warnings.append(
                        "subtitle file is not readable UTF-8; --max-chars-per-line ignored"
                    )
                rewrapped = effects.wrap_srt(text, args.max_chars_per_line) if text else None
                if text and rewrapped is None:
                    ctx.warnings.append(
                        f"no SRT cues found in {subtitle_path.name}; "
                        "--max-chars-per-line ignored"
                    )
                elif rewrapped is not None:
                    burn_path = Path(tmp_dir) / "wrapped.srt"
                    burn_path.write_text(rewrapped, encoding="utf-8")
                    wrapped = True
        escaped_path = ffmpeg.escape_filter_path(str(burn_path))
        video_filter = (
            f"subtitles=filename='{escaped_path}':force_style='{','.join(style)}'"
        )
        argv = [
            tool.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(_resolve_input(args.input)),
            "-vf",
            video_filter,
        ]
        argv.extend(_encode_defaults())
        argv.extend(_movflags(str(output)))
        argv.extend(["-y", str(output)])
        info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "srt": str(subtitle_path.resolve()),
        "style": args.style,
        "position": args.position,
        "max_chars_per_line": args.max_chars_per_line,
        "wrapped": wrapped,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


# ---------------------------------------------------------------------------
# offline effect verbs (剪映 vocabulary that ffmpeg can actually render)


def transition(ctx, args):
    """Join two clips with one xfade transition (audio cross-faded when present)."""
    tool = ctx.require(filters=["xfade"])
    available = ffmpeg.xfade_transitions(tool.ffmpeg)
    if args.list:
        return "readonly", {
            "ffmpeg": tool.ffmpeg,
            "count": len(available),
            "transitions": available,
        }
    if not args.output:
        raise JyError("VALIDATION", "-o/--output is required unless --list is used")
    if len(args.inputs) != 2:
        raise JyError("VALIDATION", "transition needs exactly two inputs")
    name = str(args.type).strip().lower()
    if name not in available:
        raise JyError(
            "VALIDATION",
            f"transition {args.type!r} is not supported by {tool.ffmpeg}",
            details={
                "available_count": len(available),
                "hint": "jy transition --list",
            },
        )
    duration = float(args.duration)
    if duration <= 0:
        raise JyError("VALIDATION", "--duration must be > 0")
    if args.offset is not None and args.offset < 0:
        raise JyError("VALIDATION", "--offset must be >= 0")
    _require_video_container(args.output)

    paths = [_resolve_input(path) for path in args.inputs]
    probes = [_probe(tool, path) for path in paths]
    for source in probes:
        _require_video(source)
    lengths = [source["duration_s"] or 0.0 for source in probes]
    if not lengths[0]:
        raise JyError(
            "UNSUPPORTED", f"cannot determine the duration of {probes[0]['path']}"
        )
    for source, length in zip(probes, lengths):
        if length and length <= duration:
            raise JyError(
                "VALIDATION",
                f"{os.path.basename(source['path'])} is {_sec(length)}s, "
                f"not longer than the {_sec(duration)}s transition",
            )

    if args.offset is None:
        offset = lengths[0] - duration
    else:
        offset = float(args.offset)
        if offset > lengths[0] - duration:
            ctx.warnings.append(
                f"--offset {_sec(offset)}s leaves less than one transition duration on "
                "the first clip; clamped"
            )
            offset = max(0.0, lengths[0] - duration)
    if offset < 0:
        raise JyError(
            "VALIDATION",
            f"the first clip ({_sec(lengths[0])}s) is shorter than the transition",
        )

    has_audio = all(bool(source["audio_streams"]) for source in probes)
    if not has_audio and any(bool(source["audio_streams"]) for source in probes):
        ctx.warnings.append(
            "some inputs have no audio; the transition output is video-only"
        )
    first = probes[0]
    width, height = _even(first["width"]), _even(first["height"])
    fps_rate = _fps_rate(first)

    graph = effects.join_graph(
        count=2,
        durations=lengths,
        width=width,
        height=height,
        fps_rate=fps_rate,
        transition=name,
        transition_duration=duration,
        has_audio=has_audio,
        first_offset=offset,
    )
    output = _checked_output(args.output, args.force)
    input_args = []
    for path in paths:
        input_args.extend(["-i", str(path)])
    argv = _filter_complex_argv(
        tool, input_args, graph, output, audio="graph" if has_audio else "none"
    )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "inputs": [str(path) for path in paths],
        "type": name,
        "duration_s": round(duration, 3),
        "offset_s": round(offset, 3),
        "normalized": {"width": width, "height": height, "fps": fps_rate},
        "audio": has_audio,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def text(ctx, args):
    """Burn one styled text line in with libass (no SRT needed)."""
    content = str(args.content).replace("\\n", "\n").strip()
    if not content:
        raise JyError("VALIDATION", "--content must not be empty")
    start = float(args.start)
    duration = float(args.duration)
    if start < 0:
        raise JyError("VALIDATION", "--start must be >= 0")
    if duration <= 0:
        raise JyError("VALIDATION", "--duration must be > 0")
    if args.font_size <= 0:
        raise JyError("VALIDATION", "--font-size must be > 0")
    if args.outline < 0:
        raise JyError("VALIDATION", "--outline must be >= 0")
    _require_video_container(args.output)

    primary = effects.parse_color(args.color, label="--color")
    _warn_missing_font(ctx, args.font_name or effects.DEFAULT_FONT)
    tool = ctx.require(filters=["ass"])
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    if source["duration_s"] and start + duration > source["duration_s"] + 0.05:
        ctx.warnings.append(
            f"text ends at {_sec(start + duration)}s but the clip is "
            f"{_sec(source['duration_s'])}s; the tail is cut off"
        )
    output = _checked_output(args.output, args.force)

    width, height = _even(source["width"]), _even(source["height"])
    anchor = effects.anchor_point(args.position, width, height)
    events = effects.text_events(
        content,
        start=start,
        duration=duration,
        animation=args.animation,
        x=anchor[0],
        y=anchor[1],
        font_size=args.font_size,
    )
    script = effects.ass_script(
        width=width,
        height=height,
        events=events,
        font_name=args.font_name,
        font_size=args.font_size,
        primary=primary,
        outline=args.outline,
    )
    with tempfile.TemporaryDirectory(prefix="jy-text-") as tmp_dir:
        ass_path = Path(tmp_dir) / "overlay.ass"
        ass_path.write_text(script, encoding="utf-8")
        video_filter = "ass=filename='" + ffmpeg.escape_filter_path(str(ass_path)) + "'"
        argv = [
            tool.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(input_path),
            "-vf",
            video_filter,
        ]
        argv.extend(_encode_defaults())
        argv.extend(_movflags(str(output)))
        argv.extend(["-y", str(output)])
        info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "content": content,
        "start_s": round(start, 3),
        "duration_s": round(duration, 3),
        "position": args.position,
        "anchor": {"x": anchor[0], "y": anchor[1]},
        "font_size": args.font_size,
        "font_name": args.font_name,
        "color": args.color,
        "outline": args.outline,
        "animation": args.animation,
        "events": len(events),
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def montage(ctx, args):
    """Cut each clip to a segment and join the segments (蒙太奇 / beat cut)."""
    transition = str(args.transition).strip().lower()
    tool = ctx.require(filters=["xfade"] if transition not in ("none", "") else ())
    if transition not in ("none", ""):
        available = ffmpeg.xfade_transitions(tool.ffmpeg)
        if transition not in available:
            raise JyError(
                "VALIDATION",
                f"transition {args.transition!r} is not supported by {tool.ffmpeg}",
                details={
                    "available_count": len(available),
                    "hint": "jy transition --list",
                },
            )
    transition_duration = float(args.transition_duration)
    if transition_duration <= 0:
        raise JyError("VALIDATION", "--transition-duration must be > 0")
    _require_video_container(args.output)

    paths = [_resolve_input(path) for path in args.inputs]
    probes = [_probe(tool, path) for path in paths]
    for source in probes:
        _require_video(source)

    beats = None
    if args.sync_beats:
        beats_path = Path(args.sync_beats).expanduser()
        if not beats_path.is_file():
            raise JyError("NOT_FOUND", f"beats file not found: {args.sync_beats}")
        try:
            beats_text = beats_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JyError(
                "VALIDATION", f"cannot read beats file {args.sync_beats}: {exc}"
            ) from exc
        boundaries, lengths = effects.parse_beats(beats_text)
        if len(lengths) > len(paths):
            raise JyError(
                "VALIDATION",
                f"--sync-beats defines {len(lengths)} cuts but only {len(paths)} "
                "clip(s) were given; pass one clip per cut or use --per-clip",
                details={"segments": len(lengths), "clips": len(paths)},
            )
        if len(lengths) < len(paths):
            ctx.warnings.append(
                f"{len(paths) - len(lengths)} clip(s) unused: --sync-beats defines "
                f"{len(lengths)} cuts"
            )
        chosen = []
        for index in range(len(lengths)):
            path, source, length = paths[index], probes[index], lengths[index]
            if source["duration_s"] and source["duration_s"] < length - 0.05:
                ctx.warnings.append(
                    f"{os.path.basename(str(path))} is {_sec(source['duration_s'])}s, "
                    f"shorter than its {_sec(length)}s beat segment; using the whole "
                    "clip"
                )
                length = source["duration_s"]
            chosen.append((path, source, length))
        mode = "beats"
        beats = {
            "path": str(beats_path.resolve()),
            "cuts_ms": [round(value * 1000, 3) for value in boundaries],
        }
    else:
        per_clip = float(args.per_clip)
        if per_clip <= 0:
            raise JyError("VALIDATION", "--per-clip must be > 0")
        chosen = []
        for path, source in zip(paths, probes):
            length = per_clip
            if source["duration_s"] and source["duration_s"] < per_clip:
                length = source["duration_s"]
                ctx.warnings.append(
                    f"{os.path.basename(str(path))} is {_sec(source['duration_s'])}s, "
                    f"shorter than --per-clip {_sec(per_clip)}s; using the whole clip"
                )
            chosen.append((path, source, length))
        mode = "per-clip"

    lengths = [length for _path, _source, length in chosen]
    if any(length <= 0 for length in lengths):
        raise JyError("UNSUPPORTED", "cannot determine a segment duration")
    if transition not in ("none", ""):
        for path, _source, length in chosen:
            if length <= transition_duration:
                raise JyError(
                    "VALIDATION",
                    f"the {os.path.basename(str(path))} segment is {_sec(length)}s, "
                    f"not longer than --transition-duration {_sec(transition_duration)}s",
                )
    has_audio = all(bool(source["audio_streams"]) for _path, source, _len in chosen)
    if not has_audio and any(
        bool(source["audio_streams"]) for _path, source, _len in chosen
    ):
        ctx.warnings.append(
            "some segments have no audio; the montage output is video-only"
        )

    first = probes[0]
    width, height = _even(first["width"]), _even(first["height"])
    fps_rate = _fps_rate(first)
    graph = effects.join_graph(
        count=len(chosen),
        durations=lengths,
        width=width,
        height=height,
        fps_rate=fps_rate,
        transition=transition,
        transition_duration=transition_duration,
        has_audio=has_audio,
        trims=lengths,
    )
    output = _checked_output(args.output, args.force)
    input_args = []
    for path in paths[: len(chosen)]:
        input_args.extend(["-i", str(path)])
    argv = _filter_complex_argv(
        tool, input_args, graph, output, audio="graph" if has_audio else "none"
    )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "mode": mode,
        "segments": [
            {
                "clip": str(path),
                "start_s": 0.0,
                "duration_s": round(length, 3),
            }
            for path, _source, length in chosen
        ],
        "transition": None if transition in ("none", "") else transition,
        "transition_duration_s": (
            None if transition in ("none", "") else round(transition_duration, 3)
        ),
        "beats": beats,
        "normalized": {"width": width, "height": height, "fps": fps_rate},
        "audio": has_audio,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def filter_look(ctx, args):
    """Apply a named look preset (or a .cube LUT) at a 0..100 intensity."""
    intensity = float(args.intensity)
    if not 0 <= intensity <= 100:
        raise JyError("VALIDATION", "--intensity must be between 0 and 100")
    if args.lut:
        lut_path = Path(args.lut).expanduser()
        if not lut_path.is_file():
            raise JyError("NOT_FOUND", f"LUT file not found: {args.lut}")
        tool = ctx.require(filters=["lut3d", "blend"])
        chain = "lut3d=file='" + ffmpeg.escape_filter_path(str(lut_path.resolve())) + "'"
        preset = None
        lut = str(lut_path.resolve())
    else:
        preset = args.preset
        tool = ctx.require(filters=list(effects.PRESET_FILTERS[preset]) + ["blend"])
        chain = effects.FILTER_PRESETS[preset]
        lut = None
    _require_video_container(args.output)

    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    output = _checked_output(args.output, args.force)
    graph = effects.filter_graph(chain, intensity)
    argv = _filter_complex_argv(
        tool,
        ["-i", str(input_path)],
        graph,
        output,
        audio="source" if source["audio_streams"] else "none",
    )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "preset": preset,
        "lut": lut,
        "intensity": intensity,
        "chain": chain,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def key(ctx, args):
    """Chroma key one colour out of the clip over a colour or image background."""
    tool = ctx.require(filters=["colorkey", "overlay"])
    similarity = float(args.similarity)
    blend = float(args.blend)
    if not 0 <= similarity <= 1:
        raise JyError("VALIDATION", "--similarity must be between 0 and 1")
    if not 0 <= blend <= 1:
        raise JyError("VALIDATION", "--blend must be between 0 and 1")
    _require_video_container(args.output)

    background = str(args.bg).strip()
    background_colour = None
    image_path = None
    if background.lower().startswith("color:"):
        background_colour = effects.parse_ffmpeg_color(background.split(":", 1)[1])
    else:
        if not effects.is_image_path(background):
            raise JyError(
                "VALIDATION",
                f"invalid --bg {args.bg!r}; use color:<name|#RRGGBB> or an image "
                "file (.jpg/.png/.webp)",
            )
        image_path = _resolve_input(background)
        _require_video(_probe(tool, image_path))

    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    if not source["duration_s"]:
        raise JyError(
            "UNSUPPORTED", f"cannot determine the duration of {source['path']}"
        )
    output = _checked_output(args.output, args.force)

    graph = effects.key_graph(
        colour_hex=args.color,
        similarity=similarity,
        blend=blend,
        width=source["width"],
        height=source["height"],
        duration=source["duration_s"],
        fps_rate=_fps_rate(source),
        background_colour=background_colour,
        image_input=image_path is not None,
    )
    input_args = ["-i", str(input_path)]
    if image_path:
        input_args.extend(["-loop", "1", "-i", str(image_path)])
    argv = _filter_complex_argv(
        tool,
        input_args,
        graph,
        output,
        audio="source" if source["audio_streams"] else "none",
    )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "color": effects.normalize_hex(args.color, label="--color"),
        "similarity": similarity,
        "blend": blend,
        "background": (
            {"type": "image", "path": str(image_path)}
            if image_path
            else {"type": "color", "value": background_colour}
        ),
        "note": KEY_NOTE,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


def beautify(ctx, args):
    """Global soft-skin look: gaussian soften + brightness + unsharp (not 美颜)."""
    levels = {
        "smooth": float(args.smooth),
        "brighten": float(args.brighten),
        "sharpen": float(args.sharpen),
    }
    for name, value in levels.items():
        if not 0 <= value <= 100:
            raise JyError("VALIDATION", f"--{name} must be between 0 and 100")
    _require_video_container(args.output)

    tool = ctx.require(filters=["blend", "gblur", "unsharp", "eq"])
    input_path = _resolve_input(args.input)
    source = _probe(tool, input_path)
    _require_video(source)
    output = _checked_output(args.output, args.force)
    if not any(levels.values()):
        ctx.warnings.append("all strengths are 0; the output is a plain re-encode")

    graph = effects.beautify_graph(**levels)
    argv = _filter_complex_argv(
        tool,
        ["-i", str(input_path)],
        graph,
        output,
        audio="source" if source["audio_streams"] else "none",
    )
    info = _run(tool, argv, output, ctx.verbose)
    data = {
        "input": _input_summary(source),
        "smooth": levels["smooth"],
        "brighten": levels["brighten"],
        "sharpen": levels["sharpen"],
        "face_aware": False,
        "note": BEAUTIFY_NOTE,
        "output": info,
        "ffmpeg_argv": argv,
    }
    return "executed", data


# ---------------------------------------------------------------------------
# helpers


def _resolve_input(path):
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise JyError("NOT_FOUND", f"input not found: {path}")
    if not candidate.is_file():
        raise JyError("VALIDATION", f"input is not a file: {path}")
    return candidate.resolve()


def _probe(tool, path):
    return ffmpeg.probe_media(tool, str(path))


def _warn_missing_font(ctx, family):
    """Warn when a family is not installed (libass would silently fall back)."""
    from .verbs import fonts as fonts_verbs

    try:
        if not fonts_verbs.quick_family_present(family):
            ctx.warnings.append(
                f"字体「{family}」本机没找到（libass 会静默回退，可能出豆腐块）；"
                "用 `jy fonts --check <family>` 校验、`jy fonts` 列出可用字族。"
            )
    except Exception:  # noqa: BLE001 - a font probe must never break a render
        pass


def _require_video(source):
    if not source.get("codec") or not source.get("width") or not source.get("height"):
        raise JyError("UNSUPPORTED", f"no video stream in {source['path']}")


def _require_video_container(path):
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in exportspec.CONTAINERS:
        raise JyError(
            "VALIDATION",
            f"output container {suffix or '(none)'!r} not supported; use "
            + ", ".join(exportspec.CONTAINERS),
        )


def _fps_rate(source):
    rate = source["fps"]["r_frame_rate"]
    if not rate or rate == "0/0":
        return None
    return rate


def _filter_complex_argv(tool, input_args, graph, output, audio="none", extra=()):
    """argv for a one-video-output filter_complex graph.

    ``audio`` picks how the audio track is carried: ``graph`` maps the graph's
    ``[a]`` output (join verbs), ``source`` maps input 0's audio untouched by the
    graph, ``none`` drops audio entirely.
    """
    argv = [tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    argv.extend(input_args)
    argv.extend(["-filter_complex", graph, "-map", "[v]"])
    if audio == "graph":
        argv.extend(["-map", "[a]"])
    elif audio == "source":
        argv.extend(["-map", "0:a?"])
    argv.extend(_video_encode_args())
    if audio == "none":
        argv.append("-an")
    else:
        argv.extend(["-c:a", "aac", "-b:a", "192k"])
    argv.extend(extra)
    argv.extend(_movflags(str(output)))
    argv.extend(["-y", str(output)])
    return argv


def _checked_output(path, force):
    target = Path(path).expanduser()
    if target.exists() and target.is_dir():
        raise JyError("VALIDATION", f"output path is a directory: {target}")
    if target.exists() and not force:
        raise JyError(
            "VALIDATION",
            f"output already exists: {target} (pass --force to overwrite)",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JyError(
            "VALIDATION", f"cannot create output directory {target.parent}: {exc}"
        ) from exc
    return target.resolve()


def _run(tool, argv, output, verbose):
    ffmpeg.run_ffmpeg(tool, argv, verbose=verbose)
    return ffmpeg.output_info(tool, str(output))


def _input_summary(source):
    return {
        "path": source["path"],
        "duration_s": source["duration_s"],
        "codec": source["codec"],
        "width": source["width"],
        "height": source["height"],
        "fps": source["fps"]["value"] or None,
        "audio_streams": len(source["audio_streams"]),
    }


def _movflags(output_path):
    if os.path.splitext(output_path)[1].lower() in (".mp4", ".mov"):
        return ["-movflags", "+faststart"]
    return []


def _video_encode_args():
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]


def _encode_defaults():
    return _video_encode_args() + ["-c:a", "aac", "-b:a", "192k"]


def _sec(value):
    return f"{value:g}"


def _even(value):
    return max(2, int(value) - int(value) % 2)


def _atempo_chain(factor):
    chain = []
    remaining = factor
    while remaining > 2.0:
        chain.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        chain.append(0.5)
        remaining /= 0.5
    chain.append(round(remaining, 6))
    return ",".join(f"atempo={_sec(step)}" for step in chain)


def _write_concat_list(list_path, paths):
    with open(list_path, "w", encoding="utf-8") as handle:
        for path in paths:
            escaped = str(path).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")


def _concat_reencode_argv(tool, probes, paths, output, warnings):
    first = probes[0]
    width = _even(first["width"])
    height = _even(first["height"])
    fps_rate = _fps_rate(first)
    has_audio = [bool(source["audio_streams"]) for source in probes]
    with_audio = all(has_audio)
    if not with_audio and any(has_audio):
        warnings.append("some inputs have no audio; the concat output is video-only")

    parts = []
    join_inputs = []
    for index in range(len(probes)):
        parts.append(f"[{index}:v]{effects.normalize_chain(width, height, fps_rate)}[v{index}]")
        join_inputs.append(f"[v{index}]")
        if with_audio:
            parts.append(f"[{index}:a]{effects.audio_chain()}[a{index}]")
            join_inputs.append(f"[a{index}]")
    concat_filter = (
        "".join(join_inputs)
        + f"concat=n={len(probes)}:v=1:a={1 if with_audio else 0}"
        + ("[v][a]" if with_audio else "[v]")
    )
    parts.append(concat_filter)

    argv = [tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    for path in paths:
        argv.extend(["-i", str(path)])
    argv.extend(["-filter_complex", ";".join(parts), "-map", "[v]"])
    if with_audio:
        argv.extend(["-map", "[a]"])
    argv.extend(
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    )
    if with_audio:
        argv.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        argv.append("-an")
    argv.extend(_movflags(str(output)))
    argv.extend(["-y", str(output)])
    return argv


def _parse_at(value, duration):
    token = str(value).strip()
    if token.endswith("%"):
        try:
            percent = float(token[:-1])
        except ValueError as exc:
            raise JyError("VALIDATION", f"invalid percentage: {value!r}") from exc
        if not 0 <= percent <= 100:
            raise JyError("VALIDATION", "--at percentage must be between 0 and 100")
        at_seconds = duration * percent / 100.0
    else:
        try:
            at_seconds = float(token)
        except ValueError as exc:
            raise JyError(
                "VALIDATION", f"invalid --at {value!r}; use seconds or e.g. 10%"
            ) from exc
    if at_seconds < 0:
        raise JyError("VALIDATION", "--at must be >= 0")
    if duration and at_seconds >= duration:
        raise JyError(
            "VALIDATION",
            f"--at {_sec(at_seconds)}s is beyond the source duration {_sec(duration)}s",
        )
    return at_seconds


def _escape_style(value):
    return value.replace("\\", "\\\\").replace("'", "\\'").replace(",", "\\,")
