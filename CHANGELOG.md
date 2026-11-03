# Changelog

## Unreleased

- **Drafts now show up in 剪映's home list.** `jy draft build -o <draft-root>/name` registers the draft in
  `root_meta_info.json` automatically (`--no-register` opts out), and two new verbs cover the other cases:
  `jy draft register DIR` (folders already inside the root) and `jy draft import DIR` (copy a project-folder
  draft into the root, rewrite its recorded paths, then register). Dry-run by default, two verified backups,
  atomic replace, and a re-merge/abort if 剪映 rewrote the index between read and write.
- Draft folders now get a `draft_meta_info.json` (52-key shape copied from what 剪映 itself writes) and a
  `draft_cover.jpg` thumbnail extracted from the first clip — 剪映's draft-store loader skips any folder
  without the former.
- Index contract established statically against 剪映 11.6.13358 / 11.7.0-beta6: `draft_ids` is a persisted
  watermark (`len+1`, never `len`), only two entry-rejection rules exist, and the app rewrites the index from
  memory on save/close/quit — so the reliable order is quit 剪映 → register → open 剪映.
- `jy material register` is now an alias of `jy draft register`.
- **`jy update status|lock|unlock`**: stop 剪映's bundled Sparkle auto-updater without patching the
  app — turn `enableAutoUpdate` off, move any staged installer (never delete) into a backup folder,
  terminate only 剪映's own update helpers by path, and mark the installed bundle +
  Sparkle cache `UF_IMMUTABLE` with a manifest so `unlock` restores the original flags.
  Dry-run by default; no daemon, no network rule, no draft touched.

## 0.3.0 — 2026-10-25

First public release (tag `v0.3.0`; earlier 0.1 and 0.2 builds were private).

- `jy` CLI for editing and rendering video with 剪映专业版 from the command line: the agent writes a draft and 剪映's own native engine renders it, no GUI clicks.
- 39 commands (56 with subcommands): probe, trim, concat, speed, reverse, montage and beat cut, subtitles, ASS text animations, 50+ transitions, filters and LUTs, curves/HSL, chroma key, picture-in-picture, audio mix, ducking and loudnorm, local ASR, covers, GIF, batch export and background jobs.
- Two engines: `jianying` (renders drafts through the installed 剪映 app) and `local` (ffmpeg, offline, deterministic).
- Live material catalog: `jy effects search-remote` searches 剪映's full online catalog of transitions, filters, effects, 花字, text animations and stickers; `jy effects fetch` downloads packages into 剪映's cache (md5-checked, atomic); `jy draft build --fetch` resolves specs by name.
- Chinese 剪映 names such as `--type 叠化` and `--preset 日系` resolve to local implementations with a `fidelity` grade (`exact | close | rough`).
- One `jy.v1` JSON object on stdout for every command; outputs are re-probed with ffprobe so `ok:true` means a real, playable file.
- Bundled agent skill (`skills/jianying-native`), zero Python dependencies, `tests/smoke.sh` (260 checks) in CI on macOS, and a rendered hero demo (`docs/render_demo.py`).
