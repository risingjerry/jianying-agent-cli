"""``jy sfx`` — a local sound-effect library, fed by 剪映's own download cache.

Why this exists: when you download a sound effect in 剪映's panel, the app drops a
**plain, complete MP3** into ``User Data/Cache/music/`` (measured: 2,970–7,568 B,
0.34–0.47 s, 65/128 kbps — all playable). So the agent can reuse those files
directly: no network, no credits, no GUI, no draft.

Two sources are indexed:

* ``$JY_SFX_DIR`` (default ``~/Movies/jy-sfx``) — your own packs, recursive;
  file names become searchable names/tags (``叮-提示音.mp3`` → 叮, 提示, ding, hint);
* 剪映's caches (``Cache/{music,effect,artistEffect,AITextTemplate}``) — read-only
  harvest. Those files are named by hash, so the entry carries the ``resource_id``
  from ``downLoadcfg`` and starts unlabelled; ``jy sfx label`` names it once and
  ``jy sfx search`` finds it from then on.

Nothing here writes to or deletes from 剪映's cache. Only ``index.json`` in jy's own
directory is written (atomic replace).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .. import ffmpeg
from ..envelope import JyError
from . import _common as common

SCHEMA = "jy.sfx.v1"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".wma", ".aiff"}
CACHE_ROOT = Path.home() / "Movies" / "JianyingPro" / "User Data" / "Cache"
CACHE_DIRS = ("music", "effect", "artistEffect", "AITextTemplate")

# name token -> extra english/中文 tags the agent is likely to search for
SYNONYMS = {
    "叮": ["ding", "bell", "chime", "hint", "notification"],
    "提示": ["hint", "notification", "prompt", "ding"],
    "通知": ["notification", "hint", "ding"],
    "消息": ["message", "notification", "ding"],
    "转场": ["transition", "whoosh", "swish"],
    "嗖": ["whoosh", "swish", "transition"],
    "笑": ["laugh", "laughing", "giggle"],
    "笑声": ["laugh", "laughing"],
    "砰": ["bang", "hit", "impact", "boom"],
    "撞": ["hit", "impact", "crash"],
    "打击": ["hit", "impact", "percussion"],
    "点击": ["click", "tap", "ui"],
    "咔": ["click", "snap", "cut"],
    "敲": ["typing", "knock", "click"],
    "打字": ["typing", "keyboard"],
    "鼓": ["drum", "beat", "percussion"],
    "节拍": ["beat", "rhythm"],
    "掌声": ["applause", "clap", "crowd"],
    "鼓掌": ["applause", "clap"],
    "错误": ["error", "wrong", "buzz"],
    "失败": ["fail", "error", "buzz"],
    "成功": ["success", "ding", "win"],
    "上升": ["rise", "up", "whoosh"],
    "下降": ["fall", "down", "whoosh"],
    "心跳": ["heartbeat", "heart", "pulse"],
    "铃": ["bell", "ring"],
    "铃声": ["bell", "ring", "phone"],
    "风声": ["wind", "ambience", "air"],
    "雨": ["rain", "ambience", "water"],
    "水": ["water", "splash", "drop"],
    "关门": ["door", "close", "slam"],
    "开门": ["door", "open", "creak"],
    "脚步": ["footsteps", "walk", "step"],
    "玻璃": ["glass", "break", "shatter"],
    "爆炸": ["explosion", "boom", "blast"],
    "魔法": ["magic", "sparkle", "chime"],
    "可爱": ["cute", "cartoon", "bubble"],
    "滑稽": ["funny", "comedy", "cartoon"],
    "紧张": ["tension", "suspense", "riser"],
    "悬疑": ["suspense", "mystery", "dark"],
    "治愈": ["calm", "heal", "soft"],
    "悲伤": ["sad", "emotional", "sorrow"],
    "欢快": ["happy", "upbeat", "cheerful"],
}
NON_TOKEN = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
CJK = re.compile(r"[\u4e00-\u9fff]")
ASCII_TOKEN = re.compile(r"[a-z0-9]+")
HASH_NAME = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


def sfx_dir():
    override = os.environ.get("JY_SFX_DIR")
    return Path(override).expanduser() if override else Path.home() / "Movies" / "jy-sfx"


def index_path():
    return sfx_dir() / "index.json"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_audio(path):
    return path.suffix.lower() in AUDIO_EXTS and not path.name.startswith(".")


def _tokens(text):
    """Chinese + English tokens: whole CJK runs, their single chars, ascii words."""
    lowered = str(text or "").lower()
    tokens = []
    for chunk in NON_TOKEN.split(lowered):
        if not chunk:
            continue
        if CJK.search(chunk):
            tokens.append(chunk)
            tokens.extend(char for char in chunk if CJK.match(char))
        else:
            tokens.extend(ASCII_TOKEN.findall(chunk) or [chunk])
    return [token for token in dict.fromkeys(tokens) if token]


def derive_tags(name, extra=()):
    """Tags from a display name, using the built-in 中文→English synonym map."""
    tags = []
    for token in _tokens(name) + [str(item).lower() for item in extra]:
        if not token:
            continue
        if len(token) > 1:  # single CJK chars stay searchable via the name itself
            tags.append(token)
        for key, mapped in SYNONYMS.items():
            if key in token or token in key:
                tags.extend(mapped)
    return sorted(dict.fromkeys(tags))


def clean_name(path, resource_id=""):
    """Human-ish name; hash/md5 file names fall back to the 剪映 resource id."""
    stem = path.stem
    cleaned = re.sub(r"[_\-\.]+", " ", stem).strip()
    if cleaned and not HASH_NAME.match(cleaned) and not cleaned.isdigit():
        return cleaned
    if resource_id:
        return f"剪映音效 {resource_id}"
    return f"剪映音效 {stem[:8]}"


def _cache_resource_ids():
    """``{filename: resourceId}`` from 剪映's per-dir ``downLoadcfg`` (read-only)."""
    mapping = {}
    for name in CACHE_DIRS:
        config = CACHE_ROOT / name / "downLoadcfg"
        if not config.is_file():
            continue
        try:
            payload = json.loads(config.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in payload.get("list") or []:
            path = str(item.get("path") or "")
            rid = str(item.get("resourceId") or "")
            if path and rid:
                mapping[path] = rid
    return mapping


def _scan(sources=()):
    """``[(path, source, resource_id)]`` for the user dir, cache dirs and extras."""
    found = []
    user_root = sfx_dir()
    if user_root.is_dir():
        for path in sorted(user_root.rglob("*")):
            if path.is_file() and _is_audio(path) and "index.json" not in path.name:
                rel = path.relative_to(user_root)
                if rel.parts and rel.parts[0].startswith("."):
                    continue
                found.append((path, "user", ""))
    resource_ids = _cache_resource_ids()
    for name in CACHE_DIRS:
        directory = CACHE_ROOT / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and _is_audio(path):
                found.append((path, "cache", resource_ids.get(path.name, "")))
    for value in sources:
        extra = Path(value).expanduser()
        if not extra.is_dir():
            raise JyError("NOT_FOUND", f"--source 不是目录：{value}")
        for path in sorted(extra.rglob("*")):
            if path.is_file() and _is_audio(path):
                found.append((path, "extra", ""))
    unique = {}
    for item in found:
        unique.setdefault(str(item[0].resolve()), item)
    return list(unique.values())


def _probe(tool, path):
    try:
        probe = ffmpeg.probe_media(tool, path)
    except JyError:
        return None
    if not probe.get("audio_streams"):
        return None
    return probe


def _entry(tool, path, source, resource_id, *, previous=None):
    probe = _probe(tool, path)
    if probe is None:
        return None
    audio = probe["audio_streams"][0]
    stat = path.stat()
    if previous and previous.get("name") and previous.get("labelled"):
        name = str(previous["name"])
    elif previous and previous.get("name"):
        name = str(previous["name"])
    else:
        name = clean_name(path, resource_id)
    entry = {
        "id": _sha256(path)[:12],
        "path": str(path),
        "name": name,
        "source": source,
        "resource_id": resource_id or (previous or {}).get("resource_id", ""),
        "duration_s": probe.get("duration_s") or 0.0,
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
        "bitrate": probe.get("bitrate"),
        "bytes": stat.st_size,
        "mtime": int(stat.st_mtime),
    }
    labelled = bool(previous and previous.get("labelled"))
    entry["labelled"] = labelled
    user_tags = list((previous or {}).get("user_tags") or [])
    entry["user_tags"] = user_tags
    if labelled or source != "cache":
        entry["tags"] = derive_tags(name, user_tags)
    else:
        # hash-named cache file without a label: no meaningful tags, but the
        # resource_id stays searchable and `jy sfx label` can name it later
        entry["tags"] = []
    return entry


# --------------------------------------------------------------------------- #
# verbs


def sfx_index(ctx, args):
    """Build/refresh the index (user dir + 剪映 cache + --source extras)."""
    tool = ctx.require()
    started = time.monotonic()
    sources = list(args.source or [])
    root = sfx_dir()
    root.mkdir(parents=True, exist_ok=True)
    previous = {}
    existing = index_path()
    if existing.is_file():
        try:
            payload = json.loads(existing.read_text("utf-8"))
            previous = {entry.get("path"): entry for entry in payload.get("entries") or []}
        except (OSError, json.JSONDecodeError):
            previous = {}

    entries, skipped = [], []
    seen_ids = {}
    for path, source, resource_id in _scan(sources):
        old = previous.get(str(path.resolve()))
        fresh = old and old.get("mtime") == int(path.stat().st_mtime) and old.get("bytes") == path.stat().st_size
        entry = old if fresh else _entry(tool, path, source, resource_id, previous=old)
        if entry is None:
            skipped.append(str(path))
            continue
        entry["source"] = source
        if resource_id:
            entry["resource_id"] = resource_id
        if entry["id"] in seen_ids:
            # same bytes twice (cache + a copy in the user dir): keep the user copy
            keeper = seen_ids[entry["id"]]
            if entry["source"] == "user" and keeper["source"] != "user":
                entries[entries.index(keeper)] = entry
                seen_ids[entry["id"]] = entry
            continue
        seen_ids[entry["id"]] = entry
        entries.append(entry)

    entries.sort(key=lambda item: (item.get("source") != "user", item.get("name") or ""))
    payload = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sources": {
            "user": str(root),
            "cache": [str(CACHE_ROOT / name) for name in CACHE_DIRS if (CACHE_ROOT / name).is_dir()],
            "extra": [str(Path(value).expanduser()) for value in sources],
        },
        "counts": {
            "total": len(entries),
            "user": sum(1 for item in entries if item["source"] == "user"),
            "cache": sum(1 for item in entries if item["source"] == "cache"),
            "extra": sum(1 for item in entries if item["source"] == "extra"),
            "labelled": sum(1 for item in entries if item.get("labelled")),
        },
        "entries": entries,
    }
    temporary = existing.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    os.replace(temporary, existing)
    if skipped:
        ctx.warnings.append(f"{len(skipped)} 个文件读不出音频流，已跳过：{skipped[:3]}")
    if not entries:
        ctx.warnings.append(
            "索引是空的：把音效文件放进 " + str(root) + "，或在剪映音效面板里点下载后再跑一次"
            "（下载的文件会落在 Cache/music/ 里）。"
        )
    return "executed", {
        "index_path": str(existing),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "counts": payload["counts"],
        "sources": payload["sources"],
        "skipped": skipped,
        "entries": entries,
    }


def load_index():
    path = index_path()
    if not path.is_file():
        raise JyError(
            "NOT_FOUND",
            f"还没有音效索引：{path}。先跑 `jy sfx index`（会收割剪映已下载的音效）。",
            details={"index_path": str(path)},
        )
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError("INTERNAL", f"索引不可读：{path}（{exc}）") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise JyError("INTERNAL", f"索引结构不对：{path}")
    return payload


def _save_index(payload):
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    os.replace(temporary, path)


def score_entry(entry, query):
    """0..1 relevance for one entry; substring + token overlap, 中文 and English."""
    q = str(query or "").strip().lower()
    if not q:
        return 0.0
    name = str(entry.get("name") or "").lower()
    tags = [str(tag).lower() for tag in entry.get("tags") or []]
    resource_id = str(entry.get("resource_id") or "")
    score = 0.0
    if name == q:
        score = 1.0
    elif q and q in name:
        score = 0.8
    elif q and name and name in q:
        score = 0.6
    for tag in tags:
        if tag == q:
            score = max(score, 0.75)
        elif q in tag or (len(q) > 1 and tag in q):
            score = max(score, 0.55)
    if resource_id and q in resource_id:
        score = max(score, 0.4)
    # single CJK characters are too weak for token overlap (音/示 match everything);
    # they still work through the substring/name checks above
    query_tokens = {token for token in _tokens(q) if len(token) > 1}
    entry_tokens = {token for token in (set(_tokens(name)) | set(tags)) if len(token) > 1}
    if query_tokens and entry_tokens:
        overlap = len(query_tokens & entry_tokens) / len(query_tokens)
        if overlap:
            score = max(score, min(0.7, 0.25 + 0.45 * overlap))
    return round(score, 3)


def sfx_search(ctx, args):
    payload = load_index()
    limit = max(1, int(args.limit or 10))
    max_duration = args.max_duration if args.max_duration is not None else None
    min_duration = args.min_duration if args.min_duration is not None else None
    rows = []
    for entry in payload["entries"]:
        if args.source and entry.get("source") != args.source:
            continue
        duration = float(entry.get("duration_s") or 0.0)
        if max_duration is not None and duration > float(max_duration):
            continue
        if min_duration is not None and duration < float(min_duration):
            continue
        score = score_entry(entry, args.query)
        if score <= 0:
            continue
        rows.append({**entry, "score": score})
    rows.sort(key=lambda item: (-item["score"], item.get("duration_s") or 0.0, item.get("name") or ""))
    trimmed = rows[:limit]
    if not rows:
        ctx.warnings.append(
            "没有匹配的音效。可以：换个说法（中文/英文）、`jy sfx list` 看全部、"
            "或用 `jy sfx label <id> --name 叮 --tags ding,hint` 给已下载的音效命名。"
        )
    elif len(rows) > 1:
        ctx.warnings.append(f"{len(rows)} 个候选，挑一个（`jy sfx add <id|name>` 精确到 id 最稳）。")
    return "readonly", {
        "query": args.query,
        "matches": len(rows),
        "returned": len(trimmed),
        "results": trimmed,
    }


def _resolve_entry(selector):
    """One entry for ``selector`` (id / name / query) — ambiguity is an error."""
    payload = load_index()
    key = str(selector or "").strip()
    entries = payload["entries"]
    by_id = [entry for entry in entries if entry["id"] == key]
    if by_id:
        return by_id[0], "id"
    exact = [entry for entry in entries if str(entry.get("name") or "").lower() == key.lower()]
    if len(exact) == 1:
        return exact[0], "name"
    if len(exact) > 1:
        candidates = exact
    else:
        scored = [(score_entry(entry, key), entry) for entry in entries]
        candidates = [entry for score, entry in scored if score >= 0.55]
        candidates.sort(key=lambda item: -score_entry(item, key))
    if not candidates:
        raise JyError(
            "NOT_FOUND",
            f"索引里没有匹配 {key!r} 的音效。用 `jy sfx search {key}` 看看，或先 `jy sfx index`。",
            details={"selector": key},
        )
    if len(candidates) > 1:
        raise JyError(
            "VALIDATION",
            f"{key!r} 匹配到 {len(candidates)} 个音效，不替你挑。用 id 精确指定，"
            "或改成更具体的名字："
            + "；".join(f"{item['id']} ← {item.get('name')}" for item in candidates[:6]),
            details={
                "selector": key,
                "candidates": [
                    {
                        "id": item["id"],
                        "name": item.get("name"),
                        "duration_s": item.get("duration_s"),
                        "bytes": item.get("bytes"),
                        "source": item.get("source"),
                        "tags": item.get("tags"),
                    }
                    for item in candidates[:20]
                ],
            },
        )
    return candidates[0], "query"


def _window_rms(tool, path, start, duration):
    """RMS level (dB) of one audio window, for a cheap 'is it audible' check."""
    argv = [
        tool.ffmpeg, "-hide_banner", "-nostats", "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{max(0.05, duration):.3f}", "-i", path, "-vn",
        "-af", "astats=metadata=1:reset=0", "-f", "null", "-",
    ]
    done = ffmpeg._run(argv)
    text = (done.stderr or "") + (done.stdout or "")
    match = re.search(r"RMS level dB:\s*(-?\d+(?:\.\d+)?|-inf)", text)
    if not match:
        return None
    value = match.group(1)
    return None if value == "-inf" else round(float(value), 2)


def sfx_add(ctx, args):
    """Mix one indexed SFX into an existing video at ``--at`` seconds."""
    tool = ctx.require()
    entry, matched_by = _resolve_entry(args.selector)
    source = Path(entry["path"])
    if not source.is_file():
        raise JyError(
            "NOT_FOUND",
            f"索引里的文件已经不在了：{source}（重新 `jy sfx index`，或 `jy sfx forget {entry['id']}`）",
            details={"id": entry["id"], "path": str(source)},
        )
    video = common._resolve_input(args.input)
    output = common._checked_output(args.output, args.force)
    if args.at is None or float(args.at) < 0:
        raise JyError("VALIDATION", "--at 必须是 >= 0 的秒数（时间线上的绝对时间点）")
    at = float(args.at)
    probe = common._probe(tool, video)
    video_duration = probe.get("duration_s") or 0.0
    if video_duration and at >= video_duration:
        raise JyError(
            "VALIDATION",
            f"--at {at:g}s 超出视频时长 {video_duration:g}s",
            details={"video_duration_s": video_duration},
        )
    sfx_duration = float(entry.get("duration_s") or 0.0)
    gain = float(args.gain or 0.0)
    fade_in = max(0.0, float(args.fade_in or 0.0))
    fade_out = max(0.0, float(args.fade_out or 0.0))
    delay_ms = int(round(at * 1000))

    chain = [f"volume={gain:g}dB"]
    if fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={fade_in:g}")
    if fade_out > 0 and sfx_duration > 0:
        start = max(0.0, sfx_duration - fade_out)
        chain.append(f"afade=t=out:st={start:g}:d={fade_out:g}")
    channels = int(entry.get("channels") or 2)
    delays = "|".join([str(delay_ms)] * max(1, channels))
    chain.append(f"adelay={delays}")
    sfx_chain = ",".join(chain)

    has_original_audio = bool(probe.get("audio_streams"))
    if has_original_audio:
        graph = (
            f"[1:a]{sfx_chain}[sfx];"
            f"[0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    else:
        graph = f"[1:a]{sfx_chain}[aout]"
        ctx.warnings.append("输入没有音轨：输出只有这条音效（画面原样复制）。")

    argv = [tool.ffmpeg, "-hide_banner", "-nostdin", "-y" if args.force else "-n"]
    argv += ["-i", video, "-i", str(source)]
    argv += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]"]
    argv += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    argv += ["-movflags", "+faststart", "-shortest", str(output)]
    result = common._run(tool, argv, str(output), getattr(args, "verbose", False))
    probe_out = common._probe(tool, output)
    audio_streams = probe_out.get("audio_streams") or []
    if not audio_streams:
        raise JyError(
            "INTERNAL", f"混音后没有音轨：{output}", details={"output": str(output)}
        )
    window = min(sfx_duration or 0.5, max(0.1, (result.get("duration_s") or video_duration) - at))
    out_rms = _window_rms(tool, str(output), at, window)
    in_rms = _window_rms(tool, video, at, window) if has_original_audio else None
    if out_rms is not None and in_rms is not None and out_rms <= in_rms + 0.2:
        ctx.warnings.append(
            f"音效窗口的响度没有明显高于原声（{in_rms} dB → {out_rms} dB）："
            "可能被原声盖住，考虑 --gain +6 或换更响的音效。"
        )
    return "executed", {
        "output_path": str(output),
        "video": {
            "path": str(video),
            "duration_s": result.get("duration_s"),
            "audio_streams": len(audio_streams),
        },
        "sfx": {
            "id": entry["id"],
            "name": entry.get("name"),
            "path": str(source),
            "duration_s": sfx_duration,
            "bytes": entry.get("bytes"),
            "matched_by": matched_by,
        },
        "placement": {
            "at_s": round(at, 3),
            "gain_db": gain,
            "fade_in_s": fade_in,
            "fade_out_s": fade_out,
            "delay_ms": delay_ms,
        },
        "verification": {
            "audio_streams": len(audio_streams),
            "duration_s": result.get("duration_s"),
            "window_rms_db": out_rms,
            "original_window_rms_db": in_rms,
            "mix": "amix(duration=first,normalize=0)" if has_original_audio else "sfx-only",
        },
    }


def sfx_list(ctx, args):
    payload = load_index()
    rows = [entry for entry in payload["entries"] if not args.source or entry.get("source") == args.source]
    limit = max(1, int(args.limit or 200))
    return "readonly", {
        "index_path": str(index_path()),
        "counts": payload.get("counts") or {},
        "sources": payload.get("sources") or {},
        "returned": min(len(rows), limit),
        "entries": rows[:limit],
    }


def sfx_forget(ctx, args):
    """Drop an entry from the index (bookkeeping only — the file is never touched)."""
    payload = load_index()
    key = str(args.selector).strip()
    keep = [
        entry
        for entry in payload["entries"]
        if entry["id"] != key and str(entry.get("name") or "").lower() != key.lower()
    ]
    if len(keep) == len(payload["entries"]):
        raise JyError(
            "NOT_FOUND",
            f"索引里没有 {key!r}。用 `jy sfx list` 看 id。",
            details={"selector": key},
        )
    dropped = [entry for entry in payload["entries"] if entry not in keep]
    payload["entries"] = keep
    payload["counts"]["total"] = len(keep)
    for source in ("user", "cache", "extra"):
        payload["counts"][source] = sum(1 for item in keep if item["source"] == source)
    payload["counts"]["labelled"] = sum(1 for item in keep if item.get("labelled"))
    _save_index(payload)
    return "executed", {
        "forgot": [
            {"id": entry["id"], "name": entry.get("name"), "path": entry["path"]}
            for entry in dropped
        ],
        "remaining": len(keep),
        "note": "只从索引里移除，磁盘上的文件没有动。",
    }


def sfx_label(ctx, args):
    """Give an index entry a searchable name/tags (cache files are hash-named)."""
    payload = load_index()
    resolved, _ = _resolve_entry(args.selector)
    # _resolve_entry re-loads the index: edit the copy that lives in this payload,
    # otherwise the saved file would silently keep the old name/tags
    entry = next(
        (item for item in payload["entries"] if item.get("id") == resolved.get("id")),
        None,
    )
    if entry is None:
        raise JyError("INTERNAL", "索引自相矛盾：重启 `jy sfx index` 后重试")
    name = str(args.name).strip()
    if not name:
        raise JyError("VALIDATION", "--name 不能为空")
    entry["name"] = name
    entry["labelled"] = True
    user_tags = [tag.strip().lower() for tag in (args.tags or "").split(",") if tag.strip()]
    entry["user_tags"] = sorted(dict.fromkeys(user_tags))
    entry["tags"] = derive_tags(name, entry["user_tags"])
    payload["counts"]["labelled"] = sum(1 for item in payload["entries"] if item.get("labelled"))
    _save_index(payload)
    return "executed", {
        "entry": entry,
        "note": "只改索引，不动文件；之后 `jy sfx search` 就能按这个名字/标签找到它。",
    }


def doctor_block():
    """Status for ``jy doctor``: how many effects jy can actually reach right now."""
    path = index_path()
    info = {
        "index_path": str(path),
        "index_present": path.is_file(),
        "indexed": 0,
        "sources": {},
        "cache_audio_files": len(_scan_cached_only()),
        "labelled": 0,
        "index_mtime": None,
    }
    if path.is_file():
        info["index_mtime"] = int(path.stat().st_mtime)
        try:
            payload = json.loads(path.read_text("utf-8"))
            entries = payload.get("entries") or []
            info["indexed"] = len(entries)
            info["labelled"] = sum(1 for entry in entries if entry.get("labelled"))
            info["sources"] = payload.get("sources") or {}
        except (OSError, json.JSONDecodeError):
            info["error"] = "索引不是合法 JSON；重新跑 `jy sfx index`"
    return info


def _scan_cached_only():
    return [
        path
        for name in CACHE_DIRS
        for path in (CACHE_ROOT / name).rglob("*")
        if path.is_file() and _is_audio(path)
    ] if CACHE_ROOT.is_dir() else []


def register(r):
    import argparse  # noqa: F401 - subparser_class handles it

    group = r.parser_for(
        "sfx",
        help="local sound effects: index/search 剪映's downloaded SFX, mix one into a video",
        description="本地音效库：索引你自己 ~/Movies/jy-sfx 里的音效，并收割剪映已下载的音效"
        "（User Data/Cache/music|effect|artistEffect|AITextTemplate 里的普通 MP3/WAV）。"
        "纯本地、不联网、不写剪映缓存；索引存在 ~/Movies/jy-sfx/index.json。",
        group=True,
    )
    sub = group.add_subparsers(
        dest="sfx_action", required=True, parser_class=r.parser_class, metavar="action"
    )

    index = sub.add_parser(
        "index", parents=[r.verbose_parent],
        help="build/refresh the index (user dir + 剪映 cache)",
        description="扫描 $JY_SFX_DIR（默认 ~/Movies/jy-sfx，递归）和剪映缓存目录，"
        "用 ffprobe 记录时长/采样率/声道/码率，按名字生成中英文标签，原子写入 index.json。",
    )
    index.add_argument("--source", action="append", metavar="DIR",
                       help="额外的音效目录（可重复）")
    r.bind(index, "sfx index", sfx_index)

    search = sub.add_parser(
        "search", parents=[r.verbose_parent],
        help="rank local sound effects by name/tags (中文 or English)",
        description="按名字和标签检索（子串 + token 重叠 + 同义词），可按来源/时长过滤；"
        "返回 id、时长、大小，方便直接接 `jy sfx add`。",
    )
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--limit", type=int, default=10, metavar="N")
    search.add_argument("--max-duration", type=float, metavar="S")
    search.add_argument("--min-duration", type=float, metavar="S")
    search.add_argument("--source", choices=("user", "cache", "extra"))
    r.bind(search, "sfx search", sfx_search)

    add = sub.add_parser(
        "add", parents=[r.verbose_parent],
        help="mix one sound effect into a video at --at seconds (keeps original audio)",
        description="把一条音效混进已有视频的指定时间点：ffmpeg 用 volume/afade/adelay + "
        "amix(normalize=0) 保留原声，ffprobe 回验输出，并比较音效窗口的 RMS 判断有没有被盖住。"
        "匹配到多个候选时直接报错并列出候选，不会替你挑。",
    )
    add.add_argument("selector", metavar="id|name")
    add.add_argument("--at", type=float, required=True, metavar="S",
                     help="时间线上的绝对时间点（秒）")
    add.add_argument("-i", "--input", required=True, metavar="VIDEO", help="要混音的视频")
    add.add_argument("-o", "--output", required=True, metavar="OUT")
    add.add_argument("--gain", type=float, default=0.0, metavar="DB")
    add.add_argument("--fade-in", type=float, default=0.0, metavar="S")
    add.add_argument("--fade-out", type=float, default=0.0, metavar="S")
    add.add_argument("--force", action="store_true")
    r.bind(add, "sfx add", sfx_add)

    listing = sub.add_parser(
        "list", parents=[r.verbose_parent], help="list indexed sound effects"
    )
    listing.add_argument("--source", choices=("user", "cache", "extra"))
    listing.add_argument("--limit", type=int, default=200, metavar="N")
    r.bind(listing, "sfx list", sfx_list)

    forget = sub.add_parser(
        "forget", parents=[r.verbose_parent],
        help="drop an entry from the index (never deletes the file)",
    )
    forget.add_argument("selector", metavar="id|name")
    r.bind(forget, "sfx forget", sfx_forget)

    label = sub.add_parser(
        "label", parents=[r.verbose_parent],
        help="name/tag an indexed entry so search can find it (cache files are hash-named)",
    )
    label.add_argument("selector", metavar="id|resource_id")
    label.add_argument("--name", required=True, metavar="NAME")
    label.add_argument("--tags", metavar="a,b")
    r.bind(label, "sfx label", sfx_label)
