"""剪映 draft-list registry: read/merge/write ``root_meta_info.json``.

剪映 has no import command; the home draft list is rendered from the plaintext
index ``<draft-root>/root_meta_info.json`` (``all_draft_store`` + a monotonic
``draft_ids`` counter).  This module owns every write to that file, plus the
"bring an external draft folder into the draft root" step that has to happen
before a draft can be listed.

Safety rules, enforced here:

* dry-run by default — nothing changes without ``apply=True``;
* the index is re-read immediately before writing and any concurrent change
  (剪映 writes it itself) aborts the write instead of clobbering it;
* two timestamped backups are written and verified first, then an atomic
  ``os.replace`` — the app's own file style is preserved (compact JSON);
* only ``all_draft_store`` / ``draft_ids`` are touched; every other top-level
  key and every foreign entry is passed through byte-for-byte in value terms;
* paths outside the draft root are only ever *copied in*, never adopted.

Draft ids: ``draft_ids`` is not ``len(all_draft_store)``.  On 2026-10-24 the
live index already carried ``draft_ids=6`` with 5 entries (one draft had been
deleted without decrementing it); on 2026-11-03 it was 7 with 6 entries.  It is
therefore treated as a monotonic counter and only bumped when a *new* entry is
inserted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import draft as draft_mod
from .envelope import JyError

INDEX_NAME = "root_meta_info.json"
BACKUP_DIR = ".jy-backups"
ROOT_ENV = "JY_DRAFT_ROOT"
MARKER = draft_mod.MARKER
MARKER_SCHEMA = draft_mod.MARKER_SCHEMA

DEFAULT_ROOTS = (
    Path.home() / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
    Path.home() / "Movies" / "JianyingPro" / "User Data" / "Projects",
)

# Neutral copy of one live entry (2026-11-03, 剪映 11.6.13358): same 37 keys, so a
# registration on a machine whose store is still empty does not invent a schema.
ENTRY_TEMPLATE = {
    "cloud_draft_cover": False,
    "cloud_draft_sync": False,
    "draft_cloud_last_action_download": False,
    "draft_cloud_purchase_info": "",
    "draft_cloud_template_id": "",
    "draft_cloud_tutorial_info": "",
    "draft_cloud_videocut_purchase_info": "",
    "draft_cover": "",
    "draft_fold_path": "",
    "draft_id": "",
    "draft_is_ai_shorts": False,
    "draft_is_cloud_temp_draft": False,
    "draft_is_infinite_canvas_draft": False,
    "draft_is_invisible": False,
    "draft_is_pippit_draft": False,
    "draft_is_web_article_video": False,
    "draft_json_file": "",
    "draft_name": "",
    "draft_new_version": "",
    "draft_root_path": "",
    "draft_timeline_materials_size": 0,
    "draft_type": "",
    "draft_web_article_video_enter_from": "",
    "pippit_avatar_url": "",
    "pippit_extra_info": "",
    "pippit_id": "",
    "pippit_user_name": "",
    "streaming_edit_draft_ready": True,
    "tm_draft_cloud_completed": "",
    "tm_draft_cloud_entry_id": -1,
    "tm_draft_cloud_modified": 0,
    "tm_draft_cloud_parent_entry_id": -1,
    "tm_draft_cloud_space_id": -1,
    "tm_draft_cloud_user_id": -1,
    "tm_draft_create": 0,
    "tm_draft_modified": 0,
    "tm_draft_removed": 0,
    "tm_duration": 0,
}


# --------------------------------------------------------------------------- #
# draft root / index discovery


def _is_index(path):
    return path.is_file() and path.name == INDEX_NAME


def default_draft_root(explicit=None):
    """Resolve 剪映's draft root (``--draft-root`` → ``$JY_DRAFT_ROOT`` → install default)."""
    if explicit:
        root = Path(explicit).expanduser()
    elif os.environ.get(ROOT_ENV):
        root = Path(os.environ[ROOT_ENV]).expanduser()
    else:
        root = next((path for path in DEFAULT_ROOTS if (path / INDEX_NAME).is_file()),
                    DEFAULT_ROOTS[0])
    root = root.resolve()
    if not (root / INDEX_NAME).is_file():
        raise JyError(
            "NOT_FOUND",
            f"找不到剪映草稿索引：{root / INDEX_NAME}。用 --draft-root 指定草稿根目录"
            f"（含 {INDEX_NAME}），或用 ${ROOT_ENV}。",
            details={"root": str(root), "candidates": [str(p) for p in DEFAULT_ROOTS]},
        )
    return root


def index_for(draft_dir, *, root_meta=None, draft_root=None):
    """Return ``(index_path, draft_root)`` governing ``draft_dir``.

    Preference: an explicit index file, then an explicit root, then the index
    sitting next to the draft folder, then the installed default.
    """
    if root_meta:
        path = Path(root_meta).expanduser().resolve()
        if not path.is_file():
            raise JyError("NOT_FOUND", f"索引文件不存在：{path}")
        return path, path.parent
    if draft_root:
        root = default_draft_root(draft_root)
        return root / INDEX_NAME, root
    folder = Path(draft_dir).expanduser().resolve()
    for candidate in (folder.parent / INDEX_NAME, folder / INDEX_NAME):
        if candidate.is_file():
            return candidate, candidate.parent
    root = default_draft_root()
    return root / INDEX_NAME, root


def load_index(meta_path):
    """Read the index and keep the raw bytes so a concurrent edit can be detected."""
    meta_path = Path(meta_path)
    try:
        raw = meta_path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JyError("UNSUPPORTED", f"草稿索引不是合法 JSON：{meta_path}",
                      details={"error": str(exc)}) from exc
    if not isinstance(document, dict) or not isinstance(document.get("all_draft_store"), list):
        raise JyError(
            "UNSUPPORTED",
            f"索引缺少 all_draft_store 数组，不是剪映草稿索引：{meta_path}",
            details={"keys": sorted(document)[:10] if isinstance(document, dict) else None},
        )
    for position, entry in enumerate(document["all_draft_store"]):
        if not isinstance(entry, dict):
            raise JyError("UNSUPPORTED",
                          f"all_draft_store[{position}] 不是对象，拒绝改写：{meta_path}")
    return document, {
        "path": str(meta_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "mtime_ns": meta_path.stat().st_mtime_ns,
    }


# --------------------------------------------------------------------------- #
# entry construction


def _neutral(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, str):
        return ""
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return None


def entry_template(store):
    """剪映's exact key set: the shipped template, plus any keys a newer build added.

    Cloning the *values* of a live entry would copy its sentinels wrong (e.g.
    ``tm_draft_cloud_entry_id = -1`` neutralises to 0), so only new keys are taken.
    """
    template = dict(ENTRY_TEMPLATE)
    if store:
        for key, value in store[-1].items():
            template.setdefault(key, _neutral(value))
    return template


def _micros(document, key, fallback_ns):
    value = document.get(key)
    if isinstance(value, (int, float)) and value > 0:
        return int(value) * 1_000_000
    return int(fallback_ns // 1_000)


def _cover(draft_dir):
    for name in ("draft_cover.jpg", "draft_cover.jpeg", "draft_cover.png"):
        candidate = Path(draft_dir) / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return ""


def _first_clip(document):
    """(path, seconds) of the earliest video segment, for a list thumbnail."""
    materials = document.get("materials")
    if not isinstance(materials, dict):
        return None
    by_id = {}
    for material in materials.get("videos") or []:
        if isinstance(material, dict) and isinstance(material.get("path"), str):
            by_id[str(material.get("id") or "")] = material["path"]
    best = None
    for track in document.get("tracks") or []:
        if not isinstance(track, dict) or track.get("type") != "video":
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            path = by_id.get(str(segment.get("material_id") or ""))
            if not path:
                continue
            source = segment.get("source_timerange") or {}
            offset = (source.get("start") or 0) / 1_000_000.0
            if best is None or offset < best[1]:
                best = (path, offset)
    return best


def write_cover(folder, document, *, tool=None, verbose=False):
    """Best-effort ``draft_cover.jpg`` from the first video clip (list thumbnail).

    Never raises: a missing ffmpeg or an offline source only means the tile keeps
    剪映's placeholder.
    """
    folder = Path(folder)
    if _cover(folder):
        return None
    clip = _first_clip(document)
    if not clip:
        return None
    path, start = clip
    if not Path(path).is_file():
        return None
    try:
        from . import ffmpeg

        tool = tool or ffmpeg.discover()
        argv = [
            tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{start + 0.2:.3f}", "-i", path,
            "-frames:v", "1", "-q:v", "3", "-y", str(folder / "draft_cover.jpg"),
        ]
        ffmpeg.run_ffmpeg(tool, argv, verbose=verbose)
    except Exception:  # pragma: no cover - thumbnail is optional
        return None
    cover = folder / "draft_cover.jpg"
    return str(cover) if cover.is_file() and cover.stat().st_size > 0 else None


def build_entry(draft_dir, index, *, store=None, name=None, root=None):
    """Build the ``all_draft_store`` entry for a jy-authored draft folder."""
    folder = Path(draft_dir).expanduser().resolve()
    resolved = draft_mod.resolve_draft_info(folder)
    info_path = resolved["info_path"]
    if info_path is None or not info_path.is_file():
        raise JyError(
            "NOT_FOUND",
            f"不是剪映草稿目录（缺 draft_info.json / Timelines/<id>/draft_info.json）：{folder}",
        )
    try:
        document = json.loads(info_path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JyError(
            "UNSUPPORTED",
            f"{info_path} 不是明文 draft_info.json（剪映重存后会被加密）；"
            "jy 只登记自己写的明文草稿",
            details={"error": str(exc)},
        ) from exc
    if not isinstance(document, dict):
        raise JyError("UNSUPPORTED", f"{info_path} 不是 JSON 对象")

    draft_root = Path(root).expanduser().resolve() if root else folder.parent
    stat = folder.stat()
    entry = entry_template(store if store is not None else index.get("all_draft_store") or [])
    entry.update({
        "draft_fold_path": str(folder),
        "draft_json_file": str(info_path),
        "draft_name": str(name or document.get("name") or folder.name),
        "draft_id": str(document.get("id") or ""),
        "draft_root_path": str(draft_root),
        "draft_cover": _cover(folder),
        "draft_new_version": "",
        "draft_timeline_materials_size": draft_mod.materials_bytes(document, folder),
        "draft_is_invisible": False,
        "streaming_edit_draft_ready": True,
        "tm_draft_create": _micros(document, "create_time", stat.st_mtime_ns),
        "tm_draft_modified": _micros(document, "update_time", stat.st_mtime_ns),
        "tm_duration": int(document.get("duration") or 0),
    })
    if not entry["draft_id"]:
        # The list keys drafts by UUID; a draft without one would collide with
        # every other blank entry, so refuse instead of writing "". See
        # `draft build`, which always stamps one.
        raise JyError("UNSUPPORTED", f"{info_path} 缺少 id 字段，拒绝登记成空 UUID")
    return entry


def _same_draft(current, entry):
    return (
        str(current.get("draft_fold_path") or "") == entry["draft_fold_path"]
        or str(current.get("draft_id") or "") == entry["draft_id"]
    )


def _next_draft_ids(index, entries, entry):
    """Bump 剪映's watermark; never regress it to ``len(store)``.

    11.6.13358 stores an integer (``draft_ids=7`` with 6 entries); older builds stored a
    list of draft ids, which the sibling management verbs still understand. Both shapes
    are handled rather than guessed at.
    """
    current = index.get("draft_ids", 0)
    if isinstance(current, bool):
        raise JyError("UNSUPPORTED", "draft_ids 是布尔值，拒绝猜写")
    if isinstance(current, int):
        return max(current, len(entries)) + 1
    if isinstance(current, list):
        ids = [item for item in current if item != entry.get("draft_id")]
        ids.append(entry["draft_id"])
        return ids
    raise JyError(
        "UNSUPPORTED",
        f"draft_ids 既不是整数也不是数组（{type(current).__name__}），拒绝猜写：{current!r}",
    )


def merge_entry(index, entry):
    """Return ``(new_index, action, ids_before, ids_after)`` without touching disk."""
    entries = list(index.get("all_draft_store") or [])
    position = next((i for i, current in enumerate(entries) if _same_draft(current, entry)), None)
    new_index = dict(index)
    ids_before = index.get("draft_ids")
    if position is None:
        # 剪映 lists newest first, so a new entry goes to the front.
        entries.insert(0, entry)
        new_index["draft_ids"] = _next_draft_ids(index, index.get("all_draft_store") or [], entry)
        action = "insert"
    else:
        entries[position] = entry
        new_index["draft_ids"] = ids_before
        action = "update"
    new_index["all_draft_store"] = entries
    return new_index, action, ids_before, new_index["draft_ids"]


# --------------------------------------------------------------------------- #
# write path


def _backup(meta_path, stamp):
    backups = []
    backup_dir = Path(meta_path).parent / BACKUP_DIR
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        canonical = backup_dir / f"{Path(meta_path).name}.{stamp}.json"
        shutil.copy2(meta_path, canonical)
        backups.append(canonical)
        # Second name on purpose: `jy draft history` lists `.jy-backups/`, while the
        # documented manual restore step looks for `<name>.bak-<stamp>` next to it.
        legacy = Path(meta_path).with_name(f"{Path(meta_path).name}.bak-{stamp}")
        shutil.copy2(meta_path, legacy)
        backups.append(legacy)
    except OSError as exc:
        raise JyError(
            "BLOCKED",
            f"备份索引失败，拒绝写入：{backups[-1] if backups else backup_dir}",
            details={"error": str(exc)},
        ) from exc
    for backup in backups:
        if not backup.is_file() or backup.stat().st_size == 0:
            raise JyError("BLOCKED", f"备份校验失败，拒绝写入：{backup}")
    return backups


def _dumps(document):
    """剪映's own index style: compact separators, no trailing newline, UTF-8."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def write_index(meta_path, document, *, entry=None, expected=None, attempts=2):
    """Atomically replace the index, aborting if somebody else changed it first.

    ``entry`` lets the retry path re-merge into a *fresh* read instead of writing
    a plan built from a stale one (剪映 writes this file itself while running).
    """
    meta_path = Path(meta_path)
    for attempt in range(attempts):
        current = hashlib.sha256(meta_path.read_bytes()).hexdigest()
        if expected is not None and current != expected["sha256"]:
            if attempt + 1 < attempts and entry is not None:
                fresh, expected = load_index(meta_path)
                document, _, _, _ = merge_entry(fresh, entry)
                continue
            raise JyError(
                "BLOCKED",
                f"索引在读取后被改动（剪映自己写的），拒绝覆盖：{meta_path}",
                details={"expected": expected["sha256"], "found": current},
            )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backups = _backup(meta_path, stamp)
        tmp = meta_path.with_name(f"{meta_path.name}.tmp-{stamp}-{os.getpid()}")
        payload = _dumps(document).encode("utf-8")
        try:
            with open(tmp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            shutil.copymode(meta_path, tmp)
            os.replace(tmp, meta_path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise JyError("BLOCKED", f"写索引失败（已保留备份）：{meta_path}",
                          details={"error": str(exc), "backups": [str(b) for b in backups]}) from exc
        return {"backups": [str(path) for path in backups],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload)}
    raise JyError("BLOCKED", f"索引被并发修改，{attempts} 次都没写成功：{meta_path}")


# --------------------------------------------------------------------------- #
# public verbs


def _running():
    try:
        from . import focus

        return bool(focus.running("com.lemon.lvpro"))
    except Exception:  # pragma: no cover - lsappinfo unavailable
        return False


def ensure_meta_info(folder):
    """Create — or re-derive — ``draft_meta_info.json`` for a jy-authored draft.

    剪映's draft-store loader drops any index entry whose folder lacks that file
    (``isDraftFoldValid``), so registering without it silently does nothing. For a
    jy draft the file is re-derived from ``draft_info.json`` every time: a folder
    that 剪映 already scanned can carry a *different* draft id, ``draft_is_invisible:
    true`` and ``tm_duration: 0`` (observed 2026-11-03 — the running app rescanned the
    folder and rewrote both the meta and the index), and leaving that in place would
    hand the app a draft it hides and reports with a zero duration.

    Non-jy draft folders are left alone. Returns the written path, or None.
    """
    folder = Path(folder)
    if not (folder / MARKER).is_file():
        if (folder / "draft_meta_info.json").is_file():
            return None
    try:
        document = json.loads((folder / "draft_info.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if (folder / "draft_meta_info.json").is_file():
            return None
        raise JyError("UNSUPPORTED", f"读不到明文 draft_info.json：{folder / 'draft_info.json'}",
                      details={"error": str(exc)}) from exc
    meta_file = folder / "draft_meta_info.json"
    meta_file.write_text(
        json.dumps(draft_mod.meta_info(document, folder), ensure_ascii=False),
        encoding="utf-8",
    )
    return str(meta_file)


def register(draft_dir, *, root_meta=None, draft_root=None, name=None, apply=False):
    """Add (or refresh) one draft in 剪映's draft list.

    ``apply=False`` reports exactly what would be written.  Writing re-reads the
    index, backs it up twice and atomically replaces it.
    """
    folder = Path(draft_dir).expanduser().resolve()
    if not folder.is_dir():
        raise JyError("NOT_FOUND", f"草稿目录不存在：{folder}")
    meta_path, resolved_root = index_for(folder, root_meta=root_meta, draft_root=draft_root)
    index, before = load_index(meta_path)
    entry = build_entry(folder, index, name=name, root=resolved_root)
    merged, action, ids_before, ids_after = merge_entry(index, entry)
    result = {
        "schema": "jy.draft_index.v1",
        "root_meta": str(meta_path),
        "draft_root": str(resolved_root),
        "draft": str(folder),
        "draft_inside_root": str(folder.parent) == str(resolved_root),
        "action": action,
        "draft_ids": {"before": ids_before, "after": ids_after},
        "entry": entry,
        "entries": {"before": len(index.get("all_draft_store") or []),
                    "after": len(merged.get("all_draft_store") or [])},
        "index_before": before,
        "jianying_running": _running(),
        "applied": False,
        "backup": None,
        "backups": [],
    }
    if not apply:
        result["note"] = "预演：未写入。加 --apply 才写索引（写入前自动备份）"
        return result
    if result["jianying_running"]:
        result["note"] = (
            "剪映正在运行：它可能用内存里的草稿列表覆盖索引。写入已执行，"
            "请退出并重开剪映再看草稿列表。"
        )
    result["meta_info_created"] = ensure_meta_info(folder)
    written = write_index(meta_path, merged, entry=entry, expected=before)
    result.update({"applied": True, "backup": written["backups"][0],
                   "backups": written["backups"], "index_after": written})
    return result


# --------------------------------------------------------------------------- #
# import (bring an external draft folder into the draft root)

_PATH_FIXUPS = {
    "draft_info.json": {"path": "folder", "name": "name"},
    "draft_content.json": {"path": "folder"},
    "draft_meta_info.json": {
        "draft_fold_path": "folder",
        "draft_root_path": "root",
        "draft_name": "name",
        "draft_cover": "cover",
    },
}


def _rewrite_paths(folder, *, root, name):
    """Point a copied draft folder at its new location; returns what changed."""
    folder = Path(folder).resolve()
    root = Path(root).resolve()
    values = {"folder": str(folder), "root": str(root), "name": str(name),
              "cover": "draft_cover.jpg"}
    changed = []
    for filename, fields in _PATH_FIXUPS.items():
        target = folder / filename
        if not target.is_file():
            continue
        try:
            document = json.loads(target.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        touched = []
        for field, source in fields.items():
            if field in document and document[field] != values[source]:
                document[field] = values[source]
                touched.append(field)
        if touched:
            target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            changed.append({"file": str(target), "fields": sorted(touched)})
    info = folder / "draft_info.json"
    marker = folder / MARKER
    if info.is_file() and marker.is_file():
        try:
            payload = json.loads(marker.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"schema": MARKER_SCHEMA}
        payload["draft_sha256"] = hashlib.sha256(info.read_bytes()).hexdigest()
        payload["imported_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        marker.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return changed


def _cover_for(folder):
    """Generate a thumbnail for an imported folder (best effort, never raises)."""
    try:
        document = json.loads((Path(folder) / "draft_info.json").read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return write_cover(folder, document) or ""


def _unique_folder(root, name):
    candidate = Path(root) / name
    suffix = 2
    while candidate.exists():
        candidate = Path(root) / f"{name}-{suffix}"
        suffix += 1
    return candidate


def _preview_entry(folder, destination, dest_root, index, *, name):
    """Entry as it *will* look after the copy, computed from the source document."""
    entry = build_entry(folder, index, name=name, root=dest_root)
    entry["draft_fold_path"] = str(destination)
    entry["draft_json_file"] = str(destination / "draft_info.json")
    cover = destination / "draft_cover.jpg"
    entry["draft_cover"] = str(cover) if cover.is_file() else ""
    return entry


def import_draft(source, *, draft_root=None, root_meta=None, name=None, move=False, apply=False):
    """Copy/move an external jy draft folder into 剪映's draft root and register it.

    Dry-run by default.  The copy never overwrites an existing folder; the source
    is left untouched unless ``move=True``.
    """
    folder = Path(source).expanduser().resolve()
    if not folder.is_dir():
        raise JyError("NOT_FOUND", f"草稿目录不存在：{folder}")
    resolved = draft_mod.resolve_draft_info(folder)
    if resolved["info_path"] is None:
        raise JyError("NOT_FOUND", f"不是剪映草稿目录：{folder}")
    try:
        json.loads(resolved["info_path"].read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JyError(
            "UNSUPPORTED",
            f"{resolved['info_path']} 不是明文 draft_info.json（剪映重存过）；"
            "jy 只导入自己写的明文草稿",
            details={"error": str(exc)},
        ) from exc
    dest_root = (Path(root_meta).expanduser().resolve().parent if root_meta
                 else default_draft_root(draft_root))
    meta_path = Path(root_meta).expanduser().resolve() if root_meta else dest_root / INDEX_NAME
    wanted = str(name or resolved["info_path"].parent.name or folder.name).strip()
    if not wanted or "/" in wanted or wanted in {".", ".."} or "\\" in wanted:
        raise JyError("VALIDATION", f"草稿名不合法：{wanted!r}")

    try:
        source_document = json.loads(resolved["info_path"].read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        source_document = {}
    source_id = str(source_document.get("id") or "")
    index, _before = load_index(meta_path)
    def already_imported(entry):
        """True when this entry's folder *is* the source draft (same folder id).

        The index ``draft_id`` is not trustworthy for this: a running 剪映 rescans the
        folder and rewrites the entry with a freshly generated UUID (observed
        2026-11-03), while ``draft_info.json`` keeps the authored id.
        """
        path = Path(str(entry.get("draft_fold_path") or ""))
        if not source_id or not path.is_dir() or path.resolve() == folder:
            return False
        if str(entry.get("draft_id") or "") == source_id:
            return True
        try:
            document = json.loads((path / "draft_info.json").read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return str(document.get("id") or "") == source_id

    existing = next(
        (entry for entry in index.get("all_draft_store") or [] if already_imported(entry)),
        None,
    )
    if existing is not None:
        # Same draft already imported: update that folder instead of making a copy.
        destination = Path(existing["draft_fold_path"])
        plan = {
            "schema": "jy.draft_import.v1",
            "source": str(folder),
            "draft_root": str(dest_root),
            "draft": str(destination),
            "name": wanted,
            "renamed": False,
            "mode": "update",
            "applied": False,
            "note": "该草稿已在剪映草稿根目录里（同 draft_id）：不复制，只刷新路径和列表条目",
        }
        if apply:
            plan["path_fixups"] = _rewrite_paths(destination, root=dest_root, name=wanted)
            plan["meta_info"] = ensure_meta_info(destination)
            plan["cover"] = _cover_for(destination)
        registration = register(destination, root_meta=meta_path, name=wanted, apply=apply)
        plan["registration"] = registration
        plan["applied"] = registration["applied"]
        return plan

    if folder.parent == dest_root:
        plan = {
            "schema": "jy.draft_import.v1",
            "source": str(folder),
            "draft_root": str(dest_root),
            "draft": str(folder),
            "name": wanted,
            "renamed": False,
            "mode": "in-place",
            "applied": False,
            "note": "草稿已在草稿根目录内：不复制，只登记（用 --name 可改列表里显示的名字）",
        }
        plan["cover"] = _cover_for(folder) if apply else ""
        registration = register(folder, root_meta=meta_path, name=wanted, apply=apply)
        plan["registration"] = registration
        plan["applied"] = registration["applied"]
        return plan

    destination = _unique_folder(dest_root, wanted)
    plan = {
        "schema": "jy.draft_import.v1",
        "source": str(folder),
        "draft_root": str(dest_root),
        "draft": str(destination),
        "name": destination.name,
        "renamed": destination.name != wanted,
        "mode": "move" if move else "copy",
        "applied": False,
    }
    if not apply:
        entry = _preview_entry(folder, destination, dest_root, index, name=destination.name)
        _merged, action, ids_before, ids_after = merge_entry(index, entry)
        plan["registration"] = {
            "schema": "jy.draft_index.v1",
            "root_meta": str(meta_path),
            "draft_root": str(dest_root),
            "draft": str(destination),
            "action": action,
            "draft_ids": {"before": ids_before, "after": ids_after},
            "entry": entry,
            "applied": False,
            "note": "预演：未写入",
        }
        plan["path_fixups"] = [
            {"file": str(destination / filename), "fields": sorted(fields)}
            for filename, fields in _PATH_FIXUPS.items()
            if (folder / filename).is_file()
        ]
        plan["note"] = ("预演：未写入。加 --apply 才会"
                        + ("移动" if move else "复制")
                        + "进草稿根目录、改写草稿内路径并登记进剪映草稿列表")
        return plan
    try:
        if move:
            shutil.move(str(folder), str(destination))
        else:
            shutil.copytree(folder, destination)
    except OSError as exc:
        raise JyError("BLOCKED", f"复制草稿失败：{folder} → {destination}",
                      details={"error": str(exc)}) from exc
    plan["path_fixups"] = _rewrite_paths(destination, root=dest_root, name=destination.name)
    plan["meta_info"] = ensure_meta_info(destination)
    plan["cover"] = _cover_for(destination)
    registration = register(destination, root_meta=meta_path, name=destination.name, apply=True)
    plan["registration"] = registration
    plan["applied"] = bool(registration.get("applied"))
    return plan
