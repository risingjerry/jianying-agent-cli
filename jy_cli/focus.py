"""Keep jy from stealing focus on macOS — without breaking 剪映's export window.

The 剪映 native render raises its own window: ``jy render`` on an unattended
machine therefore yanks the user out of whatever app they were using (verified:
frontmost becomes ``com.lemon.lvpro`` even when 剪映 was hidden first).

There is no known "do not activate" flag on the film-export bridge route, so this
module offers two mechanisms with very different side effects:

  ``restore`` (default)
      Record the frontmost app before the render and re-activate it **once**, after
      剪映's response arrives. No polling, no reactivation while the export runs,
      so 剪映 is free to close its own export window.

  ``watch`` (default)
      Every ``interval`` seconds *during* the render, if 剪映 has taken the front,
      put the user's app back; if the user switched to some other app themselves,
      follow them (that app becomes the one to restore) instead of yanking them back. Keeps the user's app in front instead of leaking 剪映's window
      for the whole export. A controlled matrix on a warm 剪映 (same draft, guard
      off → ok 3s, guard on → ok 3s, guard off → ok 2s) cleared an earlier suspicion
      that this polling wedges the export window: the real first-render failure was
      a cold-start effect, not the guard.

Implementation notes: ``lsappinfo`` (CoreServices) reads the front app;
``open -b <bundle-id>`` re-activates it. Both are built-in, need no
Accessibility/Automation permission, and never touch the mouse pointer. If the
front app cannot be read, or it *is* 剪映 itself, the guard disables itself and
says so instead of fighting the renderer.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

LSAPPINFO = "/usr/bin/lsappinfo"
OPEN = "/usr/bin/open"

JIANYING_BUNDLE = "com.lemon.lvpro"

MODES = ("off", "restore", "watch")

RESIDUAL_NOTE = (
    "剪映 会占住前台直到导出结束（导出期间反复把它拉下去会让它关不掉自己的导出窗口，"
    "实测会变成 native_export_window_close_timeout）；jy 在收到结果后把原来的前台应用"
    "拉回来一次。鼠标指针从不移动，也不点任何界面。"
)
WATCH_WARNING = (
    "watch 模式下 jy 只在剪映抢到前台时把用户的应用拉回来；用户自己切到别的应用时跟着用户走，不会拽回去；"
    "A/B（同一草稿、热启动剪映）证明这不会影响导出：guard on/off 都在 2-3s 成功。"
    "真正的首次失败是剪映冷启动问题（jy 会在冷启动时自动重试一次，见 render）。"
)


def _run(argv, timeout=3.0):
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_info(text):
    fields = {}
    for line in (text or "").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        fields[key.strip().strip('"')] = value.strip().strip('"')
    return fields


def frontmost():
    """``{"asn","name","bundle_id"}`` for the front app, or None if unreadable."""
    if not os.access(LSAPPINFO, os.X_OK):
        return None
    front = _run([LSAPPINFO, "front"])
    if front is None or front.returncode != 0:
        return None
    asn = (front.stdout or "").strip()
    if not asn.startswith("ASN:"):
        return None
    info = _run([LSAPPINFO, "info", "-only", "name,bundleID", asn])
    fields = _parse_info(info.stdout if info else "")
    return {
        "asn": asn,
        "name": fields.get("LSDisplayName"),
        "bundle_id": fields.get("CFBundleIdentifier"),
    }


def running(bundle_id):
    """True when an app with this bundle id is currently running."""
    if not bundle_id:
        return False
    listed = _run([LSAPPINFO, "list"])
    return bool(listed and listed.returncode == 0 and bundle_id in (listed.stdout or ""))


def activate(app):
    """Bring ``app`` back to the front without launching anything new."""
    bundle_id = (app or {}).get("bundle_id")
    if not bundle_id or not running(bundle_id):
        return False
    opened = _run([OPEN, "-b", bundle_id])
    return bool(opened and opened.returncode == 0)


def state():
    """Read-only focus state for ``jy doctor`` (never raises)."""
    current = frontmost()
    return {
        "provider": "lsappinfo/open",
        "available": current is not None,
        "frontmost": current,
        "modes": list(MODES),
        "default_mode": "watch",
        "guard_residual": RESIDUAL_NOTE,
        "watch_warning": WATCH_WARNING,
    }


class FocusGuard:
    """Background re-activator: keeps the pre-render app frontmost."""

    def __init__(self, interval=0.2, enabled=True, mode="restore"):
        self.interval = max(0.05, float(interval))
        self.mode = mode if mode in MODES else "restore"
        if not enabled:
            self.mode = "off"
        self.enabled = self.mode != "off"
        self.target = None
        self.avoid = None
        self.reason = None
        self.checks = 0
        self.reactivations = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self, avoid_bundle=None):
        if self.mode == "off":
            self.reason = "disabled by --focus-guard off / --no-focus-guard"
            return self
        target = frontmost()
        if target is None:
            self.reason = "frontmost app unreadable (lsappinfo unavailable)"
            return self
        if avoid_bundle and target.get("bundle_id") == avoid_bundle:
            self.target = target
            self.reason = (
                "the previously frontmost app is the renderer itself; nothing to restore"
            )
            return self
        self.target = target
        self.avoid = avoid_bundle
        if self.mode == "watch":
            self._thread = threading.Thread(
                target=self._loop, name="jy-focus-guard", daemon=True
            )
            self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.checks += 1
            current = frontmost()
            if current is None:
                continue
            if current.get("asn") == self.target.get("asn"):
                continue
            if self.avoid and current.get("bundle_id") != self.avoid:
                # The user switched apps on purpose: follow them, never pull them back.
                self.target = current
                continue
            if activate(self.target):
                self.reactivations += 1
            elif not running(self.target.get("bundle_id")):
                self.reason = "the recorded app quit during the render; guard stopped"
                return

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        restored = None
        if self.target and self.reason is None:
            current = frontmost()
            if (current and current.get("asn") != self.target.get("asn")
                    and (not self.avoid or current.get("bundle_id") == self.avoid)):
                activate(self.target)
                current = frontmost()
            restored = bool(current and current.get("asn") == self.target.get("asn"))
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "available": bool(self.target) and self.reason is None,
            "interval_ms": (
                int(round(self.interval * 1000)) if self.mode == "watch" else 0
            ),
            "previous_app": (
                {"name": self.target.get("name"), "bundle_id": self.target.get("bundle_id")}
                if self.target
                else None
            ),
            "checks": self.checks,
            "reactivations": self.reactivations,
            "restored": restored,
            "note": WATCH_WARNING if self.mode == "watch" else RESIDUAL_NOTE,
            "reason": self.reason,
        }
