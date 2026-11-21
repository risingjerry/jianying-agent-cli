# Agent instructions (jianying-native / `jy`)

`jy` is a headless video-editing CLI. Its contract is `jy.v1`, documented in `USAGE.md`:
**every command prints exactly one JSON object on stdout**, `ok` is the single truth of success,
ffmpeg logs go to stderr only. A portable Agent Skill lives in `skills/jianying-native/SKILL.md`.

## Install and test

- Install: `python3 install_global.py` (symlinks `bin/jy` into `~/.local/bin`; add `--with-skill`
  to also copy the skill into `~/.claude/skills` and `~/.agents/skills`). Requires Python 3.9+ and ffmpeg.
- Test: `./tests/smoke.sh` (synthetic media in a temp dir; no network, no GUI).
- Ground truth for flags: `jy <verb> --help`; for the environment: `jy doctor`.

## Two engines, one CLI

- **local** — ffmpeg (prefers Homebrew's `ffmpeg-full` for libass/libx264/libx265,
  falls back to any `ffmpeg` on PATH and degrades honestly per capability). Fully offline,
  headless, no GUI, no membership quota.
- **jianying** — drives the user's installed 剪映专业版 (`/Applications/VideoFusion-macOS.app`)
  through its own local native bridge to render drafts with 剪映's real engine. Requires 剪映 to be
  running. It does **not** click the UI, does not move the mouse pointer and does not open a window
  for the export path.

`jy render --remote HOST` sends a plaintext draft to 剪映 on a Windows PC over ssh/scp (see USAGE「远程渲染」).
It rewrites Mac paths in a *copy* of the draft, may install missing effect packages into the PC's 剪映 cache (never
overwriting), keeps the mp4 on the PC unless `--fetch`, and always deletes the PC job folder. Path-rewrite tests:
`python3 -m unittest discover -s tests -p 'test_remote.py'` (also run by smoke.sh).

`jy doctor` reports both engines and their capability matrix. Pick `--engine` explicitly when it
matters; never assume the 剪映 engine is available.

## Safety and privacy rules

- **Never modify the user's real 剪映 drafts.** Author into a temp/scratch directory, or copy a draft
  and work on the copy. Reading them is fine; writing is not.
- The native bridge descriptor (`$TMPDIR/infinite-canvas-native-bridge.json`) carries a per-launch
  token, and `/custom-agent/context` carries the account login cookie. **Never write either to disk,
  logs, or evidence**, never put them in argv, and never send them to a non-local host.
- `jy` itself makes no outbound network requests. 剪映 may contact its own servers for material
  downloads and AI features — that traffic is the app's, not the CLI's.
- Media paths, draft names and rendered output are user content. Do not commit them.

## Effect semantics

- Write commands refuse to overwrite an existing output unless `--force`.
- `ok:true` only means a real, playable file exists: every executed verb re-probes its output.
- Entitlement errors from 剪映 (`film_export_entitlement_denied`, `bypass_forbidden`) mean the
  material or export needs a membership quota. Surface them as `BLOCKED` with an actionable message;
  never retry, never silently downgrade to a cheaper path.
- Missing resources (`film_export_dependency_missing`) mean the effect package is not cached locally.
  Surface as `UNSUPPORTED` naming the resource; do not fall back to a visually different effect.
- The offline effects (`filter`, `transition`, `beautify`, `key`, `text`) are **approximations**.
  They are not the 剪映 material library. `beautify` is a global look, not face-aware. Say so when
  reporting results.

## Rendering unattended

- 剪映's export job may raise its window. `render` uses a focus guard: it records the foreground app,
  returns focus to it during the render, and never moves the pointer. Report the residual behaviour
  from the envelope's `data.focus_guard` rather than claiming zero interference.
- Prefer `--async` plus `jy jobs wait` for long renders so an agent turn is not blocked.
- 剪映 can crash. The CLI re-discovers the descriptor on failure and returns an actionable error; it
  only relaunches the app when explicitly asked (`--relaunch-app`).

## Testing

- `./tests/smoke.sh` must stay green. The 剪映-engine section **skips** with a clear message when the
  app is not running — a skipped section is not a pass, and it is never a reason to fake output.
- Do not add network calls, GUI clicks, or pip dependencies to make a test pass.
