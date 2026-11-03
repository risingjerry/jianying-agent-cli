"""Stop 剪映's bundled Sparkle updater without patching the app.

剪映 (VideoFusion) ships ``Sparkle.framework`` and downloads/prompts updates on its own
schedule. Its own "auto update" switch lives in
``~/Movies/JianyingPro/User Data/Config/globalSetting`` as ``enableAutoUpdate``; turning it
off is not always enough (a downloaded update can already be staged and installed on quit).

This module keeps the decision reversible and observable:

* ``enableAutoUpdate=false`` in the config (with a backup of the original file);
* any already-staged Sparkle install cache / partial download is **moved** (never deleted)
  into a timestamped folder under ``~/Library/Application Support/JianyingUpdateControl/``;
* the exact 剪映 update helper processes are terminated by path identity, never by name
  pattern;
* the installed bundle and the (now empty) Sparkle cache get ``UF_IMMUTABLE`` (``chflags
  uchg``), so an updater cannot swap the app behind the user's back. A manifest of the
  original flags is written first, and ``unlock`` restores it (with a device+inode check,
  so a replaced bundle is never touched).

Only regular files and directories are flagged; symlinks are skipped (following them would
reach outside the bundle). Nothing is deleted, no daemon is installed, and no network rule
is changed.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

from .envelope import JyError

APP = Path("/Applications/VideoFusion-macOS.app")
BUNDLE_ID = "com.lemon.lvpro"
STATE_DIR = Path.home() / "Library" / "Application Support" / "JianyingUpdateControl"
CONFIG = Path.home() / "Movies" / "JianyingPro" / "User Data" / "Config" / "globalSetting"
SPARKLE_CACHE = (
    Path.home() / "Library" / "Caches" / "com.lemon.lvpro" / "org.sparkle-project.Sparkle"
)
DOWNLOAD_DIR = Path.home() / "Movies" / "JianyingPro" / "User Data" / "Download"
AUTO_UPDATE_KEY = "enableAutoUpdate"


# --------------------------------------------------------------------------- #
# read helpers


def bundle_id():
    try:
        with open(APP / "Contents" / "Info.plist", "rb") as handle:
            return str(plistlib.load(handle).get("CFBundleIdentifier") or "")
    except (OSError, plistlib.InvalidFileException):
        return ""


def bundle_version():
    try:
        with open(APP / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
        return {
            "short": info.get("CFBundleShortVersionString"),
            "build": info.get("CFBundleVersion"),
        }
    except (OSError, plistlib.InvalidFileException):
        return {"short": None, "build": None}


def read_config():
    """The 剪映 update-related ``globalSetting`` keys, as they are on disk."""
    values = {}
    try:
        for line in CONFIG.read_text("utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    except OSError:
        # Keep the shape stable: `status` always reports the three keys, null when the
        # config file does not exist (CI, or 剪映 never launched on this machine).
        return {
            "path": str(CONFIG),
            "readable": False,
            "auto_update": None,
            "silent_upgrade": None,
            "interval_min": None,
            "values": {},
        }
    return {
        "path": str(CONFIG),
        "readable": True,
        "auto_update": values.get(AUTO_UPDATE_KEY),
        "silent_upgrade": values.get("totalSilentUpgradeSwitch"),
        "interval_min": values.get("upgradeIntervalMin"),
        "values": {
            key: values.get(key)
            for key in (AUTO_UPDATE_KEY, "totalSilentUpgradeSwitch", "upgradeIntervalMin")
        },
    }


def _walk_paths(root):
    """Every regular file and directory under ``root``, once each; symlinks skipped."""
    root = Path(root)
    yield root
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            yield Path(base) / name


def _flag_count(root, bit=stat.UF_IMMUTABLE):
    total = locked = 0
    for path in _walk_paths(root):
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        total += 1
        if info.st_flags & bit:
            locked += 1
    return total, locked


def _manifests():
    if not STATE_DIR.is_dir():
        return []
    found = []
    for base in STATE_DIR.iterdir():
        candidate = base / "original-flags.json"
        if candidate.is_file():
            found.append(candidate)
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def update_helpers():
    """Running 剪映 update helpers, matched by executable path — never by name."""
    expected = {
        str(APP / "Contents" / "Frameworks" / "Sparkle.framework" / "Versions" / "B" / "Autoupdate"),
    }
    if SPARKLE_CACHE.is_dir():
        expected.update(
            str(path)
            for path in SPARKLE_CACHE.rglob("Updater.app/Contents/MacOS/Updater")
        )
    found = []
    try:
        listing = subprocess.run(
            ["/bin/ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if command.strip() in expected:
            found.append({"pid": int(pid), "command": command.strip()})
    return found


def _quarantine_summary():
    """Every state-dir run: how many backups and quarantined files it holds."""
    if not STATE_DIR.is_dir():
        return []
    rows = []
    for path in sorted(STATE_DIR.iterdir()):
        if not path.is_dir():
            continue
        known = {CONFIG.name, "original-flags.json", "verification.json"}
        rows.append({
            "path": str(path),
            "quarantined": len([item for item in path.iterdir() if item.name not in known]),
            "config_backup": (path / CONFIG.name).is_file(),
            "manifest": (path / "original-flags.json").is_file(),
        })
    return rows


def pending_files():
    """Update artifacts that could still be installed: staged cache and partial downloads."""
    pending = []
    if SPARKLE_CACHE.is_dir():
        pending.extend(str(path) for path in sorted(SPARKLE_CACHE.iterdir()))
    if DOWNLOAD_DIR.is_dir():
        pending.extend(
            str(path)
            for path in sorted(DOWNLOAD_DIR.glob("update_*"))
        )
    return pending


def summary():
    """Cheap status for ``jy doctor``: no recursive walk."""
    try:
        app_flags = os.lstat(APP).st_flags
        app_locked = bool(app_flags & stat.UF_IMMUTABLE)
    except OSError:
        app_locked = False
    try:
        cache_locked = bool(os.lstat(SPARKLE_CACHE).st_flags & stat.UF_IMMUTABLE)
    except OSError:
        cache_locked = False
    config = read_config()
    manifests = _manifests()
    return {
        "auto_update": config.get("auto_update"),
        "app_locked": app_locked,
        "sparkle_cache_locked": cache_locked,
        "manifest": str(manifests[0]) if manifests else None,
        "pending_files": len(pending_files()),
        "helpers": len(update_helpers()),
    }


def status():
    config = read_config()
    versions = bundle_version()
    manifests = _manifests()
    total = locked = 0
    if APP.is_dir():
        total, locked = _flag_count(APP)
    cache_total = cache_locked = 0
    cache_children = []
    if SPARKLE_CACHE.is_dir():
        cache_total, cache_locked = _flag_count(SPARKLE_CACHE)
        cache_children = [path.name for path in sorted(SPARKLE_CACHE.iterdir())]
    return {
        "app": {
            "path": str(APP),
            "bundle_id": bundle_id(),
            "version": versions,
            "exists": APP.is_dir(),
            "entries": total,
            "locked_entries": locked,
            "immutable": total > 0 and locked == total,
        },
        "config": config,
        "sparkle_cache": {
            "path": str(SPARKLE_CACHE),
            "exists": SPARKLE_CACHE.is_dir(),
            "entries": cache_total,
            "locked_entries": cache_locked,
            "children": cache_children,
        },
        "helpers": update_helpers(),
        "pending_files": pending_files(),
        "state_dir": str(STATE_DIR),
        "manifests": [str(path) for path in manifests],
        "quarantine": _quarantine_summary(),
    }


# --------------------------------------------------------------------------- #
# write helpers


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _require_app():
    actual = bundle_id()
    if actual != BUNDLE_ID:
        raise JyError(
            "UNSUPPORTED",
            f"{APP} 不是剪映（CFBundleIdentifier={actual or '读不到'}，期望 {BUNDLE_ID}）",
        )


def _set_auto_update(value):
    """Rewrite just the ``enableAutoUpdate`` line; every other line is preserved."""
    text = CONFIG.read_text("utf-8")
    lines = text.splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == AUTO_UPDATE_KEY:
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{AUTO_UPDATE_KEY}={value}{ending}"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{AUTO_UPDATE_KEY}={value}\n")
    CONFIG.write_text("".join(lines), encoding="utf-8")


def _backup_config(target_dir):
    if not CONFIG.is_file():
        return None
    target = target_dir / CONFIG.name
    shutil.copy2(CONFIG, target)
    return str(target)


def _terminate(helpers):
    stopped, failed = [], []
    for helper in helpers:
        try:
            os.kill(helper["pid"], signal.SIGTERM)
            stopped.append(helper)
        except OSError as exc:
            failed.append({**helper, "error": str(exc)})
    return stopped, failed


def _quarantine(pending, target_dir):
    """Move staged update artifacts into the state dir; never delete."""
    moved, failed = [], []
    destination = target_dir / "cancelled-update"
    for item in pending:
        source = Path(item)
        if not source.exists():
            continue
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / source.name
            suffix = 2
            while target.exists():
                target = destination / f"{source.name}-{suffix}"
                suffix += 1
            shutil.move(str(source), str(target))
            moved.append({"from": str(source), "to": str(target)})
        except OSError as exc:
            failed.append({"path": str(source), "error": str(exc)})
    return moved, failed


def _walk_entries(roots):
    """Manifest entries for every regular file/dir under ``roots`` (symlinks skipped)."""
    entries = []
    for root in roots:
        if not Path(root).exists():
            continue
        for name in _walk_paths(root):
            try:
                info = os.lstat(name)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            entries.append({
                "path": str(name),
                # Record the *unlocked* state: masking only UF_IMMUTABLE keeps any
                # other flag, and re-locking an already locked bundle must not
                # produce a manifest that restores the lock.
                "flags": info.st_flags & ~stat.UF_IMMUTABLE,
                "was_locked": bool(info.st_flags & stat.UF_IMMUTABLE),
                "device": info.st_dev,
                "inode": info.st_ino,
            })
    return entries


def _chflags(entries, add=True):
    """Set/clear ``UF_IMMUTABLE`` on manifest entries whose inode still matches."""
    changed, skipped = 0, []
    for entry in entries:
        try:
            info = os.lstat(entry["path"])
        except OSError:
            skipped.append({"path": entry["path"], "reason": "missing"})
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if (entry.get("device"), entry.get("inode")) not in ((None, None), (info.st_dev, info.st_ino)):
            skipped.append({"path": entry["path"], "reason": "replaced"})
            continue
        flags = info.st_flags | stat.UF_IMMUTABLE if add else entry.get("flags", 0)
        if flags == info.st_flags:
            continue
        try:
            os.chflags(entry["path"], flags, follow_symlinks=False)
            changed += 1
        except OSError as exc:
            skipped.append({"path": entry["path"], "reason": str(exc)})
    return changed, skipped


# --------------------------------------------------------------------------- #
# verbs


def lock(*, apply=False, force=False):
    """Disable 剪映's updater and freeze the bundle against replacement."""
    _require_app()
    running = bool(update_helpers())
    if running and not force:
        raise JyError(
            "BLOCKED",
            "剪映的更新助手还在运行：先正常退出剪映，或加 --force 只终止更新助手",
            details={"helpers": update_helpers()},
        )
    config = read_config()
    pending = pending_files()
    total, locked = _flag_count(APP) if APP.is_dir() else (0, 0)
    plan = {
        "schema": "jy.update.v1",
        "app": str(APP),
        "version": bundle_version(),
        "applied": False,
        "running": running,
        "before": {
            "auto_update": config.get("auto_update"),
            "locked_entries": locked,
            "entries": total,
            "pending_files": pending,
            "helpers": update_helpers(),
        },
        "actions": [],
    }
    if not apply:
        plan["note"] = ("预演：未改动。加 --apply 才写 globalSetting、隔离待安装更新、"
                        "并给剪映 App 与更新缓存加 UF_IMMUTABLE")
        return plan
    stamp = _stamp()
    target_dir = STATE_DIR / stamp
    target_dir.mkdir(parents=True, exist_ok=True)
    backups = {"config": _backup_config(target_dir)}
    if config.get("auto_update") != "false":
        _set_auto_update("false")
        plan["actions"].append({"action": "config", "key": AUTO_UPDATE_KEY, "value": "false"})
    helpers = update_helpers()
    if helpers:
        stopped, failed = _terminate(helpers)
        plan["actions"].append({"action": "terminate", "stopped": stopped, "failed": failed})
        time.sleep(1)
    if pending:
        moved, failed = _quarantine(pending, target_dir)
        plan["actions"].append({"action": "quarantine", "moved": moved, "failed": failed})
    # A missing cache dir is recreated by the updater, so lock an empty one too.
    SPARKLE_CACHE.mkdir(parents=True, exist_ok=True)
    # The cache directory itself may already be immutable from a previous lock.
    for path in (SPARKLE_CACHE, APP):
        try:
            os.chflags(path, os.lstat(path).st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)
        except OSError:
            pass
    entries = _walk_entries([str(APP), str(SPARKLE_CACHE)])
    manifest = target_dir / "original-flags.json"
    manifest.write_text(json.dumps({
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bundle_version": bundle_version().get("build"),
        "entries": entries,
    }, ensure_ascii=False), encoding="utf-8")
    changed, skipped = _chflags(entries, add=True)
    plan["actions"].append({"action": "immutable", "entries": len(entries), "changed": changed,
                            "skipped": skipped[:10], "skipped_count": len(skipped)})
    ok, locked_after = _flag_count(APP)
    plan.update({
        "applied": True,
        "manifest": str(manifest),
        "backup": backups,
        "after": {
            "auto_update": read_config().get("auto_update"),
            "locked_entries": locked_after,
            "entries": ok,
            "pending_files": pending_files(),
            "helpers": update_helpers(),
        },
    })
    (target_dir / "verification.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return plan


def unlock(*, apply=False, restore_config=False):
    """Clear the immutable flags recorded in the newest manifest (keeps auto-update off)."""
    _require_app()
    manifests = _manifests()
    if not manifests:
        raise JyError(
            "NOT_FOUND",
            f"找不到加锁时写的 manifest（{STATE_DIR}/*/original-flags.json）；"
            "没有它就不猜原始 flags，请手动 `chflags -R nouchg /Applications/VideoFusion-macOS.app`",
        )
    manifest = manifests[0]
    try:
        recorded = json.loads(manifest.read_text("utf-8")).get("entries") or []
    except (OSError, json.JSONDecodeError) as exc:
        raise JyError("UNSUPPORTED", f"manifest 不是合法 JSON：{manifest}",
                      details={"error": str(exc)}) from exc
    plan = {
        "schema": "jy.update.v1",
        "app": str(APP),
        "applied": False,
        "manifest": str(manifest),
        "entries": len(recorded),
        "restore_config": bool(restore_config),
    }
    if not apply:
        plan["note"] = "预演：未改动。加 --apply 才清除 UF_IMMUTABLE（不会重新打开自动更新）"
        return plan
    changed, skipped = _chflags(recorded, add=False)
    try:
        os.chflags(SPARKLE_CACHE, os.lstat(SPARKLE_CACHE).st_flags & ~stat.UF_IMMUTABLE,
                   follow_symlinks=False)
    except OSError:
        pass
    plan.update({"applied": True, "cleared": changed, "skipped_count": len(skipped),
                 "skipped": skipped[:10]})
    if restore_config:
        backup = manifest.parent / CONFIG.name
        if backup.is_file():
            shutil.copy2(backup, CONFIG)
            plan["config_restored_from"] = str(backup)
    _total, locked = _flag_count(APP)
    plan["locked_entries_after"] = locked
    plan["auto_update_after"] = read_config().get("auto_update")
    return plan
