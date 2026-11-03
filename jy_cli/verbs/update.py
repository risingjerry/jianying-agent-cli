"""``jy update``: keep 剪映's bundled Sparkle updater from replacing the app.

剪映 updates itself on its own schedule; a downloaded update can be installed the next
time the app quits. The switch in 剪映's own settings is not sticky in every case, so this
verb offers a reversible, inspectable alternative (see :mod:`jy_cli.update_guard`):
disable the toggle, quarantine staged installers, and mark the installed bundle immutable.
"""

from __future__ import annotations

from .. import update_guard


def update_status(ctx, args):
    return "readonly", update_guard.status()


def update_lock(ctx, args):
    data = update_guard.lock(apply=bool(args.apply), force=bool(args.force))
    if data.get("applied") and data.get("after", {}).get("pending_files"):
        ctx.warnings.append("还有待安装的更新文件没被隔离：看 data.actions 里的 quarantine.failed")
    return ("executed" if data["applied"] else "preview"), data


def update_unlock(ctx, args):
    data = update_guard.unlock(
        apply=bool(args.apply), restore_config=bool(args.restore_config)
    )
    return ("executed" if data["applied"] else "preview"), data


def register(r):
    group = r.parser_for(
        "update",
        help="block / inspect 剪映's own auto-updater (reversible, no binary patch)",
        description="剪映自带 Sparkle 自动更新：更新包可能在退出时被装上。这里提供可逆的做法——"
        "关掉 globalSetting 的 enableAutoUpdate、把已下载的更新包移到备份目录、给 App 和更新缓存加 "
        "UF_IMMUTABLE。默认 dry-run，`--apply` 才动；`unlock` 按 manifest 还原原始 flags。",
        group=True,
    )
    sub = group.add_subparsers(
        dest="update_action", required=True, parser_class=r.parser_class, metavar="action"
    )

    status = sub.add_parser(
        "status",
        parents=[r.verbose_parent],
        help="auto-update switch, immutable lock state, staged installers, update helpers",
    )
    r.bind(status, "update status", update_status)

    lock = sub.add_parser(
        "lock",
        parents=[r.verbose_parent],
        help="disable auto-update, quarantine staged installers, freeze the bundle (dry-run unless --apply)",
    )
    lock.add_argument("--apply", action="store_true", help="真正执行（先备份 globalSetting）")
    lock.add_argument(
        "--force",
        action="store_true",
        help="即使剪映的更新助手还在运行也继续（只终止这些助手，不动主程序）",
    )
    r.bind(lock, "update lock", update_lock)

    unlock = sub.add_parser(
        "unlock",
        parents=[r.verbose_parent],
        help="restore the flags recorded at lock time (dry-run unless --apply)",
    )
    unlock.add_argument("--apply", action="store_true")
    unlock.add_argument(
        "--restore-config",
        action="store_true",
        help="同时把 globalSetting 还原成加锁前那份（会重新打开 enableAutoUpdate）",
    )
    r.bind(unlock, "update unlock", update_unlock)
