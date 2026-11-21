"""Strict draft-spec parsing, per-clip properties and 剪映 staging (batch C1/C2).

``jy_cli/draft.py`` renders the timeline; this module is the *gatekeeper* in front
of it for the fields jy promises to understand:

* unknown keys raise ``VALIDATION`` instead of being silently dropped (the text
  style bug that produced white text came exactly from a silently-ignored key);
* per-clip ``transform`` / ``opacity`` / ``volume`` / ``crop`` / ``speed`` /
  ``render_index`` are validated here and applied to the produced
  ``draft_info.json`` (draft.py stays untouched);
* sources outside a TCC-protected folder are staged into a jy-owned dir under
  ``~/Movies`` (or ``$JY_STAGING_DIR``) before the draft references them, because a
  ``~/Downloads`` reference makes macOS show a modal TCC prompt that wedges 剪映.

Nothing here touches the network or the GUI.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import os
import shutil
from pathlib import Path

from ..envelope import JyError

SPEC_KEYS = {
    "canvas",
    "clips",
    "texts",
    "duration",
    "effects",
    "filters",
    "stickers",
}

# Everything draft.py consumes per clip, plus the properties batch C adds.
CLIP_KEYS = {
    "path",
    "start",
    "duration",
    "source_start",
    "transition",
    "filters",
    "video_effects",
    "face_beauty",
    "matting",
    # agent-bridge's keyframed/curve-speed/mask materials (draft.py validates deeply)
    "keyframes",
    "curve_speed",
    "mask",
    "color_wheel",
    # batch C properties (applied by this module)
    "transform",
    "opacity",
    "volume",
    "crop",
    "speed",
    "render_index",
}

COLOR_WHEEL_SEGMENTS = {"gamma", "lift", "gain", "offset"}
COLOR_WHEEL_KEYS = {"segment", "red", "green", "blue", "resource_id", "path", "hsl"}
TRANSFORM_KEYS = {"x", "y", "scale", "rotation"}
CROP_KEYS = {"x", "y", "w", "h"}
ANIMATION_TYPES = {"in", "out", "loop"}
# 文字模板（剪映 2024+ 的新桶）：主声明 + 可选的 per-panel 资源列表
TEXT_TEMPLATE_KEYS = {
    "resource_id",
    "name",
    "path",
    "category_id",
    "category_name",
    "resources",
}
TEXT_TEMPLATE_RESOURCE_KEYS = {"panel", "resource_id", "path", "source_platform"}
TEXT_TEMPLATE_PANELS = ("default", "sticker", "fonts", "text")
TEXT_FALLBACK_KEYS = {
    "content",
    "start",
    "duration",
    "style",
    "position",
    "text_effect",
    "animations",
}


def text_item_keys():
    """Allowed text keys, following draft.py's own list when it is importable.

    draft.py owns the text schema (agent-bridge); reading its constant keeps this
    gatekeeper from drifting, while ``animations`` still gets a shape check here.
    """
    try:
        from .. import draft as draft_module

        keys = getattr(draft_module, "_TEXT_ITEM_KEYS", None)
        if keys:
            return set(keys)
    except Exception:  # noqa: BLE001 - never let a probe break the build
        pass
    return set(TEXT_FALLBACK_KEYS)


def _validate_text_template(raw, where):
    """Validate texts[].text_template: one resource_id (+ per-panel resources).

    Kept here (not in draft.py) so a typo names the field and suggests the closest
    key, exactly like every other spec field. draft.py still owns the payload shape.
    """
    if not isinstance(raw, dict):
        raise JyError(
            "VALIDATION",
            f"{where} 必须是对象 {{resource_id, name?, path?, resources?}}",
        )
    _reject_unknown(raw, TEXT_TEMPLATE_KEYS, where)
    resource_id = raw.get("resource_id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise JyError(
            "VALIDATION",
            f"{where}.resource_id 必填，且必须是非空字符串（剪映资源 id，jy 不会编造）",
        )
    resources = raw.get("resources")
    if resources is None:
        return raw
    if not isinstance(resources, list):
        raise JyError("VALIDATION", f"{where}.resources 必须是数组")
    for index, item in enumerate(resources):
        label = f"{where}.resources[{index}]"
        if not isinstance(item, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象 {{panel, resource_id}}")
        _reject_unknown(item, TEXT_TEMPLATE_RESOURCE_KEYS, label)
        panel = str(item.get("panel") or "").strip().lower()
        if panel not in TEXT_TEMPLATE_PANELS:
            close = difflib.get_close_matches(panel, list(TEXT_TEMPLATE_PANELS), n=2)
            raise JyError(
                "VALIDATION",
                f"{label}.panel 必须是 {'/'.join(TEXT_TEMPLATE_PANELS)} 之一，"
                f"收到 {item.get('panel')!r}"
                + (f"；是不是想用 {close}" if close else ""),
            )
        rid = item.get("resource_id")
        if not isinstance(rid, str) or not rid.strip():
            raise JyError(
                "VALIDATION", f"{label}.resource_id 必填（每个 panel 都要一个资源 id）"
            )
        if "source_platform" in item and not isinstance(
            item["source_platform"], (int, float)
        ):
            raise JyError(
                "VALIDATION", f"{label}.source_platform 必须是数字"
            )
    return raw


def _validate_animations(raw, where):
    if not isinstance(raw, list):
        raise JyError("VALIDATION", f"{where} 必须是数组（入场/出场/循环动画）")
    for index, item in enumerate(raw):
        label = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象 {{type,resource_id,…}}")
        kind = str(item.get("type") or "").strip().lower()
        if kind not in ANIMATION_TYPES:
            raise JyError(
                "VALIDATION",
                f"{label}.type 必须是 in/out/loop 之一（入场/出场/循环），收到 {item.get('type')!r}",
            )
        if not str(item.get("resource_id") or "").strip():
            raise JyError(
                "VALIDATION",
                f"{label}.resource_id 不能为空：剪映从自己的资源缓存解析动画，jy 不会编造 id",
            )
    return raw


def _suggest(key, candidates):
    matches = difflib.get_close_matches(key, sorted(candidates), n=3, cutoff=0.5)
    return f"；是不是想用 {matches}" if matches else ""


def _reject_unknown(mapping, allowed, where):
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise JyError(
            "VALIDATION",
            f"{where} 有未知字段 {unknown}；允许的字段是 {sorted(allowed)}"
            + _suggest(unknown[0], allowed)
            + "。jy 不会静默忽略未知字段（曾因此把文字样式写成默认白字）。",
            details={"where": where, "unknown": unknown, "allowed": sorted(allowed)},
        )


def _number(value, where, *, minimum=None, maximum=None):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise JyError("VALIDATION", f"{where} 必须是数字，得到 {value!r}") from None
    if minimum is not None and numeric < minimum:
        raise JyError("VALIDATION", f"{where} 必须 >= {minimum:g}（得到 {numeric:g}）")
    if maximum is not None and numeric > maximum:
        raise JyError("VALIDATION", f"{where} 必须 <= {maximum:g}（得到 {numeric:g}）")
    return numeric


def validate_spec(spec, *, where="spec"):
    """Reject unknown keys and normalise the batch-C per-clip properties."""
    if not isinstance(spec, dict):
        raise JyError("VALIDATION", f"{where} 必须是 JSON 对象")
    _reject_unknown(spec, SPEC_KEYS, where)

    clips = spec.get("clips") or []
    if not isinstance(clips, list):
        raise JyError("VALIDATION", f"{where}.clips 必须是数组")
    props = []
    for index, clip in enumerate(clips):
        label = f"{where}.clips[{index}]"
        if not isinstance(clip, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象")
        _reject_unknown(clip, CLIP_KEYS, label)
        props.append(_validate_props(clip, label))

    texts = spec.get("texts") or []
    if not isinstance(texts, list):
        raise JyError("VALIDATION", f"{where}.texts 必须是数组")
    allowed_text = text_item_keys()
    for index, text in enumerate(texts):
        label = f"{where}.texts[{index}]"
        if not isinstance(text, dict):
            raise JyError("VALIDATION", f"{label} 必须是对象")
        if text.get("animations") is not None:
            _validate_animations(text["animations"], f"{label}.animations")
        if text.get("text_template") is not None:
            _validate_text_template(text["text_template"], f"{label}.text_template")
        _reject_unknown(text, allowed_text, label)
    return props


def _validate_props(clip, label):
    """Normalise transform/opacity/volume/crop/speed/render_index for one clip."""
    props = {"index": int(label.rsplit("[", 1)[1].rstrip("]"))}
    transform = clip.get("transform") or {}
    if transform:
        if not isinstance(transform, dict):
            raise JyError("VALIDATION", f"{label}.transform 必须是对象 {{x,y,scale,rotation}}")
        _reject_unknown(transform, TRANSFORM_KEYS, f"{label}.transform")
        props["transform"] = {
            "x": _number(transform.get("x", 0.0), f"{label}.transform.x"),
            "y": _number(transform.get("y", 0.0), f"{label}.transform.y"),
            "scale": _number(
                transform.get("scale", 1.0), f"{label}.transform.scale", minimum=0.01
            ),
            "rotation": _number(
                transform.get("rotation", 0.0), f"{label}.transform.rotation"
            ),
        }
    if "opacity" in clip:
        props["opacity"] = _number(clip["opacity"], f"{label}.opacity", minimum=0.0, maximum=1.0)
    if "volume" in clip:
        props["volume"] = _number(clip["volume"], f"{label}.volume", minimum=0.0, maximum=8.0)
    if "speed" in clip:
        props["speed"] = _number(clip["speed"], f"{label}.speed", minimum=0.05, maximum=20.0)
    if "render_index" in clip:
        props["render_index"] = int(
            _number(clip["render_index"], f"{label}.render_index", minimum=0, maximum=100000)
        )
    wheels = clip.get("color_wheel")
    if wheels is not None:
        if isinstance(wheels, dict):
            wheels = [wheels]
        if not isinstance(wheels, list):
            raise JyError(
                "VALIDATION",
                f"{label}.color_wheel 必须是对象或数组 "
                "{{segment:gamma|lift|gain|offset, red, green, blue}}",
            )
        normalised = []
        for index, wheel in enumerate(wheels):
            where = f"{label}.color_wheel[{index}]"
            if not isinstance(wheel, dict):
                raise JyError("VALIDATION", f"{where} 必须是对象")
            _reject_unknown(wheel, COLOR_WHEEL_KEYS, where)
            segment = str(wheel.get("segment") or "").strip().lower()
            if segment not in COLOR_WHEEL_SEGMENTS:
                raise JyError(
                    "VALIDATION",
                    f"{where}.segment 必须是 {'/'.join(sorted(COLOR_WHEEL_SEGMENTS))} 之一，"
                    f"收到 {wheel.get('segment')!r}",
                )
            entry = {"segment": segment}
            for channel in ("red", "green", "blue"):
                if channel in wheel:
                    entry[channel] = _number(
                        wheel[channel], f"{where}.{channel}", minimum=-1.0, maximum=1.0
                    )
            for optional in ("resource_id", "path"):
                if optional in wheel:
                    entry[optional] = str(wheel[optional])
            if "hsl" in wheel:
                entry["hsl"] = bool(wheel["hsl"])
            normalised.append(entry)
        props["color_wheel"] = normalised
    keyframes = clip.get("keyframes")
    if keyframes is not None:
        if isinstance(keyframes, dict):
            keyframes = [keyframes]
        if not isinstance(keyframes, list) or not all(
            isinstance(item, dict) for item in keyframes
        ):
            raise JyError(
                "VALIDATION",
                f"{label}.keyframes 必须是数组（每项 {{property, points}}）或 "
                "{通道名: points} 的对象；property 用剪映通道名如 KFTypeScaleX",
            )
        props["keyframes"] = keyframes
    if "curve_speed" in clip and clip["curve_speed"] is not None:
        curve = clip["curve_speed"]
        if not isinstance(curve, dict):
            raise JyError(
                "VALIDATION",
                f"{label}.curve_speed 必须是对象 {{name,id,points:[{{x,y}},…]}}",
            )
        props["curve_speed"] = curve
    if "mask" in clip and clip["mask"] is not None:
        mask = clip["mask"]
        if not isinstance(mask, dict):
            raise JyError(
                "VALIDATION", f"{label}.mask 必须是对象 {{resource_type,name,resource_id,path,config}}"
            )
        if not str(mask.get("resource_type") or "").strip():
            raise JyError(
                "VALIDATION",
                f"{label}.mask.resource_type 不能为空（linear/line=线性、circle=圆形、rect=矩形…）",
            )
        props["mask"] = mask
    crop = clip.get("crop")
    if crop:
        if not isinstance(crop, dict):
            raise JyError(
                "VALIDATION",
                f"{label}.crop 必须是对象 {{x,y,w,h}}（0..1 归一化，左上角原点）",
            )
        _reject_unknown(crop, CROP_KEYS, f"{label}.crop")
        for key in sorted(CROP_KEYS):
            if key in crop:
                _number(crop[key], f"{label}.crop.{key}", minimum=0.0, maximum=1.0)
        x = _number(crop.get("x", 0.0), f"{label}.crop.x", minimum=0.0, maximum=1.0)
        y = _number(crop.get("y", 0.0), f"{label}.crop.y", minimum=0.0, maximum=1.0)
        w = _number(crop.get("w", 1.0), f"{label}.crop.w", minimum=0.01, maximum=1.0)
        h = _number(crop.get("h", 1.0), f"{label}.crop.h", minimum=0.01, maximum=1.0)
        if x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
            raise JyError(
                "VALIDATION",
                f"{label}.crop 超出画面：x+w={x + w:g} / y+h={y + h:g} 必须 <= 1.0",
            )
        props["crop"] = {"x": x, "y": y, "w": w, "h": h}
    return props


def _crop_polygon(crop):
    """0..1 crop box -> 剪映's four-corner polygon (top-left origin)."""
    x, y, w, h = crop["x"], crop["y"], crop["w"], crop["h"]
    return {
        "upper_left_x": x,
        "upper_left_y": y,
        "upper_right_x": x + w,
        "upper_right_y": y,
        "lower_left_x": x,
        "lower_left_y": y + h,
        "lower_right_x": x + w,
        "lower_right_y": y + h,
    }


def apply_clip_properties(draft_path, props):
    """Write the per-clip properties into a built draft_info.json.

    Segments on the video track are mapped to spec clips by order (that is the
    order draft.py emits them in). Mismatched counts are never guessed at: the
    patch is skipped and reported.
    """
    path = Path(draft_path)
    document = json.loads(path.read_text("utf-8"))
    tracks = document.get("tracks") or []
    video_segments = []
    for track in tracks:
        if track.get("type") == "video":
            video_segments.extend(track.get("segments") or [])
            break
    wanted = [item for item in props if item]
    if len(video_segments) != len(wanted):
        return {
            "applied": 0,
            "skipped": True,
            "reason": f"video segments={len(video_segments)} 与 spec clips={len(wanted)} 不一致，"
            "未写入逐片段属性（避免错位）",
        }

    materials = {item.get("id"): item for item in (document.get("materials") or {}).get("videos") or []}
    applied = []
    for segment, prop in zip(video_segments, wanted):
        touched = []
        clip = segment.setdefault("clip", {})
        transform = prop.get("transform")
        if transform:
            clip["transform"] = {"x": transform["x"], "y": transform["y"]}
            scale = transform["scale"]
            clip["scale"] = {"x": scale, "y": scale}
            clip["rotation"] = transform["rotation"]
            touched.extend(["transform", "scale", "rotation"])
        if "opacity" in prop:
            clip["alpha"] = prop["opacity"]
            touched.append("opacity")
        if "volume" in prop:
            segment["volume"] = prop["volume"]
            segment["last_nonzero_volume"] = prop["volume"]
            touched.append("volume")
        if "speed" in prop:
            segment["speed"] = prop["speed"]
            touched.append("speed")
        if "render_index" in prop:
            segment["render_index"] = prop["render_index"]
            touched.append("render_index")
        crop = prop.get("crop")
        if crop:
            material = materials.get(segment.get("material_id"))
            if material is not None:
                material["crop"] = _crop_polygon(crop)
                material["crop_ratio"] = "free"
                material["crop_scale"] = 1.0
                touched.append("crop")
        if touched:
            applied.append({"clip": prop["index"], "fields": touched})
    path.write_text(json.dumps(document, ensure_ascii=False), "utf-8")
    return {"applied": len(applied), "skipped": False, "clips": applied}


# ---------------------------------------------------------------------------
# staging (C1)


def staging_root():
    override = os.environ.get("JY_STAGING_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Movies" / "jy-staging"


def stage_clips(spec, spec_dir, *, root=None, warnings=None):
    """Copy/hardlink every clip into jy's ~/Movies staging dir.

    Returns ``(staged_spec, report)``; report entries are
    ``{original, staged, copied, bytes, linked}``. Staging is skipped when the
    source already lives under the staging root.
    """
    warnings = warnings if warnings is not None else []
    root = Path(root) if root else staging_root()
    root.mkdir(parents=True, exist_ok=True)
    staged_spec = json.loads(json.dumps(spec))
    report = []
    for index, clip in enumerate(staged_spec.get("clips") or []):
        raw = clip.get("path")
        if not raw:
            continue
        if re.match(r"^[A-Za-z]:[\\/]", str(raw)):
            report.append({"original": raw, "staged": raw, "copied": False, "bytes": None,
                           "linked": False, "reason": "remote PC media (--media-host): not staged"})
            continue
        source = Path(raw).expanduser()
        if not source.is_absolute():
            source = (Path(spec_dir) / source).resolve()
        if not source.is_file():
            raise JyError("NOT_FOUND", f"clips[{index}] 的源文件不存在：{source}")
        try:
            source.relative_to(root.resolve())
            inside = True
        except ValueError:
            inside = False
        if inside:
            report.append(
                {
                    "original": str(source),
                    "staged": str(source),
                    "copied": False,
                    "bytes": source.stat().st_size,
                    "linked": False,
                    "reason": "already inside the staging dir",
                }
            )
            continue
        digest = hashlib.sha256(
            f"{source}:{source.stat().st_size}:{source.stat().st_mtime_ns}".encode()
        ).hexdigest()[:10]
        target_dir = root / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        linked = False
        copied = False
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            try:
                os.link(source, target)
                linked = True
            except OSError:
                shutil.copy2(source, target)
                copied = True
        report.append(
            {
                "original": str(source),
                "staged": str(target),
                "copied": copied,
                "linked": linked,
                "bytes": target.stat().st_size,
            }
        )
        clip["path"] = str(target)
    local = [r for r in report if not str(r.get("reason", "")).startswith("remote PC media")]
    if len(local) < len(report):
        warnings.append(f"{len(report) - len(local)} 个素材在远程 PC 上（--media-host），不 stage，草稿里原样引用 Windows 路径。")
    if local:
        warnings.append(
            f"已把 {len(local)} 个素材 stage 到 {root}（硬链接优先，跨卷才复制）："
            "这样草稿不会引用 ~/Downloads 等受 TCC 保护的目录（那会弹模态权限框并卡死剪映）。"
        )
    return staged_spec, report
