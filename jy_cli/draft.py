"""Author a 剪映 draft folder from a small JSON timeline spec.

The document written here is a plaintext ``draft_info.json``. 剪映 11.6's draft
loader sniffs the first byte (``{`` == plaintext) and its native film-export job
renders such a folder without any GUI interaction — verified on 2026-10-24 in
the REA workspace (text-only draft + 1080x1920, and a video+text draft authored
by this module's predecessor experiment).

Text styling is real: the style is not a set of scalar material fields but a
JSON ``content`` payload with per-range ``styles``, built by :mod:`jy_cli.material`
and proven to render (frame diff, 2026-10-24).  Position comes from the segment's
``clip.transform``; ``alignment`` is intra-box only.  The effect-family materials
(transition / filter / video_effect / sticker / face_beauty) have builders but are
NOT proven to render here — see ``jy_cli.material.SCHEMA_UNVERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import material as material_lib
from .envelope import JyError

SKELETON = Path(__file__).with_name("data") / "draft_skeleton.json"
META_SKELETON = Path(__file__).with_name("data") / "draft_meta_skeleton.json"
MARKER = ".jy-draft.json"
MARKER_SCHEMA = "jy.draft.v1"

ALIGNMENTS = {"left": 0, "center": 1, "right": 2}
# Keys a spec text item may carry. Unknown keys are rejected instead of being
# silently dropped: the one thing worse than a bad title is a title that renders
# in the wrong colour because the style keys sat one level too high.
_TEXT_ITEM_KEYS = frozenset({"content", "start", "duration", "style", "position",
                             "text_effect", "animations", "text_template"})
TEXT_DEFAULTS = {
    # font_size is 剪映's own style-size unit (see material.PX_PER_SIZE_UNIT_AT_1080):
    # ~6.7 px per unit on a 1080-wide canvas, i.e. 5.0 ~= 34 px, 15.0 ~= 100 px.
    "font_size": 5.0,
    "color": "#FFFFFF",
    "alignment": 1,
    "bold": True,
    "italic": False,
    "underline": False,
    "letter_spacing": 0.0,
    "line_spacing": 0.02,
    "stroke_width": 0.08,
    "stroke_color": "#000000",
    "font_path": "",
}
# The native job's default text style is large; layering text above the video
# track is enough for it to composite. 15000 is the subtitle-layer convention
# observed in 剪映 drafts.
TEXT_RENDER_INDEX = 15000
COLOR_WHEEL_SEGMENTS = frozenset({"gamma", "lift", "gain", "offset"})


def _uid():
    return str(uuid.uuid4()).upper()


def _us(seconds):
    return int(round(float(seconds) * 1_000_000))


def _secs(us):
    return round(int(us) / 1_000_000.0, 3)


def _skeleton():
    try:
        return json.loads(SKELETON.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - shipped data
        raise JyError(
            "INTERNAL",
            f"jy 的草稿骨架文件损坏：{SKELETON}",
            details={"error": str(exc)},
        ) from exc


def _meta_skeleton():
    try:
        return json.loads(META_SKELETON.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - shipped data
        raise JyError(
            "INTERNAL",
            f"jy 的草稿元信息骨架文件损坏：{META_SKELETON}",
            details={"error": str(exc)},
        ) from exc


def materials_bytes(document, root):
    """Bytes of the media a draft references (剪映's own ``draft_timeline_materials_size``).

    Live entries mix 0 / 42 MB / 1.0 GB for drafts whose ``draft_info.json`` is 3–1 400 KB,
    so the number tracks referenced media, not the draft file. Missing files count as 0.
    """
    total = 0
    materials = document.get("materials")
    if not isinstance(materials, dict):
        return 0
    seen = set()
    for bucket in materials.values():
        if not isinstance(bucket, list):
            continue
        for material in bucket:
            if not isinstance(material, dict):
                continue
            path = material.get("path")
            if not isinstance(path, str) or not path or path in seen:
                continue
            seen.add(path)
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path(root) / candidate
            try:
                if candidate.is_file():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def meta_info(document, root, *, created_us=None, modified_us=None):
    """Build ``draft_meta_info.json``: 剪映's draft-store loader requires this file.

    Its ``isDraftFoldValid`` is ``exists(folder) && exists(folder/draft_meta_info.json)``
    (verified statically in 11.6.13358); a folder without it is skipped even when an
    index entry points at it. The 52-key skeleton is a copy of what 剪映's own render
    job wrote for a jy draft, so the shape is the app's, not a guess.
    """
    folder = Path(root).resolve()
    meta = _meta_skeleton()
    now_us = int(time.time() * 1_000_000)
    if created_us is None and isinstance(document.get("create_time"), (int, float)):
        created_us = int(document["create_time"]) * 1_000_000
    if modified_us is None and isinstance(document.get("update_time"), (int, float)):
        modified_us = int(document["update_time"]) * 1_000_000
    meta.update({
        "draft_id": str(document.get("id") or ""),
        "draft_name": str(document.get("name") or folder.name),
        "draft_fold_path": str(folder),
        "draft_root_path": str(folder.parent),
        "draft_cover": "draft_cover.jpg",
        "draft_type": str(document.get("draft_type") or ""),
        "draft_timeline_materials_size_": materials_bytes(document, folder),
        "tm_draft_create": int(created_us if created_us is not None else now_us),
        "tm_draft_modified": int(modified_us if modified_us is not None else now_us),
        "tm_duration": int(document.get("duration") or 0),
    })
    return meta


def load_spec(spec_path):
    """Read and structurally validate the timeline spec."""
    try:
        document = json.loads(Path(spec_path).read_text("utf-8"))
    except OSError as exc:
        raise JyError("NOT_FOUND", f"spec 文件读不了：{spec_path}") from exc
    except json.JSONDecodeError as exc:
        raise JyError(
            "VALIDATION",
            f"spec 不是合法 JSON：{spec_path}（{exc.msg}，第 {exc.lineno} 行）",
        ) from exc
    if not isinstance(document, dict):
        raise JyError("VALIDATION", "spec 顶层必须是 JSON 对象")
    return document


def _resolve_media(spec_dir, value, label):
    raw = str(value or "").strip()
    if not raw:
        raise JyError("VALIDATION", f"{label}.path 不能为空")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (spec_dir / path).resolve()
    if not path.exists():
        raise JyError("NOT_FOUND", f"{label} 引用的文件不存在：{path}")
    if not path.is_file():
        raise JyError("VALIDATION", f"{label} 引用的是目录，不是文件：{path}")
    return path.resolve()


def _number(value, label, *, minimum=None, maximum=None, allow_none=False):
    if value is None:
        if allow_none:
            return None
        raise JyError("VALIDATION", f"{label} 必须是数字")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JyError("VALIDATION", f"{label} 必须是数字，收到 {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise JyError("VALIDATION", f"{label} 必须 >= {minimum}，收到 {number:g}")
    if maximum is not None and number > maximum:
        raise JyError("VALIDATION", f"{label} 必须 <= {maximum}，收到 {number:g}")
    return number


def _canvas_from(spec, first_probe):
    canvas = spec.get("canvas") or {}
    if not isinstance(canvas, dict):
        raise JyError("VALIDATION", "spec.canvas 必须是对象 {width,height,fps}")
    width = canvas.get("width")
    height = canvas.get("height")
    fps = canvas.get("fps")
    if width is None or height is None:
        if first_probe is None:
            raise JyError(
                "VALIDATION",
                "spec 没有 clips 时必须显式给 canvas.width / canvas.height",
            )
        width = width if width is not None else first_probe["width"]
        height = height if height is not None else first_probe["height"]
    if fps is None:
        fps = (first_probe or {}).get("fps", {}).get("value") or 30.0
    width = int(_number(width, "canvas.width", minimum=2, maximum=7680))
    height = int(_number(height, "canvas.height", minimum=2, maximum=7680))
    fps = float(_number(fps, "canvas.fps", minimum=1, maximum=120))
    return {"width": width - width % 2, "height": height - height % 2, "fps": round(fps, 3)}


def _normalise_style(raw, label):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise JyError("VALIDATION", f"{label}.style 必须是对象")
    style = dict(TEXT_DEFAULTS)
    style.update({key: value for key, value in raw.items() if value is not None})
    if isinstance(style.get("alignment"), str):
        key = style["alignment"].strip().lower()
        if key not in ALIGNMENTS:
            raise JyError(
                "VALIDATION", f"{label}.style.alignment 只能是 left/center/right 或 0/1/2"
            )
        style["alignment"] = ALIGNMENTS[key]
    style["alignment"] = int(_number(style["alignment"], f"{label}.style.alignment",
                                     minimum=0, maximum=2))
    color = str(style.get("color") or "#FFFFFF").strip()
    if not color.startswith("#"):
        color = "#" + color
    if len(color) != 7:
        raise JyError("VALIDATION", f"{label}.style.color 需要 #RRGGBB，收到 {color!r}")
    try:
        int(color[1:], 16)
    except ValueError as exc:
        raise JyError("VALIDATION", f"{label}.style.color 不是合法十六进制：{color!r}") from exc
    style["color"] = color.upper()
    style["font_size"] = float(_number(style["font_size"], f"{label}.style.font_size",
                                       minimum=0.1, maximum=500))
    style["letter_spacing"] = float(
        _number(style["letter_spacing"], f"{label}.style.letter_spacing",
                minimum=-100, maximum=100)
    )
    style["line_spacing"] = float(
        # 剪映's own drafts store 0.02 here, so 0 must be legal.
        _number(style["line_spacing"], f"{label}.style.line_spacing", minimum=0.0, maximum=10)
    )
    for flag in ("bold", "italic", "underline"):
        style[flag] = bool(style.get(flag))
    style["stroke_width"] = float(
        _number(style.get("stroke_width", 0.08), f"{label}.style.stroke_width",
                minimum=0.0, maximum=2.0)
    )
    stroke_color = str(style.get("stroke_color") or "#000000").strip()
    if not stroke_color.startswith("#"):
        stroke_color = "#" + stroke_color
    if len(stroke_color) != 7:
        raise JyError("VALIDATION", f"{label}.style.stroke_color 需要 #RRGGBB，收到 {stroke_color!r}")
    style["stroke_color"] = stroke_color.upper()
    style["font_path"] = str(style.get("font_path") or "")
    return style




def _segment(material_id, start_us, duration_us, *, source_start_us=0, render_index=0):
    return {
        "id": _uid(),
        "material_id": material_id,
        "target_timerange": {"start": start_us, "duration": duration_us},
        "source_timerange": {"start": source_start_us, "duration": duration_us},
        "speed": 1.0,
        "volume": 1.0,
        "extra_material_refs": [],
        "is_placeholder": False,
        "render_index": render_index,
        "visible": True,
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
        "uniform_scale": {"on": True, "value": 1.0},
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        "caption_info": None,
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_lut": False,
        "intensifies_audio": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "template_id": "",
        "track_attribute": 0,
        "track_render_index": 0,
    }


def _track(kind, segments, flag=0):
    return {
        "id": _uid(),
        "type": kind,
        "attribute": 0,
        "flag": flag,
        "is_default_name": True,
        "name": "",
        "segments": segments,
    }


def effect_track(segments):
    """A 剪映 ``effect`` track — the container 画面特效 actually renders from.

    Verified against a GUI-saved draft: ``{"id","type":"effect","segments":[…],
    "is_default_name":true}`` with no ``attribute``/``flag`` keys (unlike video and
    text tracks). An effect held in a segment's ``extra_material_refs`` instead of
    this track composites as a black layer.
    """
    return {
        "id": _uid(),
        "type": "effect",
        "segments": segments,
        "is_default_name": True,
    }


def sticker_track(segments):
    """A 剪映 ``sticker`` track (verified): ``{id,type:"sticker",segments,is_default_name}``.

    Stickers never ride on the video track; 剪映 gives them their own track whose
    segments carry ``clip.scale`` / ``clip.transform`` and ``render_index`` 14000.
    """
    return {
        "id": _uid(),
        "type": "sticker",
        "segments": segments,
        "is_default_name": True,
    }


def _parse_stickers(spec, warnings):
    """Top-level ``stickers[]``: 剪映-native stickers on their own track.

    Entry: ``{resource_id, path, sticker_id?, start?, duration?, scale?, position?}``.
    ``path`` must point at 剪映's downloaded plugin package
    (``…/Download/com.lemon.openplugins.tmp/<plugin>/PluginSticker_<uuid>``).
    """
    entries = []
    for index, raw in enumerate(spec.get("stickers") or []):
        where = f"stickers[{index}]"
        if not isinstance(raw, dict):
            raise JyError("VALIDATION", f"{where} 必须是对象")
        rid = str(raw.get("resource_id") or "").strip()
        if not rid:
            raise JyError("VALIDATION", f"{where}.resource_id 不能为空")
        position = raw.get("position") or {}
        if not isinstance(position, dict):
            raise JyError("VALIDATION", f"{where}.position 必须是对象")
        entries.append({
            "resource_id": rid,
            "sticker_id": str(raw.get("sticker_id") or ""),
            "path": str(raw.get("path") or ""),
            "start": _number(raw.get("start", 0.0), f"{where}.start", minimum=0.0),
            "duration": _number(raw.get("duration", 0.0), f"{where}.duration", minimum=0.0),
            "scale": _number(raw.get("scale", 0.26), f"{where}.scale",
                             minimum=0.0, maximum=20.0),
            "position": {
                "x": _number(position.get("x", 0.0), f"{where}.position.x"),
                "y": _number(position.get("y", 0.0), f"{where}.position.y"),
            },
        })
    if entries:
        warnings.append(
            "stickers: 贴纸写成独立 sticker 轨道（与剪映 GUI 保存结构一致）；"
            "素材包必须在剪映下载缓存里，否则原生导出报 film_export_dependency_missing。"
        )
    return entries


def filter_track(segments):
    """A 剪映 ``filter`` track (verified): ``{id,type:"filter",segments,is_default_name}``.

    Filters are timeline-wide: the GUI writes one segment spanning the whole draft with
    ``render_index`` 10000, and the material lives in ``materials.effects[]`` (type
    "filter") — **not** ``materials.filters``.
    """
    return {
        "id": _uid(),
        "type": "filter",
        "segments": segments,
        "is_default_name": True,
    }


def _parse_filters(spec, warnings):
    """Top-level ``filters[]``: 剪映-native 滤镜 over the whole timeline.

    Entry: ``{resource_id, path, name?, value?, category_id?, category_name?,
    start?, duration?}``. ``path`` must point at the downloaded package
    (``…/Cache/artistEffect/<rid>/<md5>``) — an uncached filter never applies.
    """
    entries = []
    for index, raw in enumerate(spec.get("filters") or []):
        where = f"filters[{index}]"
        if not isinstance(raw, dict):
            raise JyError("VALIDATION", f"{where} 必须是对象")
        rid = str(raw.get("resource_id") or "").strip()
        if not rid:
            raise JyError("VALIDATION", f"{where}.resource_id 不能为空")
        entries.append({
            "resource_id": rid,
            "name": str(raw.get("name") or ""),
            "path": str(raw.get("path") or ""),
            "value": _number(raw.get("value", 1.0), f"{where}.value", minimum=0.0, maximum=1.0),
            "category_id": str(raw.get("category_id") or ""),
            "category_name": str(raw.get("category_name") or ""),
            "request_id": str(raw.get("request_id") or ""),
            "start": _number(raw.get("start", 0.0), f"{where}.start", minimum=0.0),
            "duration": _number(raw.get("duration", 0.0), f"{where}.duration", minimum=0.0),
        })
    if entries:
        warnings.append(
            "filters: 滤镜写成独立 filter 轨道 + materials.effects[type=filter]（剪映实测结构）；"
            "素材必须已缓存（Cache/artistEffect/…），未缓存时 GUI 的 + 都不会生效。"
        )
    return entries


def _parse_keyframes(raw, where):
    """``clips[].keyframes`` -> list of {property, points[(us, values, left, right)]}."""
    if isinstance(raw, dict):
        raw = [{"property": k, "points": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        raise JyError("VALIDATION", f"{where} 必须是数组或对象")
    channels = []
    for index, item in enumerate(raw):
        label = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象 {{property, points}}")
        prop = str(item.get("property") or item.get("property_type") or "").strip()
        if not prop.startswith("KFType"):
            raise JyError("VALIDATION",
                          f"{label}.property 必须是剪映的属性通道名（KFTypeScaleX / "
                          f"KFTypePositionX / KFTypeAlpha / KFTypeVolume …），收到 {prop!r}")
        points = []
        for p_index, point in enumerate(item.get("points") or []):
            if not isinstance(point, dict):
                raise JyError("VALIDATION", f"{label}.points[{p_index}] 必须是对象")
            values = point.get("values")
            if values is None and point.get("value") is not None:
                values = [point["value"]]
            if not isinstance(values, list) or not values:
                raise JyError("VALIDATION", f"{label}.points[{p_index}] 需要 values 数组")
            points.append((_us(_number(point.get("time", 0.0), f"{label}.points[{p_index}].time",
                                       minimum=0.0)),
                           [_number(v, f"{label}.points[{p_index}].values") for v in values],
                           point.get("left_control"), point.get("right_control")))
        if len(points) < 2:
            raise JyError("VALIDATION", f"{label} 至少要有 2 个关键帧才有轨迹")
        channels.append({"property": prop, "points": points})
    return channels


def _parse_curve_speed(raw, where):
    """``clips[].curve_speed`` -> {name, preset_id, points[(x,y)]}."""
    if not isinstance(raw, dict):
        raise JyError("VALIDATION", f"{where} 必须是对象 {{points:[[x,y],…]}}")
    points = raw.get("points") or raw.get("speed_points") or []
    normalised = []
    for index, point in enumerate(points):
        if isinstance(point, dict):
            normalised.append((_number(point.get("x"), f"{where}.points[{index}].x"),
                               _number(point.get("y"), f"{where}.points[{index}].y",
                                       minimum=0.01)))
        elif isinstance(point, (list, tuple)) and len(point) == 2:
            normalised.append((_number(point[0], f"{where}.points[{index}][0]"),
                               _number(point[1], f"{where}.points[{index}][1]",
                                       minimum=0.01)))
        else:
            raise JyError("VALIDATION", f"{where}.points[{index}] 必须是 [x,y] 或 {{x,y}}")
    if len(normalised) < 2:
        raise JyError("VALIDATION", f"{where}.points 至少需要 2 个点")
    return {"name": str(raw.get("name") or ""),
            "preset_id": str(raw.get("id") or raw.get("preset_id") or ""),
            "points": normalised}


def _parse_mask(raw, where):
    """``clips[].mask`` -> the common_mask payload."""
    if not isinstance(raw, dict):
        raise JyError("VALIDATION", f"{where} 必须是对象")
    resource_type = str(raw.get("resource_type") or "").strip()
    if not resource_type:
        raise JyError("VALIDATION", f"{where}.resource_type 不能为空（线性=line / 圆形=circle …）")
    return {
        "resource_type": resource_type,
        "name": str(raw.get("name") or ""),
        "resource_id": str(raw.get("resource_id") or ""),
        "path": str(raw.get("path") or ""),
        "config": raw.get("config") or {},
        "category": str(raw.get("category") or "video"),
        "category_name": str(raw.get("category_name") or "基础"),
        "category_id": str(raw.get("category_id") or "jichu"),
    }


def _parse_clips(spec, spec_dir, tool, warnings):
    raw = spec.get("clips") or []
    if not isinstance(raw, list):
        raise JyError("VALIDATION", "spec.clips 必须是数组")
    from . import ffmpeg  # local import: keeps this module importable without a toolchain

    clips = []
    cursor = 0.0
    for index, item in enumerate(raw):
        label = f"clips[{index}]"
        if not isinstance(item, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象 {{path,start,duration}}")
        path = _resolve_media(spec_dir, item.get("path"), label)
        probe = ffmpeg.probe_media(tool, str(path))
        if not probe.get("codec") or not probe.get("width") or not probe.get("height"):
            raise JyError("UNSUPPORTED", f"{label} 没有视频流：{path}")
        source_duration = probe["duration_s"] or 0.0
        source_start = _number(item.get("source_start", 0.0), f"{label}.source_start",
                               minimum=0.0)
        start = item.get("start")
        start = cursor if start is None else _number(start, f"{label}.start", minimum=0.0)
        duration = item.get("duration")
        if duration is None:
            duration = max(source_duration - source_start, 0.0)
            if duration <= 0:
                raise JyError(
                    "VALIDATION",
                    f"{label} 未给 duration，且 source_start 之后没有内容",
                )
            warnings.append(f"{label} 未给 duration，使用源文件剩余时长 {duration:.3f}s")
        duration = _number(duration, f"{label}.duration", minimum=0.01)
        if source_duration and source_start + duration > source_duration + 0.05:
            raise JyError(
                "VALIDATION",
                f"{label} 超出源文件时长：source_start {source_start:g}s + duration "
                f"{duration:g}s > {source_duration:g}s（先 trim 或缩短 duration）",
                details={"path": str(path), "source_duration_s": source_duration},
            )
        if start < cursor - 1e-6:
            raise JyError(
                "VALIDATION",
                f"{label}.start={start:g}s 与前一段重叠/乱序（当前游标 {cursor:g}s）；"
                "clips 必须按时间顺序且不重叠",
            )
        clips.append(
            {
                "path": str(path),
                "start": round(start, 3),
                "duration": round(duration, 3),
                "source_start": round(source_start, 3),
                "probe": probe,
                "materials": _parse_clip_materials(item, label, warnings),
            }
        )
        cursor = start + duration
    return clips


def _parse_text_effect(raw, where):
    """Normalise a ``texts[].text_effect`` (花字) declaration."""
    if not isinstance(raw, dict):
        raise JyError("VALIDATION", f"{where} 必须是对象")
    rid = str(raw.get("resource_id") or "").strip()
    if not rid:
        raise JyError("VALIDATION", f"{where}.resource_id 不能为空")
    return {
        "resource_id": rid,
        "name": str(raw.get("name") or ""),
        "path": str(raw.get("path") or ""),
        "category_id": str(raw.get("category_id") or ""),
        "category_name": str(raw.get("category_name") or ""),
        "request_id": str(raw.get("request_id") or ""),
    }


def _parse_text_template(raw, where):
    """``texts[].text_template`` — 文字模板 (verified NEW bucket ``materials.text_templates[]``).

    Field notes from the GUI save: the text track segment's ``material_id`` becomes the
    template id (render_index 14001), and the template's own package supplies its
    stickers/fonts/text panels. ``text_effect`` and ``animations`` are part of the
    template's composition — supply them from the same package; the builder attaches
    them, and they are not optional extras.
    """
    if not isinstance(raw, dict):
        raise JyError("VALIDATION", f"{where} 必须是对象")
    rid = str(raw.get("resource_id") or "").strip()
    if not rid:
        raise JyError("VALIDATION", f"{where}.resource_id 不能为空")
    resources = []
    for index, res in enumerate(raw.get("resources") or []):
        rwhere = f"{where}.resources[{index}]"
        if not isinstance(res, dict):
            raise JyError("VALIDATION", f"{rwhere} 必须是对象 {{panel, resource_id, path}}")
        panel = str(res.get("panel") or "").strip()
        if panel not in ("default", "sticker", "fonts", "text"):
            raise JyError("VALIDATION",
                          f"{rwhere}.panel 只能是 default/sticker/fonts/text，收到 {panel!r}")
        resources.append({"panel": panel,
                          "resource_id": str(res.get("resource_id") or ""),
                          "path": str(res.get("path") or ""),
                          "source_platform": int(res.get("source_platform") or 1)})
    effect = raw.get("text_effect") or None
    if effect is not None and not isinstance(effect, dict):
        raise JyError("VALIDATION", f"{where}.text_effect 必须是对象")
    return {
        "resource_id": rid,
        "name": str(raw.get("name") or ""),
        "path": str(raw.get("path") or ""),
        "category_id": str(raw.get("category_id") or ""),
        "category_name": str(raw.get("category_name") or ""),
        "request_id": str(raw.get("request_id") or ""),
        "resources": resources,
        "text_effect": ({"resource_id": str(effect.get("resource_id") or ""),
                         "path": str(effect.get("path") or "")} if effect else None),
        "animations": (_parse_text_animations(effect.get("animations") or [], f"{where}.text_effect.animations")
                       if effect and effect.get("animations") else []),
    }


def _parse_text_animations(raw, where):
    """``texts[].animations`` -> list of {kind, resource_id, name, duration, path, …}."""
    entries = []
    for index, item in enumerate(raw):
        label = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象")
        kind = str(item.get("type") or "").strip()
        if kind not in ("in", "out", "loop"):
            raise JyError("VALIDATION",
                          f"{label}.type 只能是 in/out/loop（入场/出场/循环），收到 {kind!r}")
        rid = str(item.get("resource_id") or "").strip()
        if not rid:
            raise JyError("VALIDATION", f"{label}.resource_id 不能为空")
        entries.append({
            "kind": kind,
            "resource_id": rid,
            "name": str(item.get("name") or ""),
            "duration": _number(item.get("duration", 0.5), f"{label}.duration", minimum=0.05),
            "path": str(item.get("path") or ""),
            "category_id": str(item.get("category_id") or ""),
            "category_name": str(item.get("category_name") or ""),
            "request_id": str(item.get("request_id") or ""),
        })
    return entries


def _parse_texts(spec, warnings):
    raw = spec.get("texts") or []
    if not isinstance(raw, list):
        raise JyError("VALIDATION", "spec.texts 必须是数组")
    texts = []
    for index, item in enumerate(raw):
        label = f"texts[{index}]"
        if not isinstance(item, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象 {{content,start,duration,style}}")
        unknown = sorted(set(item) - _TEXT_ITEM_KEYS)
        if unknown:
            raise JyError(
                "VALIDATION",
                f"{label} 有未知字段 {unknown}；允许的字段是 {sorted(_TEXT_ITEM_KEYS)}。"
                "颜色/字号/描边必须放在 style 对象里（例如 "
                '"style":{"color":"#FFD400","font_size":14}），放在外层会被剪映忽略并静默渲染成默认白字',
            )
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise JyError("VALIDATION", f"{label}.content 不能为空")
        start = _number(item.get("start", 0.0), f"{label}.start", minimum=0.0)
        duration = _number(item.get("duration", 3.0), f"{label}.duration", minimum=0.01)
        position = item.get("position") or {}
        if not isinstance(position, dict):
            raise JyError("VALIDATION", f"{label}.position 必须是对象 {{x,y}}（归一化到半画布）")
        texts.append(
            {
                "content": content,
                "start": round(start, 3),
                "duration": round(duration, 3),
                "style": _normalise_style(item.get("style"), label),
                "text_effect": (_parse_text_effect(item["text_effect"], f"{label}.text_effect")
                                if item.get("text_effect") else None),
                "animations": (_parse_text_animations(item["animations"], f"{label}.animations")
                               if item.get("animations") else []),
                "text_template": (_parse_text_template(item["text_template"],
                                                       f"{label}.text_template")
                                  if item.get("text_template") else None),
                "position": {
                    "x": _number(position.get("x", 0.0), f"{label}.position.x",
                                 minimum=-2.0, maximum=2.0),
                    "y": _number(position.get("y", 0.0), f"{label}.position.y",
                                 minimum=-2.0, maximum=2.0),
                },
            }
        )
    ordered = sorted(texts, key=lambda t: t["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["start"] < previous["start"] + previous["duration"] - 1e-6:
            raise JyError(
                "VALIDATION",
                "texts 在同一轨道上重叠："
                f"{previous['start']:g}+{previous['duration']:g}s 与 "
                f"{current['start']:g}+{current['duration']:g}s；请错开或拆成多个草稿",
            )
    return texts


def _parse_effect(raw, where, rid):
    """Normalise one ``video_effects[]`` entry from a spec.

    Only the fields 剪映 itself writes are accepted; ``path`` must point at the
    cached package directory (``…/Cache/effect/<resource_id>/<md5>``) or the native
    job fails dependency resolution.
    """
    if not isinstance(raw, dict):
        raise JyError("VALIDATION", f"{where} 必须是对象")
    params = raw.get("adjust_params") or {}
    if isinstance(params, list):          # 剪映 shape passed straight through
        params = {p.get("name"): p.get("value", 1.0)
                  for p in params if isinstance(p, dict) and p.get("name")}
    if not isinstance(params, dict):
        raise JyError("VALIDATION", f"{where}.adjust_params 必须是对象或数组")
    entry = {
        "resource_id": rid(raw.get("resource_id"), where),
        "name": str(raw.get("name") or ""),
        "apply_target_type": int(_number(raw.get("apply_target_type", 2),
                                        f"{where}.apply_target_type", minimum=0, maximum=3)),
        "adjust_params": [
            {"key": key, "value": _number(val, f"{where}.adjust_params.{key}")}
            for key, val in params.items()
        ],
        "path": str(raw.get("path") or ""),
        "category_id": str(raw.get("category_id") or ""),
        "category_name": str(raw.get("category_name") or ""),
        "source_platform": int(_number(raw.get("source_platform", 1),
                                       f"{where}.source_platform", minimum=0, maximum=9)),
        "request_id": str(raw.get("request_id") or ""),
    }
    for optional, default in (("start", None), ("duration", None)):
        if raw.get(optional) is not None:
            entry[optional] = _number(raw.get(optional), f"{where}.{optional}", minimum=0.0)
    return entry


def _parse_effects(spec, warnings):
    """Top-level ``effects[]``: 剪映-style effect tracks over the whole timeline.

    This is the **verified** form: the GUI puts a 画面特效 on its own ``effect``
    track. ``start``/``duration`` are seconds and default to the whole timeline.
    """
    entries = []
    for index, raw in enumerate(spec.get("effects") or []):
        where = f"effects[{index}]"
        entries.append(_parse_effect(
            raw, where, lambda value, _w: _resource_id(value, _w)))
    if entries:
        warnings.append(
            "effects: 画面特效写成独立的 effect 轨道（与剪映 GUI 保存的结构一致）；"
            "素材必须已在剪映本地缓存，否则原生导出报 film_export_dependency_missing。"
        )
    return entries


def _resource_id(raw, where):
    value = str(raw or "").strip()
    if not value:
        raise JyError("VALIDATION", f"{where}.resource_id 不能为空")
    if not value.isdigit():
        raise JyError("VALIDATION", f"{where}.resource_id 必须是十进制数字资源 ID，收到 {value!r}")
    return value


def _parse_clip_materials(item, label, warnings):
    """Normalise the optional effect-family materials declared on one clip.

    Every entry needs an explicit numeric ``resource_id``: 剪映 resolves these
    materials from its own resource cache, and this install ships no local
    transition/filter catalogue, so jy must never invent an id.
    """
    def rid(raw, where):
        value = str(raw or "").strip()
        if not value:
            raise JyError("VALIDATION", f"{where}.resource_id 不能为空")
        if not value.isdigit():
            raise JyError("VALIDATION", f"{where}.resource_id 必须是十进制数字资源 ID，收到 {value!r}")
        return value

    out = {"transition": None, "filters": [], "video_effects": [], "face_beauty": None,
           "matting": None, "keyframes": [], "curve_speed": None, "mask": None,
           "color_wheels": []}

    raw_wheels = item.get("color_wheel")
    if raw_wheels:
        if isinstance(raw_wheels, dict):
            raw_wheels = [raw_wheels]
        if not isinstance(raw_wheels, list):
            raise JyError(
                "VALIDATION",
                f"{label}.color_wheel 必须是对象或数组 "
                "{{segment:gamma|lift|gain|offset, red, green, blue}}",
            )
        for index, wheel in enumerate(raw_wheels):
            where = f"{label}.color_wheel[{index}]"
            if not isinstance(wheel, dict):
                raise JyError("VALIDATION", f"{where} 必须是对象")
            segment = str(wheel.get("segment") or "").strip().lower()
            if segment not in COLOR_WHEEL_SEGMENTS:
                raise JyError(
                    "VALIDATION",
                    f"{where}.segment 必须是 {'/'.join(sorted(COLOR_WHEEL_SEGMENTS))} 之一，"
                    f"收到 {wheel.get('segment')!r}",
                )
            out["color_wheels"].append(
                {
                    "segment": segment,
                    "red": _number(wheel.get("red", 0.0), f"{where}.red",
                                   minimum=-1.0, maximum=1.0),
                    "green": _number(wheel.get("green", 0.0), f"{where}.green",
                                     minimum=-1.0, maximum=1.0),
                    "blue": _number(wheel.get("blue", 0.0), f"{where}.blue",
                                    minimum=-1.0, maximum=1.0),
                    "resource_id": str(wheel.get("resource_id") or ""),
                    "path": str(wheel.get("path") or ""),
                    "hsl": bool(wheel.get("hsl", True)),
                }
            )

    transition = item.get("transition")
    if transition:
        if not isinstance(transition, dict):
            raise JyError("VALIDATION", f"{label}.transition 必须是对象")
        out["transition"] = {
            "resource_id": rid(transition.get("resource_id"), f"{label}.transition"),
            "name": str(transition.get("name") or ""),
            "duration": _number(transition.get("duration", 0.5),
                                f"{label}.transition.duration", minimum=0.0, maximum=30.0),
            "path": str(transition.get("path") or ""),
        }

    for index, filt in enumerate(item.get("filters") or []):
        where = f"{label}.filters[{index}]"
        if not isinstance(filt, dict):
            raise JyError("VALIDATION", f"{where} 必须是对象")
        out["filters"].append({
            "resource_id": rid(filt.get("resource_id"), where),
            "name": str(filt.get("name") or ""),
            "intensity": _number(filt.get("intensity", 100), f"{where}.intensity",
                                 minimum=0.0, maximum=100.0),
            "path": str(filt.get("path") or ""),
        })

    for index, eff in enumerate(item.get("video_effects") or []):
        out["video_effects"].append(
            _parse_effect(eff, f"{label}.video_effects[{index}]", rid))

    if item.get("keyframes"):
        out["keyframes"] = _parse_keyframes(item["keyframes"], f"{label}.keyframes")
    if item.get("curve_speed"):
        out["curve_speed"] = _parse_curve_speed(item["curve_speed"], f"{label}.curve_speed")
    if item.get("mask"):
        out["mask"] = _parse_mask(item["mask"], f"{label}.mask")

    matting = item.get("matting")
    if matting:
        if not isinstance(matting, dict):
            raise JyError("VALIDATION", f"{label}.matting 必须是对象")
        path = str(matting.get("path") or "").strip()
        if not path:
            raise JyError(
                "VALIDATION",
                f"{label}.matting.path 不能为空：抠像需要剪映生成的蒙版目录"
                "（<draft>/matting/<hash>，每帧一张，约 15MB），jy 不能凭空生成")
        out["matting"] = {
            "path": path,
            "flag": int(_number(matting.get("flag", 3), f"{label}.matting.flag",
                                minimum=0, maximum=9)),
            "custom_matting_id": str(matting.get("custom_matting_id") or ""),
        }

    beauty = item.get("face_beauty")
    if beauty:
        if not isinstance(beauty, dict):
            raise JyError("VALIDATION", f"{label}.face_beauty 必须是对象")
        out["face_beauty"] = {
            "category": str(beauty.get("category") or "skin"),
            "params": beauty.get("params") or {},
        }
    if any(out.values()):
        warnings.append(
            f"{label}: transition/filter/video_effect/face_beauty 已写入草稿，但"
            "jy 在本机只验证过文字样式真正渲染；这些素材依赖剪映本地资源缓存，"
            "未缓存时原生导出会报 film_export_dependency_missing。"
        )
    return out


def _is_jy_draft(target):
    """True for a folder jy authored (marker file, or the legacy single-file shape).

    剪映's render job *adds* staging files (draft_content.json, template-2.tmp,
    common_attachment/, …) and normalises draft_info.json, so "only one file"
    cannot be the test after the first render.
    """
    marker = target / MARKER
    if marker.is_file():
        try:
            if json.loads(marker.read_text("utf-8")).get("schema") == MARKER_SCHEMA:
                return True
        except (OSError, json.JSONDecodeError):
            return False
    entries = [entry.name for entry in target.iterdir()]
    return entries == ["draft_info.json"]


def _clear_generated(root):
    """Remove everything except our two files from a jy-authored draft folder.

    剪映's render job normalises the folder and adds staging files
    (``draft_content.json`` first among them), and on the next export that staged
    copy wins over ``draft_info.json``.  A --force rebuild must therefore clear
    them, otherwise the render would silently replay the *previous* timeline.
    Only ever called for a marker-verified jy draft with explicit --force.
    """
    keep = {"draft_info.json", "draft_meta_info.json", MARKER}
    for entry in root.iterdir():
        if entry.name in keep:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)


def _check_output_dir(out_dir, force):
    target = Path(out_dir).expanduser()
    if target.exists() and not target.is_dir():
        raise JyError("VALIDATION", f"输出路径已存在且不是目录：{target}")
    if target.is_dir():
        entries = [entry.name for entry in target.iterdir()]
        if entries and not _is_jy_draft(target):
            foreign = [name for name in entries if name != "draft_info.json"]
            raise JyError(
                "VALIDATION",
                f"拒绝写入非 jy 草稿目录：{target}"
                + (f"（含 {', '.join(sorted(foreign)[:5])}）" if foreign else "")
                + "。为防止覆盖用户真实草稿，请换一个空目录；--force 只对 jy 自己建的草稿生效。",
            )
        if entries and not force:
            raise JyError(
                "VALIDATION",
                f"草稿已存在：{target}/draft_info.json（pass --force to overwrite）",
            )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JyError("VALIDATION", f"无法创建草稿目录 {target}：{exc}") from exc
    return target.resolve()


def build(tool, spec_path, out_dir, *, force=False, warnings=None):
    """Write ``<out_dir>/draft_info.json``; return the envelope data dict."""
    warnings = warnings if warnings is not None else []
    spec_file = Path(spec_path).expanduser().resolve()
    spec = load_spec(spec_file)
    spec_dir = spec_file.parent

    clips = _parse_clips(spec, spec_dir, tool, warnings)
    texts = _parse_texts(spec, warnings)
    spec_effects = _parse_effects(spec, warnings)
    spec_stickers = _parse_stickers(spec, warnings)
    spec_filters = _parse_filters(spec, warnings)
    if not clips and not texts:
        raise JyError("VALIDATION", "spec 至少要有 1 个 clip 或 1 个 text")
    canvas = _canvas_from(spec, clips[0]["probe"] if clips else None)

    root = _check_output_dir(out_dir, force)
    if force and (root / MARKER).is_file():
        _clear_generated(root)
    document = _skeleton()
    document["id"] = _uid()
    document["name"] = root.name
    document["path"] = str(root)
    now = int(time.time())
    document["create_time"] = now
    document["update_time"] = now
    document["canvas_config"] = {
        "background": spec.get("canvas", {}).get("background")
        if isinstance(spec.get("canvas"), dict)
        else None,
        "height": canvas["height"],
        "width": canvas["width"],
        "ratio": "original",
    }
    document["fps"] = canvas["fps"]
    for bucket in ("videos", "texts", "transitions", "video_effects", "effects",
                   "stickers", "material_animations", "manual_beautys", "filters",
                   "common_mask", "speeds", "primary_color_wheels", "hsl"):
        document["materials"].setdefault(bucket, [])
        document["materials"][bucket] = []
    document["tracks"] = []
    document["keyframe_graph_list"] = []
    document["keyframes"] = {
        "adjusts": [],
        "audios": [],
        "effects": [],
        "filters": [],
        "handwrites": [],
        "stickers": [],
        "texts": [],
        "videos": [],
    }

    total_us = 0
    summary_clips = []
    summary_effects = []
    effect_segments = []
    if clips:
        segments = []
        for index, clip in enumerate(clips):
            material_id, video_entry = material_lib.video_material(clip["probe"])
            document["materials"]["videos"].append(video_entry)
            start_us, duration_us = _us(clip["start"]), _us(clip["duration"])
            segment = _segment(
                material_id,
                start_us,
                duration_us,
                source_start_us=_us(clip["source_start"]),
                render_index=index,
            )
            extra = clip.get("materials") or {}
            if extra.get("transition"):
                spec_t = extra["transition"]
                tid, tmat = material_lib.transition_material(
                    spec_t["resource_id"], name=spec_t["name"],
                    duration_us=_us(spec_t["duration"]), path=spec_t["path"])
                document["materials"]["transitions"].append(tmat)
                material_lib.attach(segment, [tid])
                summary_effects.append({"kind": "transition", "clip": index,
                                        "resource_id": spec_t["resource_id"]})
            for spec_f in extra.get("filters") or []:
                fid, fmat = material_lib.filter_material(
                    spec_f["resource_id"], name=spec_f["name"],
                    intensity=spec_f["intensity"], path=spec_f["path"])
                document["materials"]["filters"].append(fmat)
                material_lib.attach(segment, [fid])
                summary_effects.append({"kind": "filter", "clip": index,
                                        "resource_id": spec_f["resource_id"]})
            for spec_e in extra.get("video_effects") or []:
                eid, emat = material_lib.video_effect_material(
                    spec_e["resource_id"], name=spec_e["name"],
                    apply_target_type=spec_e["apply_target_type"],
                    adjust_params=spec_e["adjust_params"], path=spec_e["path"],
                    category_id=spec_e.get("category_id", ""),
                    category_name=spec_e.get("category_name", ""),
                    source_platform=spec_e.get("source_platform", 1),
                    request_id=spec_e.get("request_id") or None)
                document["materials"]["video_effects"].append(emat)
                # 剪映 never binds an effect to a video segment: it gets its own
                # effect track spanning the clip (see effect_track()).
                effect_segments.append(material_lib.effect_track_segment(
                    eid, duration_us))
                summary_effects.append({"kind": "video_effect", "clip": index,
                                        "resource_id": spec_e["resource_id"]})
            if extra.get("keyframes"):
                # Verified container: the SEGMENT's common_keyframes (keyframe_graph_list
                # is null in this build). Easing = per-keyframe Bezier handles.
                segment.setdefault("common_keyframes", [])
                for channel in extra["keyframes"]:
                    segment["common_keyframes"].append(
                        material_lib.keyframe_channel(channel["property"], channel["points"]))
                summary_effects.append({"kind": "keyframes", "clip": index,
                                        "channels": [c["property"] for c in extra["keyframes"]]})

            if extra.get("curve_speed"):
                # 曲线变速 replaces the flat speed material with a curve_speed one.
                for ref_index, ref in enumerate(segment.get("extra_material_refs") or []):
                    if any(sp["id"] == ref for sp in document["materials"]["speeds"]):
                        segment["extra_material_refs"].pop(ref_index)
                        break
                cid, cmat = material_lib.curve_speed_material(
                    extra["curve_speed"]["points"], name=extra["curve_speed"]["name"],
                    preset_id=extra["curve_speed"]["preset_id"])
                document["materials"]["speeds"].append(cmat)
                material_lib.attach(segment, [cid])
                summary_effects.append({"kind": "curve_speed", "clip": index,
                                        "points": len(extra["curve_speed"]["points"])})

            if extra.get("mask"):
                mid, mmat, constant_id = material_lib.mask_material(
                    extra["mask"]["resource_type"], name=extra["mask"]["name"],
                    resource_id=extra["mask"]["resource_id"], path=extra["mask"]["path"],
                    config=extra["mask"]["config"], category=extra["mask"]["category"],
                    category_name=extra["mask"]["category_name"],
                    category_id=extra["mask"]["category_id"])
                document["materials"]["common_mask"].append(mmat)
                material_lib.attach(segment, [mid, constant_id])
                summary_effects.append({"kind": "mask", "clip": index,
                                        "resource_type": extra["mask"]["resource_type"]})

            for wheel in extra.get("color_wheels") or []:
                wid, wmat, hsid, hmat, companion = material_lib.color_wheel_material(
                    wheel["segment"], red=wheel["red"], green=wheel["green"],
                    blue=wheel["blue"], resource_id=wheel["resource_id"],
                    path=wheel["path"], hsl=wheel["hsl"],
                )
                document["materials"].setdefault("primary_color_wheels", []).append(wmat)
                refs = [wid]
                if hmat is not None:
                    document["materials"].setdefault("hsl", []).append(hmat)
                    refs.append(hsid)
                material_lib.attach(segment, refs)
                summary_effects.append({"kind": "color_wheel", "clip": index,
                                        "segment": wheel["segment"],
                                        "hsl": bool(wheel["hsl"])})

            if extra.get("matting"):
                # 智能抠像 lands on the VIDEO MATERIAL (verified): a matting dict whose
                # path points at 剪映's per-frame mask dir inside the draft folder.
                video_entry["matting"] = {
                    "flag": int(extra["matting"]["flag"]),
                    "path": extra["matting"]["path"],
                    "custom_matting_id": extra["matting"]["custom_matting_id"],
                }
                summary_effects.append({"kind": "matting", "clip": index,
                                        "flag": extra["matting"]["flag"]})
            if extra.get("face_beauty"):
                # 美颜 lives in materials.effects[] (type "figure"), NOT manual_beautys;
                # the owning video material is flagged is_set_beauty_mode.
                bid, bmats = material_lib.beauty_effect_materials(
                    extra["face_beauty"]["params"])
                document["materials"]["effects"].extend(bmats)
                material_lib.attach(segment, bid)
                video_entry["is_set_beauty_mode"] = True
                video_entry.setdefault("beauty_face_auto_preset", {})
                summary_effects.append({"kind": "face_beauty", "clip": index,
                                        "params": list((extra["face_beauty"]["params"] or {}))})
            segments.append(segment)
            total_us = max(total_us, start_us + duration_us)
            summary_clips.append({key: clip[key] for key in ("path", "start", "duration",
                                                             "source_start")})
        document["tracks"].append(_track("video", segments))

    # 画面特效 live on their own ``effect`` track (verified against the GUI).
    # Top-level ``effects[]`` entries may carry start/duration; they default to the
    # whole timeline, which is how 剪映 writes a single full-length effect.
    for spec_e in spec_effects:
        eid, emat = material_lib.video_effect_material(
            spec_e["resource_id"], name=spec_e["name"],
            apply_target_type=spec_e["apply_target_type"],
            adjust_params=spec_e["adjust_params"], path=spec_e["path"],
            category_id=spec_e.get("category_id", ""),
            category_name=spec_e.get("category_name", ""),
            source_platform=spec_e.get("source_platform", 1),
            request_id=spec_e.get("request_id") or None)
        document["materials"]["video_effects"].append(emat)
        start_us = _us(spec_e.get("start") or 0.0)
        duration_us = _us(spec_e["duration"]) if spec_e.get("duration") else max(
            total_us - start_us, 0)
        effect_segments.append(material_lib.effect_track_segment(
            eid, duration_us, start_us=start_us))
        summary_effects.append({"kind": "video_effect",
                                "resource_id": spec_e["resource_id"],
                                "start": start_us / 1e6, "duration": duration_us / 1e6})


    summary_texts = []
    if texts:
        if not clips:
            warnings.append(
                "草稿只有文字轨道：剪映会把它渲染在背景上（颜色/字号/位置由剪映默认模板决定）"
            )
        segments = []
        for text in sorted(texts, key=lambda item: item["start"]):
            effect = text.get("text_effect")
            material_id, text_material = material_lib.text_material(
                text["content"], text["style"], canvas_width=canvas["width"],
                text_effect=effect
            )
            document["materials"]["texts"].append(text_material)
            start_us, duration_us = _us(text["start"]), _us(text["duration"])
            text_segment = _segment(material_id, start_us, duration_us,
                                    render_index=TEXT_RENDER_INDEX)
            # Styled text also needs a sticker_animation material slot, which is what
            # 剪映 writes for every text segment it creates itself.
            anim_id = _uid()
            anim_slot = {"id": anim_id, "type": "sticker_animation", "animations": [],
                         "multi_language_current": "none"}
            template = text.get("text_template")
            if template:
                # 文字模板 FORKS the segment: its material_id becomes the text_template id,
                # the render_index is 14001, and the text material + template's own
                # text_effect + animations all ride in extra_material_refs.
                tid, tmat = material_lib.text_template_material(
                    template["resource_id"], name=template["name"], path=template["path"],
                    resources=template["resources"], category_id=template["category_id"],
                    category_name=template["category_name"],
                    request_id=template["request_id"] or None)
                document["materials"]["text_templates"].append(tmat)
                text_segment["material_id"] = tid
                text_segment["render_index"] = 14001
                material_lib.attach(text_segment, [material_id])
                tpl_effect = template.get("text_effect")
                if tpl_effect and tpl_effect.get("resource_id"):
                    eid, emat = material_lib.text_effect_material(
                        tpl_effect["resource_id"], name=template["name"],
                        path=tpl_effect["path"])
                    document["materials"]["effects"].append(emat)
                    material_lib.attach(text_segment, [eid])
                for spec_anim in template.get("animations") or []:
                    anim_slot["animations"].append(material_lib.text_animation_entry(
                        spec_anim["resource_id"], spec_anim["kind"], name=spec_anim["name"],
                        duration_us=_us(spec_anim["duration"]), path=spec_anim["path"],
                        category_id=spec_anim["category_id"],
                        category_name=spec_anim["category_name"]))
                summary_effects.append({"kind": "text_template", "text": text["content"][:20],
                                        "template": template["name"],
                                        "resource_id": template["resource_id"]})
            # 文字动画 (入场/出场/循环) live in this same slot: animations[] entries with
            # type in/out/loop — see material.text_animation_entry().
            for spec_anim in text.get("animations") or []:
                anim_slot["animations"].append(material_lib.text_animation_entry(
                    spec_anim["resource_id"], spec_anim["kind"], name=spec_anim["name"],
                    duration_us=_us(spec_anim["duration"]), path=spec_anim["path"],
                    category_id=spec_anim["category_id"],
                    category_name=spec_anim["category_name"],
                    request_id=spec_anim["request_id"] or None))
                summary_effects.append({"kind": "text_animation", "clip": index,
                                        "text": text["content"][:20],
                                        "animation": spec_anim["name"] or spec_anim["kind"]})
            document["materials"]["material_animations"].append(anim_slot)
            material_lib.attach(text_segment, [anim_id])
            if effect:
                eid, emat = material_lib.text_effect_material(
                    effect["resource_id"], name=effect["name"], path=effect["path"],
                    category_id=effect["category_id"],
                    category_name=effect["category_name"],
                    request_id=effect["request_id"] or None)
                document["materials"]["effects"].append(emat)
                material_lib.attach(text_segment, [eid])
                summary_effects.append({"kind": "text_effect", "text": text["content"][:20],
                                        "resource_id": effect["resource_id"]})
            text_segment["clip"]["transform"] = {
                "x": round(text["position"]["x"], 4),
                "y": round(text["position"]["y"], 4),
            }
            segments.append(text_segment)
            total_us = max(total_us, start_us + duration_us)
            summary_texts.append(
                {
                    "content": text["content"],
                    "start": text["start"],
                    "duration": text["duration"],
                    "style": text["style"],
                    "position": text["position"],
                }
            )
        document["tracks"].append(_track("text", segments))

    if effect_segments:
        document["tracks"].append(effect_track(effect_segments))

    if spec_filters:
        filter_segments = []
        for spec_f in spec_filters:
            fid, fmat = material_lib.filter_effect_material(
                spec_f["resource_id"], name=spec_f["name"], path=spec_f["path"],
                value=spec_f["value"], category_id=spec_f["category_id"],
                category_name=spec_f["category_name"],
                request_id=spec_f["request_id"] or None)
            document["materials"]["effects"].append(fmat)
            start_us = _us(spec_f["start"])
            duration_us = _us(spec_f["duration"]) if spec_f["duration"] else max(
                total_us - start_us, 0)
            filter_segments.append(material_lib.filter_track_segment(
                fid, duration_us, start_us=start_us))
            summary_effects.append({"kind": "filter", "resource_id": spec_f["resource_id"],
                                    "start": start_us / 1e6, "duration": duration_us / 1e6})
        document["tracks"].append(filter_track(filter_segments))

    if spec_stickers:
        sticker_segments = []
        for spec_s in spec_stickers:
            sid, smat = material_lib.sticker_material(
                spec_s["resource_id"], path=spec_s["path"],
                sticker_id=spec_s["sticker_id"] or None)
            document["materials"]["stickers"].append(smat)
            start_us = _us(spec_s["start"])
            duration_us = _us(spec_s["duration"]) if spec_s["duration"] else max(
                total_us - start_us, 0)
            sticker_segments.append(material_lib.sticker_track_segment(
                sid, start_us, duration_us, scale=spec_s["scale"],
                transform=(spec_s["position"]["x"], spec_s["position"]["y"])))
            summary_effects.append({"kind": "sticker", "resource_id": spec_s["resource_id"],
                                    "start": start_us / 1e6, "duration": duration_us / 1e6})
        document["tracks"].append(sticker_track(sticker_segments))

    requested_duration = spec.get("duration")
    if requested_duration is not None:
        requested_us = _us(_number(requested_duration, "spec.duration", minimum=0.01))
        if requested_us < total_us:
            raise JyError(
                "VALIDATION",
                f"spec.duration={requested_duration:g}s 短于时间线内容 {_secs(total_us)}s",
            )
        total_us = requested_us
    document["duration"] = total_us

    draft_file = root / "draft_info.json"
    draft_file.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    meta_file = root / "draft_meta_info.json"
    meta_file.write_text(
        json.dumps(meta_info(document, root, created_us=now * 1_000_000,
                             modified_us=now * 1_000_000), ensure_ascii=False),
        encoding="utf-8",
    )
    digest = hashlib.sha256(draft_file.read_bytes()).hexdigest()
    marker_file = root / MARKER
    marker_file.write_text(
        json.dumps(
            {
                "schema": MARKER_SCHEMA,
                "builder": "jy",
                "draft_sha256": digest,
                "created": now,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    if summary_texts:
        warnings.append(
            "文字样式由样式块（content.styles）驱动，剪映会真正渲染字号/颜色/描边；"
            "font_size 是剪映字号单位（1080 宽画布约 6.7px/单位），"
            "位置用 position.x/y（归一化到半画布，y 向上为正），alignment 只影响多行对齐。"
        )

    return {
        "engine": "jianying",
        "draft_path": str(root),
        "draft_info": str(draft_file),
        "draft_sha256": digest,
        "duration_s": _secs(total_us),
        "canvas": canvas,
        "tracks": [
            {"type": track["type"], "segments": len(track["segments"])}
            for track in document["tracks"]
        ],
        "clips": summary_clips,
        "texts": summary_texts,
        "effects": summary_effects,
        "files": [str(draft_file), str(meta_file), str(marker_file)],
    }


# --------------------------------------------------------------------------- #
# draft_info.json resolution (single-file and multi-timeline layouts)
# --------------------------------------------------------------------------- #


def resolve_draft_info(draft_dir):
    """Locate the real ``draft_info.json`` for a draft folder.

    剪映 migrates a jy-authored draft to the multi-timeline layout the first time it
    opens it: the root ``draft_info.json`` disappears and the live one moves to
    ``<draft>/Timelines/<uuid>/draft_info.json`` (main timeline named in
    ``Timelines/project.json`` → ``main_timeline_id``). Everything that reads or
    hashes a draft must go through this resolver.

    Returns ``{"draft_dir", "info_path", "layout", "timeline_id"}``; ``info_path`` is
    None when neither layout exists.
    """
    root = Path(draft_dir).expanduser().resolve()
    root_info = root / "draft_info.json"
    if root_info.is_file():
        return {"draft_dir": root, "info_path": root_info, "layout": "single",
                "timeline_id": None}
    timelines = root / "Timelines"
    timeline_id = None
    candidate = None
    project = timelines / "project.json"
    if project.is_file():
        try:
            main_id = str(json.loads(project.read_text("utf-8")).get("main_timeline_id") or "")
        except (OSError, json.JSONDecodeError):
            main_id = ""
        if main_id:
            maybe = timelines / main_id / "draft_info.json"
            if maybe.is_file():
                candidate, timeline_id = maybe, main_id
    if candidate is None and timelines.is_dir():
        found = sorted(
            timelines.glob("*/draft_info.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if found:
            candidate = found[0]
            timeline_id = candidate.parent.name
    return {"draft_dir": root, "info_path": candidate,
            "layout": "multi-timeline" if candidate else "missing",
            "timeline_id": timeline_id}

