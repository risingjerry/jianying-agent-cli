"""The 剪映-engine verbs: ``draft build`` and ``render``.

Kept separate from :mod:`jy_cli.commands` (the ffmpeg verbs) so the two engines
stay readable and independently testable. Handlers follow the same contract:
return ``(outcome, data)`` or raise :class:`JyError`; never print to stdout.

``render`` is the unattended path, so it carries three hardening layers:
  * a focus guard (:mod:`jy_cli.focus`) that keeps the user's front app in front
    while 剪映 renders (the render itself raises the 剪映 window);
  * transport resilience: pre-flight health, fresh descriptor discovery, one
    bounded retry, an explicit ``--relaunch-app`` escalation, and bounded waits
    so the command can never hang;
  * scratch-draft cleanup for jy-authored drafts under the cache directory.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import draft as drafts, ffmpeg, focus, jianying, jobs
from .verbs import _local_render
from .commands import _checked_output
from .envelope import JyError

ENGINE_NOTE = (
    "剪映原生引擎：草稿由 jy 用明文 draft_info.json 编写，渲染走 127.0.0.1 native "
    "bridge 的 film-export；只需要剪映在运行，无需点界面。"
)
# A wedged bridge must fail in seconds: health probes are capped at this budget and
# the export is never attempted until health answers.
HEALTH_TIMEOUT_S = 5.0
WARMUP_TIMEOUT_MS = 200_000

BUSY_HINT = (
    "剪映正忙于另一个导出（native_export_busy）。如果剪映界面里没有正在进行的导出，"
    "说明剪映的导出队列卡住了：重启剪映，或加 --relaunch-app 让 jy 重启它。"
)
RETRY_DELAY_S = 2.0


def draft_build(ctx, args):
    """Author a 剪映 draft folder from a JSON timeline spec."""
    tool = ctx.require()
    data = drafts.build(
        tool, args.spec, args.output, force=bool(args.force), warnings=ctx.warnings
    )
    data["note"] = ENGINE_NOTE
    root = Path(data["draft_info"]).parent
    _record_spec(root, args.spec)
    if _under_scratch_dir(root):
        jobs.mark_scratch_draft(root, label="draft build")
        data["scratch"] = jobs.draft_report(root)
        ctx.warnings.append(
            f"草稿写在 jy 的临时目录 {jobs.drafts_dir()} 下，已标记为可清理（jy clean）。"
        )
    if args.force and Path(data["draft_info"]).is_file():
        ctx.warnings.append("--force：已覆盖目标目录里的 draft_info.json")
    return "executed", data


def _under_scratch_dir(path):
    try:
        Path(path).resolve().relative_to(jobs.drafts_dir().resolve())
        return True
    except ValueError:
        return False



SPEC_RECORD = ".jy-spec.json"


def _record_spec(root, spec_path):
    """Keep the timeline spec next to the draft so a local fallback can re-render it."""
    source = Path(spec_path).expanduser().resolve()
    try:
        spec = json.loads(source.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for clip in spec.get("clips") or []:
        if isinstance(clip, dict) and clip.get("path"):
            candidate = Path(str(clip["path"])).expanduser()
            if not candidate.is_absolute():
                clip["path"] = str((source.parent / candidate).resolve())
    record = {
        "schema": "jy.spec.v1",
        "source": str(source),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "spec": spec,
    }
    try:
        (Path(root) / SPEC_RECORD).write_text(
            json.dumps(record, ensure_ascii=False, indent=1), "utf-8"
        )
    except OSError:
        return None
    return Path(root) / SPEC_RECORD


def _recorded_spec(root):
    path = Path(root) / SPEC_RECORD
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    spec = record.get("spec")
    if not isinstance(spec, dict):
        return None
    return {"spec": spec, "spec_dir": str(Path(record.get("source") or root).parent)}


_ID_KEYS = (
    "resourceId",
    "resource_id",
    "materialId",
    "material_id",
    "id",
    "resource",
    "item",
    "detail",
    "name",
    "title",
)
_MATERIAL_KINDS = ("videos", "texts", "audios", "stickers", "effects", "filters", "images")


def _material_index(root):
    """id -> {id,kind,name} from the draft, so a native error can name the item."""
    try:
        document = json.loads((Path(root) / "draft_info.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    materials = document.get("materials") or {}
    index = {}
    for kind, items in materials.items():
        if kind not in _MATERIAL_KINDS or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "").strip()
            if not identifier:
                continue
            name = (
                item.get("material_name")
                or item.get("name")
                or item.get("content")
                or item.get("resource_name")
            )
            if isinstance(name, str) and len(name) > 80:
                name = name[:77] + "…"
            index[identifier] = {
                "id": identifier,
                "kind": kind[:-1] if kind.endswith("s") else kind,
                "name": name if isinstance(name, str) and name.strip() else None,
            }
    return index


def _walk_identifiers(node, found, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if isinstance(value, (str, int)) and str(value).strip() and key in _ID_KEYS:
                found.append({"key": child, "value": str(value)})
            elif isinstance(value, (dict, list)):
                _walk_identifiers(value, found, child)
    elif isinstance(node, list):
        for position, value in enumerate(node):
            _walk_identifiers(value, found, f"{path}[{position}]")


def _blocked_item(error, root):
    """(item, identifiers) the native error points at, resolved against the draft."""
    details = error.details or {}
    body = details.get("native_body")
    identifiers = []
    if isinstance(body, (dict, list)):
        _walk_identifiers(body, identifiers)
    index = _material_index(root)
    resolved = None
    for identifier in identifiers:
        candidate = index.get(identifier["value"])
        if candidate:
            resolved = dict(candidate, matched_by=identifier["key"])
            break
    if resolved is None and identifiers:
        resolved = {
            "id": identifiers[0]["value"],
            "kind": None,
            "name": None,
            "matched_by": identifiers[0]["key"],
        }
    return resolved, identifiers, index


def _entitlement_error(error, root):
    """BLOCKED, but naming exactly what 剪映 refused and why the tier matters."""
    item, identifiers, _index = _blocked_item(error, root)
    native = (error.details or {}).get("native_error") or "unknown"
    if item and item.get("name"):
        label = f"「{item['name']}」（id={item['id']}"
        label += f"，{item['kind']}）" if item.get("kind") else "）"
        message = (
            f"剪映拒绝导出：{label} 需要更高的会员档位。剪映区分 VIP 与 SVIP —— "
            "同一条素材/同一个导出能力在不同档位权限不同，请在该素材的角标或剪映的导出"
            "提示里确认它需要哪一档（jy 离线读不到会员档位）。"
        )
    elif item:
        message = (
            f"剪映拒绝导出：id={item['id']} 的素材/导出项需要更高的会员档位。"
            "剪映区分 VIP 与 SVIP —— 请在该素材的角标或导出提示里确认它需要哪一档"
            "（jy 离线读不到会员档位）。"
        )
    else:
        message = (
            "剪映拒绝导出（权益不足），但回执没有给出具体素材 id。剪映区分 VIP 与 SVIP，"
            "同一条素材在不同档位权限不同：请在草稿/导出面板里检查带角标的那一项需要哪一档"
            "（jy 离线读不到会员档位）。"
        )
    message += f"草稿：{root}；native_error={native}"
    details = dict(error.details or {})
    details.update(
        {
            "draft_path": str(root),
            "native_error": native,
            "blocked_item": item,
            "identifiers": identifiers,
            "membership": {
                "tiers": ["VIP", "SVIP"],
                "tier_readable_offline": False,
                "hint": "剪映的会员档位只在服务端；本机没有可读缓存（vipRemindViewedInfo.ini 只是 UI 标记）",
            },
        }
    )
    return JyError("BLOCKED", message, retriable=False, details=details)


def _dependency_error(error, root):
    """UNSUPPORTED, but naming the missing resource and how to make 剪映 fetch it."""
    item, identifiers, _index = _blocked_item(error, root)
    native = (error.details or {}).get("native_error") or "unknown"
    if item and item.get("name"):
        label = f"「{item['name']}」（id={item['id']}）"
    elif item:
        label = f" id={item['id']} 的素材"
    else:
        label = "草稿引用的某个素材"
    message = (
        f"剪映本机缓存缺少{label}。打开剪映一次，让它在素材库/草稿里把这个素材下载或初始化，"
        f"然后再重试（jy 不联网、也不会替你下载素材）。草稿：{root}；native_error={native}"
    )
    details = dict(error.details or {})
    details.update(
        {
            "draft_path": str(root),
            "native_error": native,
            "missing_item": item,
            "identifiers": identifiers,
            "next": "open 剪映 once and let it fetch the material, then re-run jy render",
        }
    )
    return JyError("UNSUPPORTED", message, details=details)


ENTITLEMENT_TOKENS = ("entitlement", "commerce", "vip", "svip", "membership")


def _is_entitlement(error):
    if error.code == "BLOCKED":
        return True
    native = str((error.details or {}).get("native_error") or "").lower()
    return any(token in native for token in ENTITLEMENT_TOKENS)


def _is_dependency_missing(error):
    native = str((error.details or {}).get("native_error") or "")
    return native == "film_export_dependency_missing"


def _enrich(error, root):
    if _is_entitlement(error):
        return _entitlement_error(error, root)
    if _is_dependency_missing(error):
        return _dependency_error(error, root)
    return error


def _local_fallback(ctx, tool, root, target, exc):
    """--on-entitlement local: re-render the same timeline with ffmpeg, itemised."""
    recorded = _recorded_spec(root)
    if recorded is None:
        raise _with_fallback_note(
            exc,
            {
                "available": False,
                "reason": "这个草稿没有记录 spec（生成它的 jy 版本早于 .jy-spec.json），"
                "所以没有可用的本地近似；请用当前版本重新 `jy draft build`，或去掉需要更高档位的素材",
            },
        )
    try:
        data = _local_render.render_spec(
            tool,
            recorded["spec"],
            str(target),
            spec_dir=recorded["spec_dir"],
            verbose=ctx.verbose,
            warnings=ctx.warnings,
        )
    except JyError as local_error:
        raise _with_fallback_note(
            exc,
            {"available": True, "failed": local_error.as_dict()},
        ) from local_error
    return data


def _with_fallback_note(exc, note):
    details = dict(exc.details or {})
    details["local_fallback"] = note
    return JyError(exc.code, exc.message, retriable=exc.retriable, details=details)


def _resolve_draft_dir(value):
    """Draft folder + the real draft_info.json, handling 剪映's timeline migration.

    剪映 rewrites a draft the first time it opens it: the root ``draft_info.json``
    disappears and the live one moves to ``Timelines/<uuid>/draft_info.json``. The
    bridge still wants the *draft folder*, but summary/sha must read the migrated
    file — otherwise a previously-opened draft can never render again.
    """
    candidate = Path(value).expanduser()
    if not candidate.exists():
        raise JyError("NOT_FOUND", f"草稿路径不存在：{value}")
    if candidate.is_file():
        if candidate.name != "draft_info.json":
            raise JyError(
                "VALIDATION",
                f"草稿路径是文件但不是 draft_info.json：{candidate}（传草稿目录即可）",
            )
        candidate = candidate.parent
    resolved = drafts.resolve_draft_info(candidate)
    if resolved["info_path"] is None:
        raise JyError(
            "VALIDATION",
            f"草稿目录缺少 draft_info.json（根目录和 Timelines/*/ 都没有）：{candidate}",
            details={"draft_dir": str(candidate)},
        )
    return resolved


def _draft_summary(info_path):
    """Summarise a draft folder.

    剪映 encrypts ``draft_info.json`` as soon as it saves the draft itself
    (base64 AES-GCM). A draft that the user opened and edited in the GUI is
    therefore no longer plaintext, and rendering it must still work: the native
    exporter takes the file's bytes and its own loader handles both forms. So
    when the file is not JSON we fall back to the parts we can still know
    (path, file size) instead of failing the whole render with a JSONDecodeError.
    """
    info_path = Path(info_path)
    raw = info_path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "path": str(info_path.parent),
            "info_path": str(info_path),
            "duration_s": None,
            "canvas": {"width": None, "height": None, "fps": None},
            "tracks": [],
            "draft_encrypted": True,
            "draft_bytes": len(raw),
        }
    canvas = document.get("canvas_config") or {}
    tracks = document.get("tracks") or []
    return {
        "path": str(info_path.parent),
        "info_path": str(info_path),
        "duration_s": round((document.get("duration") or 0) / 1_000_000.0, 3),
        "canvas": {
            "width": canvas.get("width"),
            "height": canvas.get("height"),
            "fps": document.get("fps"),
        },
        "tracks": [
            {"type": track.get("type"), "segments": len(track.get("segments") or [])}
            for track in tracks
        ],
    }


def _sha256(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_receipt(receipt):
    """Receipt fields worth reporting — no paths outside the caller's own output."""
    if not isinstance(receipt, dict):
        return {}
    return {
        key: receipt.get(key)
        for key in ("format", "requestId", "snapshotId", "revision", "durationMs")
        if receipt.get(key) is not None
    }


def _preflight(timeout=HEALTH_TIMEOUT_S):
    """Cheap, secret-free reachability report taken before the render starts."""
    report = {
        "reachable": False,
        "health_ok": False,
        "logged_in": None,
        "route": None,
        "bridge_state": None,
    }
    bridge = _bridge_report(timeout)
    report["bridge_state"] = bridge
    report["bridge"] = {
        "pid": bridge.get("pid"),
        "port": bridge.get("port"),
        "descriptor": bridge.get("descriptor"),
    }
    if bridge["state"] != "ok":
        report["error"] = {"state": bridge["state"], "message": bridge.get("error")}
        return report
    try:
        client = jianying.Client(timeout=timeout)
    except JyError as exc:
        report["error"] = {"code": exc.code, "message": exc.message}
        return report
    report["bridge"] = jianying.public_state(client.desc)
    try:
        client.health()
        report["health_ok"] = True
        report["reachable"] = True
    except jianying.BridgeError as exc:
        report["error"] = exc.as_details()
        return report
    try:
        client.account_scope()
        report["logged_in"] = True
    except (JyError, jianying.BridgeError):
        report["logged_in"] = False
    try:
        report["route"] = client.probe_film_export_route()
    except jianying.BridgeError as exc:
        report["route"] = {"responded": False, **exc.as_details()}
    return report


def _retriable(exc):
    if exc.kind == "transport":
        return True
    if exc.status in (401,):
        return True
    if isinstance(exc.status, int) and exc.status >= 500:
        return True
    return False


def _busy(exc):
    return exc.code() == "native_export_busy"


# 剪映's export window can get stuck: retrying these never helps, the app has to
# be restarted (or --relaunch-app has to do it).
WEDGED_CODES = (
    "native_export_window_close_timeout",
    "native_export_window_unavailable",
    "native_export_stuck",
)


def _wedged(exc):
    return exc.code() in WEDGED_CODES


def _quit_app(wait_s=8.0, kill_wait_s=5.0):
    """Quit 剪映, escalating SIGTERM -> SIGKILL. Returns True when the pid is gone.

    A wedged 剪映 ignores SIGTERM (that is exactly the state that needs a restart),
    so the escalation is part of the recovery path, not an afterthought.
    """
    located = jianying.locate()
    pid = located.get("pid")
    if not located.get("running") or not pid:
        return True
    for sig, budget in ((signal.SIGTERM, wait_s), (signal.SIGKILL, kill_wait_s)):
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if not jobs.alive(pid):
                return True
            time.sleep(0.2)
    return not jobs.alive(pid)


def _app_uptime_s():
    """Seconds since the running 剪映 process started (None when unknown)."""
    located = jianying.locate()
    pid = located.get("pid")
    if not located.get("running") or not pid:
        return None
    done = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "etime="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    text = (done.stdout or "").strip()
    if done.returncode != 0 or not text:
        return None
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        try:
            days = int(head)
        except ValueError:
            return None
    try:
        parts = [int(piece) for piece in text.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _bridge_ready(timeout=5.0):
    """True only when the descriptor points at a live pid *and* health answers.

    A quitting 剪映 leaves its old descriptor behind (old pid/port), so
    ``locate().running`` alone is not enough after a relaunch.
    """
    located = jianying.locate()
    if not located.get("running") or not jobs.alive(located.get("pid")):
        return False
    try:
        jianying.Client(timeout=timeout).health()
    except (JyError, jianying.BridgeError):
        return False
    return True


def _relaunch(wait_ms):
    """Restart 剪映 and wait until the native bridge really answers (bounded)."""
    started = time.monotonic()
    quit_ok = _quit_app()
    opened = _open_bundle_path() or focus.activate({"bundle_id": focus.JIANYING_BUNDLE})
    deadline = time.monotonic() + max(1_000, int(wait_ms)) / 1000.0
    ready = False
    while time.monotonic() < deadline:
        if _bridge_ready():
            ready = True
            break
        time.sleep(0.5)
    return {
        "quit_ok": quit_ok,
        "opened": opened,
        "bridge_back": ready,
        "waited_ms": int(round((time.monotonic() - started) * 1000)),
    }


def _open_bundle_path():
    app = jianying.APP_BUNDLE
    if not app.is_dir():
        return False
    import subprocess

    try:
        done = subprocess.run(
            ["/usr/bin/open", str(app)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _render_once(root, temp_path, timeout_ms):
    """One bridge attempt with fresh discovery; raises BridgeError/JyError."""
    client = jianying.Client()
    receipt = client.export_draft(
        str(root), str(temp_path), fmt="mp4", timeout_ms=timeout_ms
    )
    return client, receipt


def _bridge_report(timeout_s=None):
    """Three-state bridge snapshot (never raises, never leaks the token)."""
    state = jianying.bridge_state(timeout=timeout_s or HEALTH_TIMEOUT_S)
    return {
        "state": state["state"],
        "pid": state.get("pid"),
        "port": state.get("port"),
        "reachable": bool(state.get("reachable")),
        "health_ms": state.get("health_ms"),
        "descriptor": state.get("descriptor"),
        "error": state.get("error"),
        "remedy": state.get("remedy"),
    }


def _health_gate(ctx, args, bridge):
    """Fail fast on a wedged/dead bridge; --relaunch-app is the only escalation.

    Returns the (possibly refreshed) bridge report plus the relaunch report when
    one was performed. Raises UNAVAILABLE immediately otherwise — the old
    behaviour was to hand a wedged bridge to the export path and wait ~10 min.
    """
    timeout_ms = int(getattr(args, "relaunch_wait_ms", 45_000))
    if bridge["state"] == "ok":
        return bridge, None
    if not getattr(args, "relaunch_app", False):
        if bridge["state"] == "wedged":
            raise JyError(
                "UNAVAILABLE",
                "剪映进程还在，但 native bridge 不响应（health "
                f"{bridge.get('health_ms') or '?'}ms 无应答，多半已卡死）。"
                "用 `jy render ... --relaunch-app` 让 jy 重启它，或手动重启剪映后再试；"
                "不要在这个 bridge 上反复重试。",
                retriable=True,
                details={"bridge": bridge, "hint": "--relaunch-app", "stage": "health_gate"},
            )
        raise JyError(
            "UNAVAILABLE",
            "剪映没有运行（找不到可用的 native bridge 描述文件）。打开剪映后重试；"
            "`jy render ... --relaunch-app` 会替你打开它并等 bridge 就绪。",
            retriable=True,
            details={"bridge": bridge, "hint": "--relaunch-app", "stage": "health_gate"},
        )
    ctx.warnings.append(
        f"bridge 状态 {bridge['state']}：--relaunch-app 生效，正在重启剪映"
        "（SIGTERM→SIGKILL，然后重开并等 health 200）。"
    )
    relaunch = _relaunch(timeout_ms)
    refreshed = _bridge_report()
    if refreshed["state"] != "ok":
        raise JyError(
            "UNAVAILABLE",
            "重启剪映后 native bridge 仍不可用"
            f"（当前状态 {refreshed['state']}，等待 {relaunch['waited_ms']}ms）。"
            "可以再试一次，或手动打开剪映确认它能正常完成一次导出。",
            retriable=True,
            details={
                "bridge": refreshed,
                "relaunch": relaunch,
                "stage": "after_relaunch",
            },
        )
    return refreshed, relaunch


def _warmup_export(ctx, tool, args, bridge):
    """Pay the known first-export cost on a throwaway 1 s draft (opt-in)."""
    report = {"performed": False, "elapsed_ms": 0, "failed": False}
    if not getattr(args, "warmup", False):
        report["reason"] = "disabled (pass --warmup to opt in)"
        return report
    cold = bool(getattr(args, "cold_start", False))
    flaky = (bridge.get("health_ms") or 0) > 1000
    if not (cold or flaky or getattr(args, "_relaunched", False)):
        report["reason"] = (
            "skipped: 剪映 looks warm (health fast, no relaunch, no cold start)"
        )
        return report
    timeout_ms = int(getattr(args, "warmup_timeout_ms", WARMUP_TIMEOUT_MS))
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="jy-warmup-") as tmp_dir:
            clip = Path(tmp_dir) / "warmup.mp4"
            ffmpeg.run_ffmpeg(
                tool,
                [
                    tool.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-y", str(clip),
                ],
                verbose=ctx.verbose,
            )
            spec = Path(tmp_dir) / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "canvas": {"width": 320, "height": 180, "fps": 30},
                        "clips": [{"path": str(clip), "start": 0, "duration": 1.0}],
                    }
                ),
                "utf-8",
            )
            draft_dir = Path(tmp_dir) / "draft"
            drafts.build(tool, str(spec), str(draft_dir), force=True, warnings=[])
            out = Path(tmp_dir) / "warmup-out.mp4"
            jianying.Client().export_draft(
                str(draft_dir), str(out), fmt="mp4", timeout_ms=timeout_ms
            )
            report["performed"] = True
            report["failed"] = not out.is_file()
    except (JyError, jianying.BridgeError, OSError) as exc:
        report["performed"] = True
        report["failed"] = True
        report["error"] = str(exc)[:300]
        ctx.warnings.append(
            f"--warmup 的预热导出失败（{str(exc)[:120]}）；仍会尝试正式导出。"
        )
    report["elapsed_ms"] = int(round((time.monotonic() - started) * 1000))
    if report["performed"] and not report["failed"]:
        ctx.warnings.append(
            f"--warmup：已用 1s 草稿预热剪映（{report['elapsed_ms']}ms），正式导出随后开始。"
        )
    return report


def _render_with_recovery(ctx, args, root, temp_path, timeout_ms, attempts):
    """Try the render, re-discover on transport failures, bounded and explainable.

    Failure ladder (never unbounded, never silent):
      1. first attempt with a freshly discovered bridge;
      2. ``native_export_busy`` -> wait, retry once (the queue may just be draining);
      3. transport/401/5xx -> re-discover and retry once;
      4. ``--relaunch-app`` -> quit + reopen 剪映 (clears a wedged export queue too),
         wait for the bridge, retry once.
    """
    wait_ms = int(getattr(args, "relaunch_wait_ms", 45_000))
    relaunch_allowed = bool(getattr(args, "relaunch_app", False))
    cold_start = bool(getattr(args, "cold_start", False))
    attempts_total = 3
    last_error = None
    relaunched = False
    cold_retry_used = False
    for number in range(1, attempts_total + 1):
        entry = {"attempt": number}
        started = time.monotonic()
        try:
            client, receipt = _render_once(root, temp_path, timeout_ms)
            entry.update(
                {
                    "ok": True,
                    "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
                    "bridge": jianying.public_state(client.desc),
                }
            )
            attempts.append(entry)
            return client, receipt
        except jianying.BridgeError as exc:
            last_error = exc
            entry.update(
                {
                    "ok": False,
                    "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
                    **exc.as_details(),
                }
            )
            attempts.append(entry)
            if number >= attempts_total:
                break
            if _wedged(exc):
                # Normally terminal (a stuck export window only gets worse), but the
                # FIRST export after a fresh 剪映 launch reliably hits this and then
                # succeeds: allow exactly one cold-start retry.
                if cold_start and not cold_retry_used and number < attempts_total:
                    cold_retry_used = True
                    entry["cold_start_retry"] = True
                    ctx.warnings.append(
                        "剪映看起来是刚启动的（冷启动）：首次 film-export 常见 "
                        f"{exc.code()}；按冷启动规则重试一次。"
                    )
                    time.sleep(1.0)
                    continue
                break
            if _busy(exc):
                if relaunch_allowed and not relaunched:
                    relaunched = True
                    ctx.warnings.append(
                        "剪映的导出队列卡住了（native_export_busy 重试后仍占用）："
                        "--relaunch-app 生效，正在重启剪映。"
                    )
                    entry["recovery"] = {"relaunch": _relaunch(wait_ms)}
                    continue
                ctx.warnings.append(
                    f"剪映报告 native_export_busy（第 {number} 次）；{RETRY_DELAY_S:g}s 后重试。"
                )
                time.sleep(RETRY_DELAY_S)
                continue
            if _retriable(exc):
                recovery = {"rediscovered": True, "attempt": number}
                if relaunch_allowed and not relaunched:
                    relaunched = True
                    ctx.warnings.append(
                        "native bridge 不可达：--relaunch-app 生效，正在重启剪映"
                        "（未保存的界面状态会丢失；草稿已自动保存）。"
                    )
                    recovery["relaunch"] = _relaunch(wait_ms)
                entry["recovery"] = recovery
                continue
            break
    raise _bridge_to_jy_error(last_error)


def _bridge_to_jy_error(exc):
    if exc is None:
        return JyError("INTERNAL", "剪映导出失败（没有收到可诊断的错误）")
    if _wedged(exc):
        return JyError(
            "UNAVAILABLE",
            "剪映的导出窗口卡住了（" + (exc.code() or "wedged") + "）。这种情况不会自己恢复："
            "重启剪映，或加 --relaunch-app 让 jy 重启它再试。jy 不会在这里重试（重试只会"
            "让导出窗口更卡）。",
            retriable=False,
            details=exc.as_details(),
        )
    if _busy(exc):
        return JyError(
            "UNAVAILABLE",
            BUSY_HINT,
            retriable=True,
            details=exc.as_details(),
        )
    return jianying.to_jy_error(exc)


def _render_remote(ctx, args, resolved):
    """``--remote HOST``: same draft, rendered by 剪映 on a Windows PC (see remote.py)."""
    from . import remote

    timeout_ms = int(getattr(args, "timeout_ms", jianying.DEFAULT_TIMEOUT_MS))
    if timeout_ms < 5_000:
        raise JyError("VALIDATION", "--timeout-ms 至少 5000")
    tool = ctx.require()
    target = None
    if getattr(args, "fetch", False):
        target = _checked_output(args.output, args.force)
    info_path = resolved["info_path"]
    summary = _draft_summary(info_path)
    outcome, data = remote.render_remote(
        ctx, args, tool=tool, resolved=resolved, summary=summary,
        sha256=_sha256(info_path), target=target, bridge_error=_bridge_to_jy_error,
    )
    if outcome != "executed":
        return outcome, data
    root = resolved["draft_dir"]
    cleanup = data["cleanup"]
    if jobs.is_scratch_draft(root) and not getattr(args, "keep_draft", False):
        cleanup.update(jobs.remove_scratch_draft(root))
        cleanup["kept"] = False
        ctx.warnings.append(f"已清理 jy 的临时草稿：{root}（--keep-draft 可保留）")
    if not getattr(args, "fetch", False):
        ctx.warnings.append(
            f"成片留在 {args.remote} 上：{data['remote']['output_path']}（加 --fetch 同时拷回 {args.output}）。"
        )
    data["note"] = ENGINE_NOTE + " --remote：在远程 Windows 剪映上导出，Mac 只负责打包和校验。"
    return outcome, data


def render(ctx, args):
    """Render a draft folder to MP4 through the 剪映 native film-export job."""
    if getattr(args, "engine", "jianying") == "local":
        raise JyError(
            "UNSUPPORTED",
            "本地 ffmpeg 引擎不渲染剪映草稿（draft_info.json 是剪映的格式）。"
            "请用 `jy export`/`jy trim`/`jy concat` 等本地动词处理源文件，"
            "或去掉 --engine=local 用剪映引擎（--engine=jianying）。",
        )
    resolved = _resolve_draft_dir(args.draft)
    root = resolved["draft_dir"]
    info_path = resolved["info_path"]
    if getattr(args, "remote", None):
        return _render_remote(ctx, args, resolved)
    if getattr(args, "fetch", False):
        raise JyError("VALIDATION", "--fetch 只和 --remote 一起用（把远程成片拷回 -o）")
    target = _checked_output(args.output, args.force)
    timeout_ms = int(getattr(args, "timeout_ms", jianying.DEFAULT_TIMEOUT_MS))
    if timeout_ms < 5_000:
        raise JyError("VALIDATION", "--timeout-ms 至少 5000")
    health_timeout_ms = int(getattr(args, "health_timeout_ms", 5000))
    if not 500 <= health_timeout_ms <= 5000:
        raise JyError("VALIDATION", "--health-timeout-ms 需在 500..5000（快速失败优先）")
    args.health_timeout_s = health_timeout_ms / 1000.0
    interval_ms = int(getattr(args, "focus_interval_ms", 200))
    if not 50 <= interval_ms <= 5000:
        raise JyError("VALIDATION", "--focus-interval-ms 需在 50..5000")
    wait_ms = int(getattr(args, "relaunch_wait_ms", 45_000))
    if wait_ms < 1_000:
        raise JyError("VALIDATION", "--relaunch-wait-ms 至少 1000")

    cold_window_ms = int(getattr(args, "cold_start_window_ms", 180_000))
    if cold_window_ms < 0:
        raise JyError("VALIDATION", "--cold-start-window-ms 必须 >= 0")
    uptime_s = _app_uptime_s()
    cold_start = bool(
        cold_window_ms and uptime_s is not None and uptime_s * 1000 < cold_window_ms
    )
    args.cold_start = cold_start
    cold_report = {
        "detected": cold_start,
        "app_uptime_s": uptime_s,
        "window_ms": cold_window_ms,
        "behaviour": "剪映 刚启动（冷启动）时首次 film-export 常以 "
        "native_export_window_close_timeout 失败；检测到冷启动时 jy 会自动重试一次",
    }
    if cold_start:
        ctx.warnings.append(
            f"剪映 刚启动 {uptime_s}s（< {cold_window_ms / 1000:g}s）：首次 film-export "
            "常会超时，jy 会在冷启动规则下自动重试一次。"
        )

    tool = ctx.require()
    summary = _draft_summary(info_path)
    sha256 = _sha256(info_path)
    if resolved["layout"] == "multi-timeline":
        ctx.warnings.append(
            "草稿已被剪映迁移成多时间线布局：真正的 draft_info.json 在 "
            f"{info_path}（main_timeline_id={resolved['timeline_id']}）；"
            "jy 已按该文件算 SHA/时长，并把草稿目录交给 bridge。"
        )
    preflight = _preflight(getattr(args, "health_timeout_s", HEALTH_TIMEOUT_S))

    if getattr(args, "preflight", False):
        return "readonly", {
            "engine": "jianying",
            "draft": {
            **summary,
            "sha256": sha256,
            "layout": resolved["layout"],
            "timeline_id": resolved["timeline_id"],
        },
            "output_path": str(target),
            "preflight": preflight,
            "bridge_state": preflight["bridge_state"],
            "cold_start": cold_report,
            "focus": focus.state(),
            "lock": {"path": str(jobs.cache_root() / jobs.RENDER_LOCK), "held": False},
            "note": ENGINE_NOTE
            + " --preflight 只做只读体检，不会让剪映开始导出。"
            " bridge_state 三态：not_running（打开剪映）/ wedged（重启或 --relaunch-app）/ ok。",
        }

    # Fast health gate: a wedged bridge must fail in seconds, not minutes.
    bridge, relaunch_report = _health_gate(ctx, args, preflight["bridge_state"])
    args._relaunched = bool(relaunch_report)
    if relaunch_report:
        # The app we just started is the process that matters now; the pre-relaunch
        # uptime measured the wedged one and would mis-report cold_start.
        uptime_s = _app_uptime_s()
        cold_start = bool(
            cold_window_ms and uptime_s is not None and uptime_s * 1000 < cold_window_ms
        )
        args.cold_start = cold_start
        cold_report = {
            "detected": cold_start,
            "app_uptime_s": uptime_s,
            "window_ms": cold_window_ms,
            "measured_after_relaunch": True,
            "behaviour": "剪映 刚启动（冷启动）时首次 film-export 常以 "
            "native_export_window_close_timeout 失败；检测到冷启动时 jy 会自动重试一次",
        }
        ctx.warnings.append(
            f"重启后的剪映 uptime={uptime_s}s：冷启动={'是' if cold_start else '否'}"
            "（--warmup 会先付掉首次导出的代价）。"
        )
    warmup_report = _warmup_export(ctx, tool, args, bridge)

    # Render next to the requested output, then atomically move it into place.
    # This keeps --force honest and never leaves a half-written target behind.
    handle, temp_name = tempfile.mkstemp(
        prefix=".jy-jianying-",
        suffix=os.path.splitext(target.name)[1] or ".mp4",
        dir=str(target.parent),
    )
    os.close(handle)
    Path(temp_name).unlink(missing_ok=True)
    temp_path = Path(temp_name)

    attempts = []
    lock = jobs.RenderLock(timeout_ms=int(getattr(args, "lock_wait_ms", 30_000)))
    lock_info = lock.acquire()
    if lock_info["waited_ms"] > 250:
        ctx.warnings.append(
            f"等另一个 jy render 释放剪映导出窗口 {lock_info['waited_ms']}ms"
            "（film-export 单实例，串行是必须的）"
        )
    focus_mode = getattr(args, "focus_mode", "watch")
    guard = focus.FocusGuard(
        interval=interval_ms / 1000.0, mode=focus_mode
    ).start(avoid_bundle=focus.JIANYING_BUNDLE)
    receipts = {}
    try:
        try:
            client, receipt = _render_with_recovery(
                ctx, args, root, temp_path, timeout_ms, attempts
            )
            receipts = receipt
            if not temp_path.is_file():
                raise JyError(
                    "INTERNAL",
                    "剪映回执成功，但目标位置没有产出文件（film_export_output_missing 的变体）。",
                    details={"receipt": _safe_receipt(receipt), "path": str(temp_path)},
                )
            output = ffmpeg.output_info(tool, str(temp_path))
            if not output.get("codec") or not output.get("width") or not output.get("height"):
                raise JyError(
                    "INTERNAL",
                    "剪映产出不是可播放的视频文件（ffprobe 没有视频流）。",
                    details={"output": output},
                )
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
            guard_report = guard.stop()
            lock.release()
    except JyError as error:
        error = _enrich(error, root)
        error.details.setdefault("focus_guard", guard_report)
        error.details.setdefault("attempts", attempts)
        error.details.setdefault("preflight", preflight)
        error.details.setdefault("lock", lock_info)
        error.details.setdefault("cold_start", cold_report)
        error.details.setdefault("bridge", bridge)
        if (
            getattr(args, "on_entitlement", "fail") == "local"
            and _is_entitlement(error)
        ):
            # Explicit, itemised downgrade — never a silent substitution.
            ctx.warnings.append(
                "剪映因会员档位拒绝了导出；--on-entitlement local：改用 ffmpeg 本地近似重渲染，"
                "降级明细见 data.degraded_items。"
            )
            degraded = _local_fallback(ctx, tool, root, target, error)
            degraded["fallback"] = {
                "from": "jianying",
                "reason": (error.details or {}).get("native_error"),
                "blocked_item": (error.details or {}).get("blocked_item"),
                "membership": (error.details or {}).get("membership"),
            }
            degraded["draft"] = {**summary, "sha256": sha256}
            degraded["focus_guard"] = guard_report
            degraded["preflight"] = preflight
            return "executed", degraded
        raise

    output["path"] = str(target)
    ctx.warnings.append(
        "剪映渲染会在这个草稿目录里补充 staging 文件（draft_content.json、template.tmp 等）"
        "并规范化 draft_info.json，所以渲染后草稿 SHA 会变；这是剪映的正常行为，jy 不删除它们。"
    )

    cleanup = None
    if jobs.is_scratch_draft(root):
        if getattr(args, "keep_draft", False):
            cleanup = {"kept": True, "path": str(root), "reason": "--keep-draft"}
        else:
            cleanup = jobs.remove_scratch_draft(root)
            cleanup["kept"] = False
            ctx.warnings.append(
                f"已清理 jy 的临时草稿：{root}（--keep-draft 可保留）"
            )

    data = {
        "engine": "jianying",
        "draft": {
            **summary,
            "sha256": sha256,
            "layout": resolved["layout"],
            "timeline_id": resolved["timeline_id"],
        },
        "output": output,
        "bridge": jianying.public_state(client.desc),
        "receipt": _safe_receipt(receipts),
        "preflight": preflight,
        "attempts": attempts,
        "focus_guard": guard_report,
        "lock": lock_info,
        "cold_start": cold_report,
        "bridge": bridge,
        "warmup": warmup_report,
        "relaunch": relaunch_report,
        "cleanup": cleanup,
        "note": ENGINE_NOTE,
    }
    return "executed", data
