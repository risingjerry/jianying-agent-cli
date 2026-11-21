"""``jy render --remote HOST``: render a Mac-authored 剪映 draft on a Windows PC.

The Windows build of 剪映 ships the same native bridge as the Mac build, so the
export call is identical. What differs is everything around it:

  1. probe   ssh HOST -> remote_render.ps1 -Mode probe: game-mode flag, 剪映 app
             dir, bridge state, login, and which of the draft's Mac resources
             (app fonts, cached effect packages) already exist on the PC;
  2. pack    copy the draft, rewrite every absolute Mac path in its JSON files to
             a Windows path (PC app dir / PC 剪映 cache / a copy inside the job
             folder), tar the job folder;
  3. export  scp the tar, remote_render.ps1 -Mode export unpacks it, POSTs
             film-export and moves the mp4 to ``%USERPROFILE%/studio/outputs/
             <draft>/<name>``; the mp4 stays on the PC (that is where it gets
             published). Its moov box comes back so the Mac can verify it;
  4. fetch   only with ``--fetch``: scp the mp4 back to the ``-o`` path, ffprobe it;
  5. clean   always delete the PC job folder (never the output).

Secrets: the bridge token and the account context never leave the PC script;
the Mac only learns ``logged_in`` true/false. Only ssh/scp to the named host are
used — no other network traffic.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from . import jianying, jianying_cache
from .envelope import JyError

MAC_APP_RESOURCES = "/Applications/VideoFusion-macOS.app/Contents/Resources"
JOB_ROOT = "studio/jobs/jianying"          # relative to the PC user's home
OUTPUT_ROOT = "studio/outputs"             # relative to the PC user's home
PS_SCRIPT = Path(__file__).parent / "data" / "remote_render.ps1"
PACKAGE_DIRS = ("effect", "artistEffect")
SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=8",
    "-o", "LogLevel=ERROR",
)
_JOB_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_COPY_DIR_LIMIT = 512 * 1024 * 1024


# --------------------------------------------------------------------------- paths


def _norm(path):
    path = str(path)
    return path.rstrip("/") if len(path) > 1 else path


def _under(path, base):
    """Relative POSIX path of ``path`` under ``base`` ('' for base itself), else None."""
    path, base = _norm(path), _norm(base)
    if path == base:
        return ""
    if path.startswith(base + "/"):
        return path[len(base) + 1:]
    return None


def looks_like_path(value):
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and len(value) > 1
        and "\n" not in value
        and len(value) < 4096
    )


def collect_paths(node, found=None):
    """Every string value in a JSON document that is an absolute POSIX path."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for value in node.values():
            collect_paths(value, found)
    elif isinstance(node, list):
        for value in node:
            collect_paths(value, found)
    elif looks_like_path(node):
        found.add(_norm(node))
    return found


def classify(path, draft_root, *, cache_root, app_resources=MAC_APP_RESOURCES):
    """(kind, rel) for one Mac path.

    kinds: ``draft`` (inside the draft folder), ``draft_parent`` (an ancestor of
    it, e.g. draft_root_path), ``app`` (inside the 剪映 app's Resources),
    ``cache`` (inside 剪映's download cache), ``media`` (anything else).
    """
    roots = (draft_root,) if isinstance(draft_root, str) else tuple(draft_root)
    for root in roots:
        rel = _under(path, root)
        if rel is not None:
            return "draft", rel
    for root in roots:
        if _under(root, path) is not None:
            return "draft_parent", _under(root, path)
    rel = _under(path, app_resources)
    if rel is not None:
        return "app", rel
    rel = _under(path, cache_root)
    if rel is not None:
        return "cache", rel
    return "media", None


def package_key(cache_rel):
    """('effect/<id>', '<md5>', rest) for a downloaded effect package path, else None."""
    parts = cache_rel.split("/")
    if len(parts) >= 3 and parts[0] in PACKAGE_DIRS and parts[1].isdigit():
        return f"{parts[0]}/{parts[1]}", parts[2], "/".join(parts[3:])
    return None


def probe_request(paths, draft_root, *, cache_root, app_resources=MAC_APP_RESOURCES):
    """What the PC has to check before the Mac can choose a Windows path."""
    app_rels, cache_rels, keys = set(), set(), set()
    for path in paths:
        kind, rel = classify(path, draft_root, cache_root=cache_root, app_resources=app_resources)
        if kind == "app" and rel:
            app_rels.add(rel)
        elif kind == "cache" and rel:
            pkg = package_key(rel)
            if pkg:
                keys.add(pkg[0])
            else:
                cache_rels.add(rel)
    return {
        "app_rels": sorted(app_rels),
        "cache_rels": sorted(cache_rels),
        "package_keys": sorted(keys),
    }


def _safe_name(name, fallback="file"):
    stem = _SAFE.sub("_", name).strip("._") or fallback
    return stem[-80:]


def windows_name(name, fallback="draft"):
    """A file/folder name Windows accepts; keeps CJK, drops reserved characters."""
    cleaned = _WIN_BAD.sub("_", str(name)).strip(" .")
    return cleaned[:120] or fallback


def plan(paths, draft_root, probe, *, cache_root, app_resources=MAC_APP_RESOURCES,
         exists=os.path.exists, isdir=os.path.isdir):
    """Decide the Windows path for every Mac path.

    Returns ``{"mapping": {mac: pc}, "copies": [(src, job_rel)], "report": [...],
    "unresolved": [...], "installs": ["effect/<id>/<md5>"]}``. ``installs`` are effect
    packages the PC's 剪映 cache lacks; the PC script moves them from the job's
    ``install/`` folder into its cache (never overwriting). ``copies`` are local files/dirs that must travel in the
    job folder; job_rel is relative to the job folder. Pure given ``probe`` and the
    ``exists``/``isdir`` callables, so it is unit-testable without a PC.
    """
    job = probe["job_dir"].rstrip("/")
    pc_app = probe["app_resources"].rstrip("/")
    pc_cache = probe["cache_root"].rstrip("/")
    app_exists = probe.get("app_exists") or {}
    font_alt = probe.get("font_alt") or {}
    cache_exists = probe.get("cache_exists") or {}
    packages = probe.get("packages") or {}

    mapping, copies, report, unresolved, installs = {}, [], [], [], []
    copied = {}
    media_index = 0

    def copy(src, job_rel):
        if src not in copied:
            copies.append((src, job_rel))
            copied[src] = job_rel
        return f"{job}/{copied[src]}"

    for path in sorted(paths):
        kind, rel = classify(path, draft_root, cache_root=cache_root, app_resources=app_resources)
        target, how = None, None
        if kind == "draft":
            target, how = (f"{job}/draft/{rel}" if rel else f"{job}/draft"), "draft"
        elif kind == "draft_parent":
            target, how = job, "draft_parent"
        elif kind == "app":
            if app_exists.get(rel):
                target, how = f"{pc_app}/{rel}", "pc_app"
            elif font_alt.get(rel):
                target, how = font_alt[rel], "pc_app_font_by_name"
            elif exists(path):
                target, how = copy(path, f"res/{rel}"), "copied"
        elif kind == "cache":
            pkg = package_key(rel) if rel else None
            if pkg:
                key, md5, rest = pkg
                have = packages.get(key) or []
                if md5 in have:
                    target, how = f"{pc_cache}/{rel}", "pc_cache"
                elif have:
                    alt = sorted(have)[-1]
                    target = f"{pc_cache}/{key}/{alt}" + (f"/{rest}" if rest else "")
                    how = "pc_cache_other_version"
                elif exists(f"{_norm(cache_root)}/{key}/{md5}"):
                    # 剪映 only applies a package that sits in its own cache (a copy
                    # elsewhere renders as a silent no-op), so it is installed there.
                    pkg_src = f"{_norm(cache_root)}/{key}/{md5}"
                    if f"{key}/{md5}" not in installs:
                        installs.append(f"{key}/{md5}")
                        copy(pkg_src, f"install/{key}/{md5}")
                        stamp = f"{pkg_src}_modity_time.txt"
                        if exists(stamp):
                            copy(stamp, f"install/{key}/{md5}_modity_time.txt")
                    target, how = f"{pc_cache}/{rel}", "installed_in_pc_cache"
            elif rel and cache_exists.get(rel):
                target, how = f"{pc_cache}/{rel}", "pc_cache"
            elif rel and exists(path):
                target, how = copy(path, f"cache/{rel}"), "copied"
        else:
            if exists(path):
                if isdir(path) and _dir_size(path) > _COPY_DIR_LIMIT:
                    how = None
                else:
                    media_index += 1
                    name = f"{media_index:03d}_{_safe_name(os.path.basename(path))}"
                    target, how = copy(path, f"media/{name}"), "copied"
        if target is None:
            unresolved.append({"path": path, "kind": kind})
            continue
        mapping[path] = target
        report.append({"kind": kind, "how": how, "from": path, "to": target})
    return {"mapping": mapping, "copies": copies, "report": report,
            "unresolved": unresolved, "installs": installs}


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
            if total > _COPY_DIR_LIMIT:
                return total
    return total


def rewrite(node, mapping):
    """Return ``node`` with every mapped Mac path replaced; also inside longer strings."""
    if not mapping:
        return node
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(
        "(" + "|".join(re.escape(k) for k in keys) + r")(?=$|[/\"'\s,;)\]}\\])"
    )

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, str) and "/" in value:
            exact = mapping.get(_norm(value)) if looks_like_path(value) else None
            if exact is not None:
                return exact
            return pattern.sub(lambda m: mapping[m.group(1)], value)
        return value

    return fix(node)


def draft_json_files(draft_dir):
    """Plaintext JSON documents in a draft folder: [(path, parsed)]. Skips binaries/backups."""
    out = []
    for path in sorted(Path(draft_dir).rglob("*.json")):
        if ".bak" in path.name or path.name.startswith(".jy-") or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            continue
        raw = path.read_bytes()
        if not raw.lstrip()[:1] in (b"{", b"["):
            continue
        try:
            out.append((path, json.loads(raw.decode("utf-8"))))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return out


# --------------------------------------------------------------------------- mp4 check


def _boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        head = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            break
        yield kind.decode("latin-1"), pos + head, pos + size
        pos += size


def parse_moov(moov):
    """Duration / tracks / video size+codec from a raw moov box (bytes incl. header)."""
    info = {"duration_s": None, "tracks": []}
    top = list(_boxes(moov))
    if not top or top[0][0] != "moov":
        return info
    _, body, end = top[0]
    for kind, start, stop in _boxes(moov, body, end):
        if kind == "mvhd":
            version = moov[start]
            if version == 1:
                scale, duration = struct.unpack(">IQ", moov[start + 20:start + 32])
            else:
                scale, duration = struct.unpack(">II", moov[start + 12:start + 20])
            if scale:
                info["duration_s"] = round(duration / scale, 3)
        elif kind == "trak":
            track = {}
            for sub, s2, e2 in _boxes(moov, start, stop):
                if sub == "tkhd":
                    w, h = struct.unpack(">II", moov[e2 - 8:e2])
                    track["width"], track["height"] = w >> 16, h >> 16
                elif sub == "mdia":
                    for m, s3, e3 in _boxes(moov, s2, e2):
                        if m == "hdlr":
                            track["type"] = moov[s3 + 8:s3 + 12].decode("latin-1")
                        elif m == "minf":
                            for n, s4, e4 in _boxes(moov, s3, e3):
                                if n == "stbl":
                                    for o, s5, e5 in _boxes(moov, s4, e4):
                                        if o == "stsd" and e5 - s5 >= 16:
                                            track["codec"] = moov[s5 + 12:s5 + 16].decode("latin-1")
            info["tracks"].append(track)
    return info


def remote_output_info(export):
    """``output`` block for an mp4 left on the PC, verified from its moov box."""
    moov = base64.b64decode(export.get("moov_b64") or "") if export.get("moov_b64") else b""
    boxes = export.get("boxes") or []
    parsed = parse_moov(moov) if moov else {"duration_s": None, "tracks": []}
    video = next((t for t in parsed["tracks"] if t.get("type") == "vide"), {})
    audio = next((t for t in parsed["tracks"] if t.get("type") == "soun"), {})
    if not ({"ftyp", "moov", "mdat"} <= set(boxes)) or not video.get("width") or not parsed["duration_s"]:
        raise JyError(
            "INTERNAL",
            "PC 上的产出不是完整的 mp4（缺 ftyp/moov/mdat 或没有视频轨）。",
            details={"boxes": boxes, "parsed": parsed, "path": export.get("output_path")},
        )
    return {
        "path": export.get("output_path"),
        "size_bytes": export.get("bytes"),
        "duration_s": parsed["duration_s"],
        "codec": {"avc1": "h264", "avc3": "h264", "hvc1": "hevc", "hev1": "hevc"}.get(
            video.get("codec"), video.get("codec")),
        "width": video.get("width"),
        "height": video.get("height"),
        "audio_codec": {"mp4a": "aac"}.get(audio.get("codec"), audio.get("codec")),
        "verified_by": "mp4 box check on the PC copy (moov parsed on the Mac); --fetch adds ffprobe",
    }


# --------------------------------------------------------------------------- ssh plumbing


class Remote:
    def __init__(self, host, job_id=None):
        if not re.match(r"^[A-Za-z0-9._@-]+$", host or ""):
            raise JyError("VALIDATION", f"--remote 主机名不合法：{host!r}（填 ~/.ssh/config 里的别名，如 gamingpc）")
        self.host = host
        self.job = job_id or "jy-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        if not _JOB_RE.match(self.job):
            raise JyError("VALIDATION", f"bad job id {self.job!r}")
        self.job_rel = f"{JOB_ROOT}/{self.job}"
        self.created = False

    def _run(self, argv, timeout):
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise JyError(
                "UNAVAILABLE",
                f"远程命令超时（{timeout:.0f}s）：{' '.join(argv[:2])} …",
                retriable=True,
                details={"host": self.host, "stage": "ssh_timeout"},
            ) from exc
        except OSError as exc:
            raise JyError("UNAVAILABLE", f"没法运行 ssh/scp：{exc}") from exc
        return done

    def ssh(self, command, timeout=60.0):
        done = self._run(["ssh", *SSH_OPTS, self.host, command], timeout)
        if done.returncode == 255:
            raise self.unreachable(done)
        return done

    def unreachable(self, done):
        err = (done.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        return JyError(
            "UNAVAILABLE",
            f"连不上远程电脑 {self.host}（ssh 失败：{err or '无输出'}）。确认它开着机、在局域网里，"
            f"`ssh {self.host}` 能登录后重试；或去掉 --remote 在本机渲染。",
            retriable=True,
            details={"host": self.host, "stage": "ssh"},
        )

    def scp_to(self, files, timeout=1800.0):
        dest = f"{self.host}:{self.job_rel}/"
        done = self._run(["scp", "-q", *SSH_OPTS, *[str(f) for f in files], dest], timeout)
        if done.returncode != 0:
            if done.returncode == 255:
                raise self.unreachable(done)
            raise JyError(
                "UNAVAILABLE",
                f"把任务文件拷到 {self.host} 失败：{(done.stderr or b'').decode('utf-8', 'replace')[-300:]}",
                retriable=True,
                details={"host": self.host, "stage": "scp_to"},
            )

    def scp_from(self, remote_path, local_path, timeout=1800.0):
        done = self._run(["scp", "-q", *SSH_OPTS, f"{self.host}:{remote_path}", str(local_path)], timeout)
        if done.returncode != 0:
            raise JyError(
                "UNAVAILABLE",
                f"从 {self.host} 拷回成片失败：{(done.stderr or b'').decode('utf-8', 'replace')[-300:]}",
                retriable=True,
                details={"host": self.host, "stage": "scp_from", "remote_path": remote_path},
            )

    def mkdir(self):
        done = self.ssh(f"mkdir {self.job_rel.replace('/', chr(92))}")
        if done.returncode != 0:
            raise JyError(
                "UNAVAILABLE",
                f"在 {self.host} 上建任务目录失败：{(done.stderr or b'').decode('utf-8', 'replace')[-200:]}",
                details={"host": self.host, "stage": "mkdir"},
            )
        self.created = True

    def script(self, mode, *, launch=False, timeout=120.0):
        command = (
            "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
            f"{self.job_rel.replace('/', chr(92))}\\jy-remote.ps1 -Mode {mode} -Job {self.job}"
            + (" -Launch" if launch else "")
        )
        done = self.ssh(command, timeout=timeout)
        text = (done.stdout or b"").decode("utf-8", "replace")
        for line in reversed(text.splitlines()):
            line = line.strip().lstrip("﻿")
            if line.startswith("JYREMOTE "):
                return json.loads(line[len("JYREMOTE "):])
        raise JyError(
            "INTERNAL",
            f"远程脚本没有返回结果（exit {done.returncode}）。",
            details={
                "host": self.host,
                "stage": f"script_{mode}",
                "stdout_tail": text[-600:],
                "stderr_tail": (done.stderr or b"").decode("utf-8", "replace")[-600:],
            },
        )

    def cleanup(self):
        """Delete the PC job folder (literal, validated id). Never touches outputs."""
        if not self.created:
            return {"removed": False, "reason": "job dir never created"}
        try:
            done = self.ssh(f"rmdir /s /q {self.job_rel.replace('/', chr(92))}", timeout=60.0)
        except JyError as exc:
            return {"removed": False, "error": exc.message, "job_dir": self.job_rel}
        return {"removed": done.returncode == 0, "job_dir": self.job_rel}


def _with_bom(src, dest):
    data = Path(src).read_bytes()
    if not data.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data
    Path(dest).write_bytes(data)


def _probe_errors(remote, probe, *, launch):
    """Turn a failed/unready probe into the actionable jy error."""
    err = probe.get("error")
    if err == "game_mode":
        return JyError(
            "BLOCKED",
            f"{remote.host} 正在打游戏（游戏模式开着：{probe.get('game_mode')}）。游戏优先，"
            "这次不远程渲染：等他打完再试，或去掉 --remote 在本机渲染。",
            details={"host": remote.host, "stage": "game_mode"},
        )
    if err == "jianying_not_installed":
        return JyError("UNAVAILABLE", f"{remote.host} 上没找到剪映专业版（%LOCALAPPDATA%\\JianyingPro\\Apps\\<版本>）。",
                       details={"host": remote.host})
    if err == "run_gui_missing":
        return JyError("UNAVAILABLE", f"{remote.host} 上缺 pcwork\\run-gui.ps1，没法在桌面会话里启动剪映。",
                       details={"host": remote.host})
    if err:
        return JyError("INTERNAL", f"远程体检失败：{err}", details={"host": remote.host, "probe": probe})
    bridge = probe.get("bridge") or {}
    if bridge.get("state") == "not_running":
        if launch and probe.get("launched"):
            msg = f"已在 {remote.host} 上启动剪映，但 {probe['launched'].get('waited_ms')}ms 内 bridge 没就绪；稍等再试。"
        else:
            msg = (f"{remote.host} 上的剪映没有运行。让他打开剪映，或加 --relaunch-app 让 jy 在他的桌面会话里"
                   "启动剪映（会弹出剪映窗口）。")
        return JyError("UNAVAILABLE", msg, retriable=True,
                       details={"host": remote.host, "bridge": bridge, "hint": "--relaunch-app"})
    if bridge.get("state") == "wedged":
        return JyError(
            "UNAVAILABLE",
            f"{remote.host} 上的剪映进程在，但 native bridge 不响应（多半卡死）。请他手动重启剪映后再试；"
            "jy 不会远程杀掉他桌面上的剪映。",
            retriable=True,
            details={"host": remote.host, "bridge": bridge},
        )
    if probe.get("logged_in") is False:
        return JyError(
            "UNAVAILABLE",
            f"{remote.host} 上的剪映没登录（拿不到 accountScope）。请他在 PC 剪映里扫码登录后重试。",
            retriable=True,
            details={"host": remote.host, "authenticated": False},
        )
    return None


def _tar(job_stage, tar_path):
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for entry in sorted(Path(job_stage).iterdir()):
            tar.add(str(entry), arcname=entry.name)


def render_remote(ctx, args, *, tool, resolved, summary, sha256, target, bridge_error):
    """The whole remote round trip. Returns (outcome, data) like the local render."""
    root = Path(resolved["draft_dir"]).resolve()
    roots = tuple(dict.fromkeys([str(root), _norm(os.path.abspath(str(resolved["draft_dir"])))]))
    host = args.remote
    timeout_ms = int(getattr(args, "timeout_ms", jianying.DEFAULT_TIMEOUT_MS))
    launch = bool(getattr(args, "relaunch_app", False))
    fetch = bool(getattr(args, "fetch", False))
    cache_root = str(jianying_cache.cache_root())

    if summary.get("draft_encrypted"):
        raise JyError(
            "UNSUPPORTED",
            "这个草稿的 draft_info.json 已被剪映加密（在剪映里打开保存过），jy 没法把里面的 Mac 路径改成 "
            "Windows 路径，所以不能远程渲染。用 jy draft build 生成的明文草稿，或去掉 --remote 在本机渲染。",
            details={"draft_path": str(root)},
        )

    documents = draft_json_files(root)
    paths = set()
    for _path, doc in documents:
        collect_paths(doc, paths)

    draft_name = windows_name(root.name)
    out_name = windows_name(Path(args.output).name, "out.mp4")
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    output_rel = f"{OUTPUT_ROOT}/{draft_name}/{out_name}"

    remote = Remote(host)
    stages = []
    started = time.monotonic()

    def stage(name, t0):
        stages.append({"stage": name, "elapsed_ms": int(round((time.monotonic() - t0) * 1000))})

    cleanup = None
    local_tmp = None
    try:
        with tempfile.TemporaryDirectory(prefix="jy-remote-") as tmp:
            tmp = Path(tmp)
            # 1. probe
            t0 = time.monotonic()
            remote.mkdir()
            request = probe_request(paths, roots, cache_root=cache_root)
            request["output_rel"] = output_rel
            (tmp / "probe.json").write_text(json.dumps(request, ensure_ascii=False), "utf-8")
            _with_bom(PS_SCRIPT, tmp / "jy-remote.ps1")
            remote.scp_to([tmp / "jy-remote.ps1", tmp / "probe.json"])
            probe = remote.script("probe", launch=launch, timeout=180.0 if launch else 150.0)
            stage("probe", t0)
            problem = _probe_errors(remote, probe, launch=launch)
            if problem:
                raise problem
            if probe.get("output_exists") and not args.force:
                raise JyError(
                    "VALIDATION",
                    f"{host} 上已有同名成片：~/{output_rel}（加 --force 覆盖，或换 -o 文件名）",
                    details={"host": host, "remote_output": output_rel},
                )
            if launch and probe.get("launched"):
                ctx.warnings.append(
                    f"--relaunch-app：已在 {host} 的桌面会话里启动剪映（等了 {probe['launched'].get('waited_ms')}ms）。"
                )

            preflight = {
                "remote": host,
                "app_version": probe.get("app_version"),
                "bridge_state": probe.get("bridge"),
                "logged_in": probe.get("logged_in"),
                "launched": probe.get("launched"),
            }
            if getattr(args, "preflight", False):
                return "readonly", {
                    "engine": "jianying",
                    "remote": {"host": host, "job_dir": probe.get("job_dir"),
                               "output_path": f"{probe.get('home')}/{output_rel}"},
                    "draft": {**summary, "sha256": sha256},
                    "preflight": preflight,
                    "bridge_state": probe.get("bridge"),
                    "paths": (pre := plan(paths, roots, probe, cache_root=cache_root))["report"],
                    "unresolved": pre["unresolved"],
                    "packages_to_install": pre["installs"],
                    "note": "--remote --preflight：只读体检（PC 剪映状态 + 草稿路径会怎么改写），不导出。",
                }

            # 2. pack
            t0 = time.monotonic()
            decided = plan(paths, roots, probe, cache_root=cache_root)
            stage_dir = tmp / "job"
            stage_dir.mkdir()
            shutil.copytree(root, stage_dir / "draft", ignore=shutil.ignore_patterns("*.bak*", ".jy-*"))
            for path, doc in draft_json_files(stage_dir / "draft"):
                fixed = rewrite(doc, decided["mapping"])
                if fixed != doc:
                    path.write_text(json.dumps(fixed, ensure_ascii=False), "utf-8")
            for src, job_rel in decided["copies"]:
                dest = stage_dir / job_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if os.path.isdir(src):
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)
            for item in decided["unresolved"]:
                ctx.warnings.append(f"草稿引用的路径在 Mac 上不存在，原样保留（PC 上会缺）：{item['path']}")
            if any(r["how"] == "pc_cache_other_version" for r in decided["report"]):
                ctx.warnings.append("部分素材包在 PC 剪映缓存里是另一个版本（md5 不同），已指向 PC 自己的版本。")
            tar_path = tmp / "pkg.tar"
            _tar(stage_dir, tar_path)
            export_req = {"timeout_ms": timeout_ms, "output_rel": output_rel, "force": bool(args.force),
                          "installs": decided["installs"]}
            (tmp / "export.json").write_text(json.dumps(export_req, ensure_ascii=False), "utf-8")
            stage("pack", t0)

            t0 = time.monotonic()
            remote.scp_to([tar_path, tmp / "export.json"])
            stage("upload", t0)

            # 3. export (one retry: cold-start window_close_timeout or busy)
            attempts = []
            export = None
            for number in (1, 2):
                t0 = time.monotonic()
                export = remote.script("export", timeout=timeout_ms / 1000.0 + 120.0)
                stage(f"export_{number}", t0)
                entry = {"attempt": number, "ok": bool(export.get("ok")),
                         "elapsed_ms": export.get("elapsed_ms"), "status": export.get("status")}
                attempts.append(entry)
                if export.get("ok"):
                    break
                err = _export_error(remote, export, bridge_error)
                entry["error"] = err.code + ": " + err.message[:160]
                native = (err.details or {}).get("native_error")
                cold = (export.get("uptime_s") or 10**9) < 180
                retry = (native == "native_export_busy") or (
                    native == "native_export_window_close_timeout" and cold)
                if number == 1 and retry:
                    ctx.warnings.append(f"{host} 上剪映第一次导出报 {native}；按冷启动/忙碌规则重试一次。")
                    time.sleep(2.0)
                    continue
                raise err
            output = remote_output_info(export)

            # 4. optional fetch back
            fetched = None
            if fetch:
                t0 = time.monotonic()
                from .commands import _checked_output
                from . import ffmpeg

                target = _checked_output(args.output, args.force)
                handle, local_tmp = tempfile.mkstemp(prefix=".jy-remote-", suffix=".mp4", dir=str(target.parent))
                os.close(handle)
                remote.scp_from(export["output_path"], local_tmp)
                info = ffmpeg.output_info(tool, local_tmp)
                if not info.get("codec") or not info.get("width"):
                    raise JyError("INTERNAL", "拷回的成片不是可播放的视频（ffprobe 没有视频流）。",
                                  details={"output": info})
                os.replace(local_tmp, target)
                local_tmp = None
                info["path"] = str(target)
                fetched = info
                stage("fetch", t0)
    finally:
        if local_tmp:
            Path(local_tmp).unlink(missing_ok=True)
        cleanup = remote.cleanup()

    if not cleanup.get("removed"):
        ctx.warnings.append(f"{host} 上的任务目录没删掉：~/{remote.job_rel}（{cleanup.get('error', '')}）")
    receipt = {}
    try:
        body = json.loads(export.get("body") or "{}")
        receipt = {k: body.get(k) for k in ("format", "requestId", "snapshotId", "revision") if body.get(k)}
    except (TypeError, json.JSONDecodeError):
        pass
    receipt["durationMs"] = export.get("elapsed_ms")
    copied = [r for r in decided["report"] if r["how"] in ("copied", "installed_in_pc_cache")]
    data = {
        "engine": "jianying",
        "draft": {**summary, "sha256": sha256, "layout": resolved["layout"],
                  "timeline_id": resolved["timeline_id"]},
        "output": fetched or output,
        "remote": {
            "host": host,
            "app_version": probe.get("app_version"),
            "output_path": export.get("output_path"),
            "output": output,
            "fetched": bool(fetched),
            "fetched_path": fetched["path"] if fetched else None,
            "job_dir": probe.get("job_dir"),
            "draft_sha256_on_pc": export.get("draft_sha256"),
            "paths_rewritten": len(decided["mapping"]),
            "files_copied": len(copied),
            "packages_installed": export.get("installed") or [],
            "path_report": decided["report"],
            "unresolved": decided["unresolved"],
            "stages": stages,
            "elapsed_ms": int(round((time.monotonic() - started) * 1000)),
        },
        "bridge": {"host": host, **(probe.get("bridge") or {})},
        "receipt": receipt,
        "preflight": preflight,
        "attempts": attempts,
        "focus_guard": {"mode": "remote", "note": "渲染在远程 PC 上，Mac 的前台不受影响"},
        "lock": None,
        "cold_start": {"detected": (probe.get("bridge") or {}).get("uptime_s", 10**9) < 180,
                       "app_uptime_s": (probe.get("bridge") or {}).get("uptime_s")},
        "warmup": {"performed": False, "reason": "remote"},
        "relaunch": probe.get("launched"),
        "cleanup": {"remote_job": cleanup},
    }
    return "executed", data


def _export_error(remote, export, bridge_error):
    """Failed export -> the same jy errors a local render raises."""
    err = export.get("error")
    if err == "game_mode":
        return _probe_errors(remote, export, launch=False)
    if err == "not_logged_in":
        return _probe_errors(remote, {"bridge": {"state": "ok"}, "logged_in": False}, launch=False)
    if err and err.startswith("bridge_"):
        return _probe_errors(remote, {"bridge": export.get("bridge") or {"state": err[7:]}}, launch=False)
    if err == "output_exists":
        return JyError("VALIDATION", f"{remote.host} 上已有同名成片：{export.get('output_path')}（加 --force 覆盖）")
    if err:
        return JyError("INTERNAL", f"远程导出失败：{err}", details={"host": remote.host, "export": export})
    if export.get("status") == 0:
        kind = export.get("kind") or "transport"
        return bridge_error(jianying.BridgeError(
            f"PC bridge {kind}: {export.get('transport_error')}", kind=kind, retriable=True))
    try:
        body = json.loads(export.get("body") or "null")
    except json.JSONDecodeError:
        body = export.get("body")
    if export.get("status") == 200:
        return JyError("INTERNAL", "PC 剪映回执成功，但没有产出文件。",
                       details={"host": remote.host, "native_body": body})
    return bridge_error(jianying.BridgeError(
        "PC 剪映拒绝了导出请求", status=export.get("status"), body=body, kind="http"))
