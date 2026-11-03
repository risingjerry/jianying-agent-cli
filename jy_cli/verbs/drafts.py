"""Draft verbs: ``draft build`` (strict spec + staging) and draft management.

Management (``list/rename/duplicate/delete/move/history``) works on 剪映's draft
root (``root_meta_info.json``). Safety rules, all enforced here:

* dry-run by default — nothing changes without ``--apply``;
* the index is backed up before every mutation;
* deletes move the folder into ``<root>/.jy-trash`` instead of unlinking;
* any path that resolves outside the draft root is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .. import draft as drafts
from .. import draft_index, jianying_commands
from ..envelope import JyError
from . import _draft_spec as draft_spec
from .. import jianying_cache

ROOT_ENV = "JY_DRAFT_ROOT"
INDEX_NAME = "root_meta_info.json"
TRASH_DIR = ".jy-trash"
BACKUP_DIR = ".jy-backups"


# ---------------------------------------------------------------------------
# draft root plumbing


def draft_root(explicit=None):
    if explicit:
        root = Path(explicit).expanduser()
    elif os.environ.get(ROOT_ENV):
        root = Path(os.environ[ROOT_ENV]).expanduser()
    else:
        candidates = [
            Path.home() / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
            Path.home() / "Movies" / "JianyingPro" / "User Data" / "Projects",
        ]
        root = next((path for path in candidates if (path / INDEX_NAME).is_file()), candidates[0])
    root = root.resolve()
    if not (root / INDEX_NAME).is_file():
        raise JyError(
            "NOT_FOUND",
            f"找不到草稿索引：{root / INDEX_NAME}。用 --draft-root 指定剪映草稿根目录"
            f"（含 {INDEX_NAME}），或用 ${ROOT_ENV}。",
            details={"root": str(root)},
        )
    return root


def _inside(root, path):
    try:
        Path(path).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _require_inside(root, path):
    resolved = Path(path).expanduser().resolve()
    if not _inside(root, resolved):
        raise JyError(
            "VALIDATION",
            f"拒绝操作草稿根目录之外的路径：{resolved}（root={root}）",
            details={"root": str(root), "path": str(resolved)},
        )
    return resolved


def _index_path(root):
    return root / INDEX_NAME


def _load_index(root):
    path = _index_path(root)
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError("INTERNAL", f"草稿索引不可读：{path}（{exc}）") from exc
    if not isinstance(document, dict) or not isinstance(document.get("all_draft_store"), list):
        raise JyError(
            "UNSUPPORTED", f"{path} 不是剪映草稿索引（缺 all_draft_store）", details={"path": str(path)}
        )
    return document


def _save_index(root, document, *, apply):
    """Back the index up, then write it (only when applying)."""
    path = _index_path(root)
    if not apply:
        return None
    backup_dir = root / BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{INDEX_NAME}.{stamp}.bak"
    shutil.copy2(path, backup)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), "utf-8")
    os.replace(temporary, path)
    return backup


def _entries(document):
    return document.get("all_draft_store") or []


def _entry_path(entry):
    return Path(entry.get("draft_fold_path") or "")


def _find_entry(document, key):
    wanted = str(key or "").strip()
    if not wanted:
        raise JyError("VALIDATION", "需要一个草稿名或 draft_id")
    matches = [
        entry
        for entry in _entries(document)
        if wanted.lower() in (
            str(entry.get("draft_name") or "").lower(),
            str(entry.get("draft_id") or "").lower(),
        )
    ]
    if not matches:
        names = [str(entry.get("draft_name") or "") for entry in _entries(document)]
        import difflib

        close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.5)
        raise JyError(
            "NOT_FOUND",
            f"没有这个草稿：{wanted!r}" + (f"；是不是想用：{'、'.join(close)}" if close else "")
            + "（用 `jy draft list` 看全部）",
            details={"requested": wanted, "suggestions": close},
        )
    if len(matches) > 1:
        raise JyError(
            "VALIDATION",
            f"草稿名 {wanted!r} 不唯一（{len(matches)} 个），请用 draft_id 指定",
            details={"matches": [str(entry.get("draft_id")) for entry in matches]},
        )
    return matches[0]


def _duration_s(folder):
    """Duration from draft_info.json (single or migrated multi-timeline layout)."""
    resolved = drafts.resolve_draft_info(folder)
    info = resolved["info_path"]
    if info is None or not info.is_file():
        return None
    try:
        document = json.loads(info.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None  # 剪映 encrypts drafts it re-saved; the index still knows
    duration = document.get("duration")
    return round(duration / 1_000_000.0, 3) if isinstance(duration, (int, float)) else None


def _index_duration_s(entry):
    """``tm_duration`` (µs) from root_meta_info.json — survives encryption."""
    value = entry.get("tm_duration")
    if isinstance(value, (int, float)) and value > 0:
        return round(value / 1_000_000.0, 3)
    return None


def _unique_name(root, name):
    candidate = root / name
    suffix = 2
    while candidate.exists():
        candidate = root / f"{name}-{suffix}"
        suffix += 1
    return candidate.name


def _trimmed(document, entry):
    folder = _entry_path(entry)
    duration = _duration_s(folder)
    duration_source = "draft_info.json" if duration is not None else None
    if duration is None:
        duration = _index_duration_s(entry)
        duration_source = "root_meta_info.tm_duration" if duration is not None else None
    return {
        "draft_id": entry.get("draft_id"),
        "name": entry.get("draft_name"),
        "folder": folder.name,
        "path": str(folder),
        "duration_s": duration,
        "duration_source": duration_source,
        "modified": entry.get("tm_draft_modified"),
        "materials_bytes": entry.get("draft_timeline_materials_size"),
        "invisible": bool(entry.get("draft_is_invisible")),
        "in_root": _inside(Path(_entry_path(entry)).parent, _entry_path(entry)) if _entry_path(entry) else False,
    }


# ---------------------------------------------------------------------------
# draft build (strict spec + staging + per-clip properties)


def draft_build(ctx, args):
    """Validate the spec loudly, stage sources into ~/Movies, then build the draft."""
    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        raise JyError("NOT_FOUND", f"spec 不存在：{args.spec}")
    try:
        raw_spec = json.loads(spec_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError("VALIDATION", f"spec 不是合法 JSON：{args.spec}（{exc}）") from exc

    # materials given by name (or without a cache path) -> resource_id + path from 剪映's cache
    resolved = jianying_cache.resolve_spec(raw_spec, fetch=bool(getattr(args, "fetch", False)))
    props = draft_spec.validate_spec(raw_spec, where="spec")
    effective_spec = spec_path
    staging = None
    # relative --staging-dir is relative to the caller's CWD, not to the temp spec copy
    staging_dir = Path(args.staging_dir).expanduser().resolve() if args.staging_dir else None
    with tempfile.TemporaryDirectory(prefix="jy-draft-spec-") as tmp_dir:
        if not getattr(args, "no_staging", False):
            staged_spec, staging = draft_spec.stage_clips(
                raw_spec,
                spec_path.parent,
                root=staging_dir,
                warnings=ctx.warnings,
            )
            effective_spec = Path(tmp_dir) / "spec.staged.json"
            effective_spec.write_text(json.dumps(staged_spec, ensure_ascii=False), "utf-8")
        elif resolved:
            for clip in raw_spec.get("clips") or []:  # keep relative clip paths valid
                if isinstance(clip, dict) and clip.get("path") and not Path(clip["path"]).expanduser().is_absolute():
                    clip["path"] = str((spec_path.parent / clip["path"]).resolve())
            effective_spec = Path(tmp_dir) / "spec.resolved.json"
            effective_spec.write_text(json.dumps(raw_spec, ensure_ascii=False), "utf-8")
        forwarded = type(args)(**vars(args))
        forwarded.spec = str(effective_spec)
        outcome, data = jianying_commands.draft_build(ctx, forwarded)

    patched = None
    draft_info = data.get("draft_info")
    if draft_info and any(
        key in prop for prop in props for key in ("transform", "opacity", "volume", "crop", "speed", "render_index")
    ):
        patched = draft_spec.apply_clip_properties(draft_info, props)
        if patched.get("skipped"):
            ctx.warnings.append(patched["reason"])
        else:
            ctx.warnings.append(
                f"已写入逐片段属性：{patched['applied']} 段"
                "（transform/opacity/volume/crop/speed/render_index）"
            )
    if staging is None:
        data["staging"] = {"enabled": False, "root": None, "clips": []}
    else:
        data["staging"] = {
            "enabled": True,
            "root": str(staging_dir or draft_spec.staging_root()),
            "clips": staging,
            "total_bytes": sum(item.get("bytes") or 0 for item in staging),
            "linked": sum(1 for item in staging if item.get("linked")),
            "copied": sum(1 for item in staging if item.get("copied")),
        }
    data["clip_properties"] = patched or {"applied": 0, "skipped": False}
    data["materials_resolved"] = resolved
    if data.get("draft_info"):
        folder = Path(data["draft_info"]).resolve().parent
        try:
            document = json.loads(Path(data["draft_info"]).read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            document = None
        cover = draft_index.write_cover(folder, document, tool=getattr(ctx, "tool", None)) if document else None
        if cover:
            data["cover"] = cover
    data["registration"] = _auto_register(ctx, data.get("draft_info"), args)
    return outcome, data


def _auto_register(ctx, draft_info, args):
    """Register a draft that was built straight into 剪映's draft root.

    A draft outside the root cannot be listed without copying it in (that is
    ``jy draft import``), so this is a no-op there — reported, not guessed.
    """
    if not draft_info:
        return {"registered": False, "reason": "没有 draft_info.json 路径"}
    if getattr(args, "no_register", False):
        return {"registered": False, "reason": "--no-register"}
    folder = Path(draft_info).resolve().parent
    index = folder.parent / INDEX_NAME
    if not index.is_file():
        return {"registered": False,
                "reason": f"草稿不在剪映草稿根目录下（{index} 不存在）：用 `jy draft import` 放进去",
                "draft_root_index": str(index)}
    try:
        outcome = draft_index.register(folder, root_meta=index, apply=True)
    except JyError as exc:
        ctx.warnings.append(
            f"草稿已生成，但没登记进剪映草稿列表（{exc.code}）：{exc.message}。"
            f"稍后可用 `jy draft register {folder} --apply` 重试。"
        )
        return {"registered": False, "error": {"code": exc.code, "message": exc.message}}
    if outcome.get("jianying_running") and not getattr(args, "no_register", False):
        ctx.warnings.append(
            "剪映正在运行：它会把新出现的草稿目录按自己的规则收纳（新 draft_id、draft_is_invisible=true），"
            "退出剪映后跑一次 `jy draft register <草稿> --apply` 可修正。"
        )
    ctx.warnings.append(
        f"已登记进剪映草稿列表（{outcome['action']}，draft_ids "
        f"{outcome['draft_ids']['before']}→{outcome['draft_ids']['after']}）：{folder}"
        + ("。剪映正在运行：请退出并重开剪映再看列表" if outcome.get("jianying_running") else "")
    )
    return outcome


def draft_register(ctx, args):
    """Add one draft folder to 剪映's home list (dry-run unless --apply)."""
    data = draft_index.register(
        args.draft,
        root_meta=args.root_meta,
        draft_root=args.draft_root,
        name=args.name,
        apply=bool(args.apply),
    )
    if data.get("jianying_running"):
        ctx.warnings.append(
            "剪映正在运行：它会在下次写索引时用内存里的草稿列表覆盖文件，登记可能看不到。"
            "退出并重开剪映后再看首页列表。"
        )
    if not data.get("draft_inside_root"):
        ctx.warnings.append(
            f"草稿目录不在草稿根目录内（{data['draft']}）：剪映可能只扫草稿根目录。"
            "要真正出现在首页列表，用 `jy draft import` 复制进草稿根目录。"
        )
    return ("executed" if data["applied"] else "preview"), data


def draft_import(ctx, args):
    """Copy an external draft folder into the draft root, then register it."""
    data = draft_index.import_draft(
        args.draft,
        draft_root=args.draft_root,
        root_meta=args.root_meta,
        name=args.name,
        move=bool(args.move),
        apply=bool(args.apply),
    )
    if data.get("registration", {}).get("jianying_running"):
        ctx.warnings.append(
            "剪映正在运行：登记可能被它内存里的列表覆盖；退出并重开剪映后再看首页列表。"
        )
    return ("executed" if data["applied"] else "preview"), data


# ---------------------------------------------------------------------------
# management verbs


def draft_list(ctx, args):
    root = draft_root(args.draft_root)
    document = _load_index(root)
    entries = [_trimmed(document, entry) for entry in _entries(document)]
    if args.folder:
        entries = [entry for entry in entries if entry["folder"] == args.folder]
    entries.sort(key=lambda item: item.get("modified") or 0, reverse=True)
    return "readonly", {
        "draft_root": str(root),
        "index": str(_index_path(root)),
        "count": len(entries),
        "drafts": entries,
    }


def _mutation_report(root, action, apply, details, undo_hint):
    return {
        "draft_root": str(root),
        "action": action,
        "apply": bool(apply),
        "dry_run": not apply,
        "changes": details,
        "undo": undo_hint,
    }


def draft_rename(ctx, args):
    root = draft_root(args.draft_root)
    document = _load_index(root)
    entry = _find_entry(document, args.draft)
    old_path = _require_inside(root, _entry_path(entry))
    new_name = str(args.new_name).strip()
    if not new_name or "/" in new_name:
        raise JyError("VALIDATION", "新名字不能为空，也不能包含 '/'")
    new_path = old_path.parent / new_name
    _require_inside(root, new_path)
    if new_path.exists():
        raise JyError("VALIDATION", f"目标目录已存在：{new_path}")
    changes = {
        "from": {"name": entry.get("draft_name"), "path": str(old_path)},
        "to": {"name": new_name, "path": str(new_path)},
    }
    backup = None
    if args.apply:
        old_path.rename(new_path)
        entry["draft_name"] = new_name
        entry["draft_fold_path"] = str(new_path)
        entry["draft_json_file"] = str(new_path / "draft_info.json")
        entry["draft_root_path"] = str(root)
        if entry.get("draft_cover"):
            entry["draft_cover"] = str(new_path / Path(entry["draft_cover"]).name)
        backup = _save_index(root, document, apply=True)
        changes["index_backup"] = str(backup)
    report = _mutation_report(root, "rename", args.apply, changes, f"--apply 会重命名目录并更新索引（备份在 {BACKUP_DIR}）")
    return ("executed" if args.apply else "preview"), report


def draft_duplicate(ctx, args):
    root = draft_root(args.draft_root)
    document = _load_index(root)
    entry = _find_entry(document, args.draft)
    source = _require_inside(root, _entry_path(entry))
    name = str(args.new_name or f"{source.name}-copy").strip()
    target = source.parent / name
    _require_inside(root, target)
    if target.exists():
        raise JyError("VALIDATION", f"目标目录已存在：{target}")
    changes = {"from": str(source), "to": str(target), "bytes": _tree_size(source)}
    backup = None
    if args.apply:
        shutil.copytree(source, target)
        clone = dict(entry)
        clone.update(
            {
                "draft_id": str(uuid.uuid4()).upper(),
                "draft_name": name,
                "draft_fold_path": str(target),
                "draft_json_file": str(target / "draft_info.json"),
                "draft_root_path": str(root),
                "tm_draft_create": _now_us(),
                "tm_draft_modified": _now_us(),
            }
        )
        if entry.get("draft_cover"):
            clone["draft_cover"] = str(target / Path(entry["draft_cover"]).name)
        document["all_draft_store"].append(clone)
        backup = _save_index(root, document, apply=True)
        if isinstance(document.get("draft_ids"), list):
            document["draft_ids"] = sorted(
                {str(item.get("draft_id")) for item in document["all_draft_store"]}
            )
            _save_index(root, document, apply=True)
        changes["index_backup"] = str(backup)
        changes["draft_id"] = clone["draft_id"]
    report = _mutation_report(root, "duplicate", args.apply, changes, f"--apply 会复制目录并登记新草稿（备份在 {BACKUP_DIR}）")
    return ("executed" if args.apply else "preview"), report


def draft_delete(ctx, args):
    root = draft_root(args.draft_root)
    document = _load_index(root)
    entry = _find_entry(document, args.draft)
    folder = _require_inside(root, _entry_path(entry))
    trash = root / TRASH_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{folder.name}"
    _require_inside(root, trash)
    apply = bool(args.apply or args.yes)
    changes = {
        "draft": entry.get("draft_name"),
        "path": str(folder),
        "trash": str(trash),
        "note": "移动到 .jy-trash（可手动恢复），不会直接删除",
    }
    backup = None
    if apply:
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(trash))
        document["all_draft_store"] = [
            item for item in _entries(document) if item.get("draft_id") != entry.get("draft_id")
        ]
        if isinstance(document.get("draft_ids"), list):
            document["draft_ids"] = [
                item for item in document["draft_ids"] if item != entry.get("draft_id")
            ]
        backup = _save_index(root, document, apply=True)
        changes["index_backup"] = str(backup)
    report = _mutation_report(root, "delete", apply, changes, f"从 {trash} 移回原位即可撤销")
    return ("executed" if apply else "preview"), report


def draft_move(ctx, args):
    root = draft_root(args.draft_root)
    document = _load_index(root)
    entry = _find_entry(document, args.draft)
    source = _require_inside(root, _entry_path(entry))
    folder_name = str(args.folder).strip()
    if not folder_name or "/" in folder_name:
        raise JyError("VALIDATION", "目标文件夹名不能为空，也不能包含 '/'")
    target_dir = root / folder_name
    _require_inside(root, target_dir)
    target = target_dir / source.name
    _require_inside(root, target)
    if target.exists():
        raise JyError("VALIDATION", f"目标位置已存在：{target}")
    changes = {"from": str(source), "to": str(target)}
    backup = None
    if args.apply:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        entry["draft_fold_path"] = str(target)
        entry["draft_json_file"] = str(target / "draft_info.json")
        if entry.get("draft_cover"):
            entry["draft_cover"] = str(target / Path(entry["draft_cover"]).name)
        backup = _save_index(root, document, apply=True)
        changes["index_backup"] = str(backup)
    report = _mutation_report(root, "move", args.apply, changes, f"--apply 会移动目录并更新索引（备份在 {BACKUP_DIR}）")
    return ("executed" if args.apply else "preview"), report


def draft_history(ctx, args):
    root = draft_root(args.draft_root)
    backups = []
    backup_dir = root / BACKUP_DIR
    if backup_dir.is_dir():
        for path in sorted(backup_dir.iterdir()):
            if path.is_file():
                backups.append(
                    {
                        "kind": "index",
                        "path": str(path),
                        "bytes": path.stat().st_size,
                        "modified": time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)
                        ),
                    }
                )
    draft_backups = []
    for entry in _entries(_load_index(root)):
        folder = Path(entry.get("draft_fold_path") or "")
        if not folder.is_dir():
            continue
        for pattern in (".backup", "draft_info.json.bak*"):
            for candidate in sorted(folder.glob(pattern)):
                draft_backups.append(
                    {
                        "kind": "draft",
                        "draft": entry.get("draft_name"),
                        "path": str(candidate),
                        "modified": time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.localtime(candidate.stat().st_mtime)
                        ),
                    }
                )
    return "readonly", {
        "draft_root": str(root),
        "index_backups": backups,
        "draft_backups": draft_backups,
        "count": len(backups) + len(draft_backups),
    }


# ---------------------------------------------------------------------------
# helpers + registration


def _now_us():
    return int(time.time() * 1_000_000)


def _tree_size(path):
    total = 0
    for item in Path(path).rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def register(r):
    group = r.parser_for(
        "draft",
        help="build a 剪映 draft, or list/rename/duplicate/delete/move drafts",
        description="草稿层：`build` 从 spec 生成 draft_info.json（严格校验字段、默认 staging 素材）；"
        "其余子命令管理剪映草稿根目录（默认 dry-run，--apply 才动，索引先备份，删除进 .jy-trash）。",
        group=True,
    )
    sub = group.add_subparsers(
        dest="draft_action", required=True, parser_class=r.parser_class, metavar="action"
    )

    build = sub.add_parser(
        "build",
        parents=[r.output_parent],
        help="write <dir>/draft_info.json from spec.json (strict fields, staged media)",
        description="读 spec（canvas + clips[] + texts[] + 可选 effects/filters/stickers）并生成剪映草稿。"
        "未知字段会报错（不会静默忽略）；clip 源文件默认 stage 到 ~/Movies/jy-staging 再引用，"
        "避免 ~/Downloads 触发 macOS TCC 弹窗卡死剪映（--no-staging 可关）。"
        "逐片段属性：transform{x,y,scale,rotation}/opacity/volume/crop{x,y,w,h}/speed/render_index；"
        "关键帧 keyframes[{property:KFTypeScaleX, points:[{time,values,left_control,right_control}]}]、"
        "速度曲线 curve_speed{name,id,points:[{x,y}]}、蒙版 mask{resource_type,name,resource_id,path,config}；"
        "文字动画 texts[].animations[{type:in|out|loop, resource_id, name?, duration?}]。",
    )
    build.add_argument("--spec", required=True, metavar="FILE", help="JSON 时间线 spec")
    build.add_argument(
        "--no-staging", action="store_true", help="reference the original paths (no copy)"
    )
    build.add_argument(
        "--fetch", action="store_true",
        help="download materials named in the spec that are not cached yet (`jy effects fetch`)",
    )
    build.add_argument(
        "--staging-dir", metavar="DIR", help="override the staging dir (default $JY_STAGING_DIR or ~/Movies/jy-staging)"
    )
    build.add_argument(
        "--no-register", action="store_true",
        help="do not add the draft to 剪映's home list (default: register when -o is inside the draft root)",
    )
    r.bind(build, "draft build", draft_build)

    registration = sub.add_parser(
        "register",
        parents=[r.verbose_parent],
        help="add a draft folder to 剪映's home list (dry-run unless --apply)",
        description="把草稿登记进剪映首页草稿列表：改写 <草稿根>/root_meta_info.json 的 all_draft_store，"
        "并维护 draft_ids 计数器（它不是条目数）。默认只预演；--apply 才写，写前双备份 + 原子替换，"
        "读取后索引被剪映改动时拒绝覆盖。",
    )
    registration.add_argument("draft", metavar="draft-dir", help="含 draft_info.json 的草稿目录")
    registration.add_argument("--name", help="列表里显示的名字（默认用草稿 name）")
    registration.add_argument("--draft-root", metavar="DIR",
                              help="剪映草稿根目录（默认取草稿目录的上一级，再退回本机安装路径）")
    registration.add_argument("--root-meta", metavar="FILE", help="直接指定 root_meta_info.json")
    registration.add_argument("--apply", action="store_true", help="真正写入索引")
    r.bind(registration, "draft register", draft_register)

    importer = sub.add_parser(
        "import",
        parents=[r.verbose_parent],
        help="copy an external draft folder into 剪映's draft root and list it",
        description="把草稿根目录之外的剪映草稿（例如项目文件夹里 `jy draft build` 出来的草稿）"
        "复制进剪映草稿根目录、改写草稿里记录的路径、再登记进首页列表。默认 dry-run；--apply 才复制并写入；"
        "目标目录永不覆盖（重名自动加 -2）。",
    )
    importer.add_argument("draft", metavar="draft-dir", help="草稿根目录之外的草稿目录")
    importer.add_argument("--name", help="导入后的草稿名（默认沿用目录名）")
    importer.add_argument("--draft-root", metavar="DIR", help="剪映草稿根目录")
    importer.add_argument("--root-meta", metavar="FILE", help="直接指定 root_meta_info.json")
    importer.add_argument("--move", action="store_true",
                          help="移动而不是复制（源目录会被移走）")
    importer.add_argument("--apply", action="store_true", help="真正复制/移动并登记")
    r.bind(importer, "draft import", draft_import)

    listing = sub.add_parser(
        "list", parents=[r.verbose_parent], help="list drafts from root_meta_info.json"
    )
    listing.add_argument("--draft-root", metavar="DIR")
    listing.add_argument("--folder", metavar="NAME", help="only drafts in this folder")
    r.bind(listing, "draft list", draft_list)

    rename = sub.add_parser(
        "rename", parents=[r.verbose_parent], help="rename a draft (dry-run unless --apply)"
    )
    rename.add_argument("draft", metavar="name|id")
    rename.add_argument("new_name", metavar="new-name")
    rename.add_argument("--draft-root", metavar="DIR")
    rename.add_argument("--apply", action="store_true")
    r.bind(rename, "draft rename", draft_rename)

    duplicate = sub.add_parser(
        "duplicate", parents=[r.verbose_parent], help="copy a draft (dry-run unless --apply)"
    )
    duplicate.add_argument("draft", metavar="name|id")
    duplicate.add_argument("--name", dest="new_name", metavar="new-name")
    duplicate.add_argument("--draft-root", metavar="DIR")
    duplicate.add_argument("--apply", action="store_true")
    r.bind(duplicate, "draft duplicate", draft_duplicate)

    delete = sub.add_parser(
        "delete",
        parents=[r.verbose_parent],
        help="move a draft to <root>/.jy-trash (dry-run unless --apply/--yes)",
    )
    delete.add_argument("draft", metavar="name|id")
    delete.add_argument("--draft-root", metavar="DIR")
    delete.add_argument("--apply", action="store_true")
    delete.add_argument("--yes", action="store_true", help="alias for --apply")
    r.bind(delete, "draft delete", draft_delete)

    move = sub.add_parser(
        "move", parents=[r.verbose_parent], help="move a draft into a folder (dry-run unless --apply)"
    )
    move.add_argument("draft", metavar="name|id")
    move.add_argument("folder", metavar="folder")
    move.add_argument("--draft-root", metavar="DIR")
    move.add_argument("--apply", action="store_true")
    r.bind(move, "draft move", draft_move)

    history = sub.add_parser(
        "history", parents=[r.verbose_parent], help="list index/draft backups"
    )
    history.add_argument("--draft-root", metavar="DIR")
    r.bind(history, "draft history", draft_history)
