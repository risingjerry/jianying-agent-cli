"""剪映-engine verbs: ``draft build`` and ``render`` (sync or ``--async``)."""

from __future__ import annotations

import argparse

from .. import jianying_commands


def register(r):
    render = r.parser_for(
        "render",
        parents=[r.output_parent, r.async_parent],
        help="render a 剪映 draft folder to MP4 via the running 剪映 (native bridge)",
        description="用剪映自己的原生 film-export 渲染草稿目录：只需要剪映在运行，"
        "不需要点任何界面。默认带焦点守卫（渲染期间把前台应用还给用户）；"
        "传输失败会重新发现 bridge 并重试一次；--relaunch-app 才允许拉起剪映。",
    )
    render.add_argument(
        "draft", metavar="draft-dir", help="含 draft_info.json 的草稿目录"
    )
    render.add_argument(
        "--engine",
        choices=("jianying", "local"),
        default="jianying",
        help="jianying=剪映原生引擎（默认）；local 会明确报错并指向 ffmpeg 动词",
    )
    render.add_argument(
        "--timeout-ms",
        type=int,
        default=600_000,
        metavar="MS",
        help="交给剪映的导出超时（默认 600000 = 10 分钟）",
    )
    render.add_argument(
        "--focus-guard",
        choices=("off", "restore", "watch"),
        default=argparse.SUPPRESS,
        help="watch (default) = re-activate the previous app every interval during the "
        "export, then restore; restore = touch focus only once, after the export; "
        "off = never touch focus. A/B on a warm 剪映: watch and off both render fine.",
    )
    render.add_argument(
        "--no-focus-guard",
        dest="focus_mode",
        action="store_const",
        const="off",
        default=argparse.SUPPRESS,
        help="alias for --focus-guard off",
    )
    render.add_argument(
        "--focus-interval-ms",
        type=int,
        default=200,
        metavar="MS",
        help="poll interval for --focus-guard watch (default 200)",
    )
    render.add_argument(
        "--relaunch-app",
        action="store_true",
        help="if the bridge is gone, relaunch 剪映 once and retry (default off: never "
        "launches apps behind your back)",
    )
    render.add_argument(
        "--relaunch-wait-ms",
        type=int,
        default=45_000,
        metavar="MS",
        help="how long to wait for the bridge after --relaunch-app (default 45000)",
    )
    render.add_argument(
        "--warmup",
        action="store_true",
        help="pay the known first-export cost on a throwaway 1 s draft before the real "
        "export (opt-in; runs after a relaunch / cold start / slow health)",
    )
    render.add_argument(
        "--warmup-timeout-ms",
        type=int,
        default=200_000,
        metavar="MS",
        help="bound for the warmup export (default 200000)",
    )
    render.add_argument(
        "--health-timeout-ms",
        type=int,
        default=5000,
        metavar="MS",
        help="bridge health probe budget before giving up (default 5000; keep <= 5000)",
    )
    render.add_argument(
        "--preflight",
        action="store_true",
        help="read-only health/route/focus check; never starts an export",
    )
    render.add_argument(
        "--cold-start-window-ms",
        type=int,
        default=180_000,
        metavar="MS",
        help="treat 剪映 as freshly launched when its process is younger than this; "
        "the first export then gets one automatic retry (default 180000). 0 disables",
    )
    render.add_argument(
        "--lock-wait-ms",
        type=int,
        default=30_000,
        metavar="MS",
        help="wait this long for the single-instance export window (default 30000)",
    )
    render.add_argument(
        "--on-entitlement",
        choices=("fail", "local"),
        default="fail",
        help="when 剪映 refuses an item for membership reasons: fail (default) or "
        "re-render the same timeline with the local ffmpeg approximation (explicitly "
        "reported in data.degraded_items)",
    )
    render.add_argument(
        "--keep-draft",
        action="store_true",
        help="do not delete a jy-authored scratch draft after a successful render",
    )
    render.add_argument(
        "--remote",
        metavar="SSH_HOST",
        default=None,
        help="render on a Windows PC's 剪映 over ssh (e.g. gamingpc): packs the draft + media, "
        "rewrites Mac paths, exports there; the mp4 stays at %%USERPROFILE%%\\studio\\outputs\\"
        "<draft>\\<-o name>. --relaunch-app lets it start 剪映 on the PC desktop",
    )
    render.add_argument(
        "--fetch",
        action="store_true",
        help="with --remote: also copy the mp4 back to -o and ffprobe it",
    )
    render.set_defaults(focus_mode="watch")
    r.bind(render, "render", jianying_commands.render)
