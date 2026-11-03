<div align="center">

# jianying-agent-cli (`jy`)

**Let AI agents edit videos with 剪映专业版 (JianyingPro / CapCut China desktop) — the agent writes the draft, 剪映's own engine renders it, no GUI clicks. Materials are searched and downloaded by name on demand; ffmpeg handles preprocessing.**

Cut, concat, subtitles, titles, transitions, filters, keying, audio, keyframes, batch export — every call prints exactly one JSON object, so Claude Code, Codex or any script can drive it without a GUI.

[![test](https://github.com/risingjerry/jianying-agent-cli/actions/workflows/test.yml/badge.svg)](https://github.com/risingjerry/jianying-agent-cli/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-D97757.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-6A9BCC.svg)
![macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)
![No pip deps](https://img.shields.io/badge/deps-ffmpeg%20only-2ea44f.svg)
[![GitHub stars](https://img.shields.io/github/stars/risingjerry/jianying-agent-cli?style=social)](https://github.com/risingjerry/jianying-agent-cli/stargazers)

English · [简体中文](README.zh-CN.md)

![jy demo: an agent pre-processes clips with jy probe, reverse and speed, then builds a 剪映 draft with 叠化 transitions, the 暗灰电影感 filter, a 暗角 vignette and 渐显入场 text animations and renders it with jy render on 剪映's native engine; each command prints one JSON line, then the finished 12-second film "Nightfall" plays](docs/demo.gif)

<sub>Rendered by 剪映's own engine, driven headlessly by <code>jy draft build</code> + <code>jy render</code> — real 剪映 transitions, filter, effect and text animation on public-domain U.S. National Park Service time-lapses — <a href="docs/DEMO-CREDITS.md">footage credits</a> · <a href="docs/demo.mp4">MP4 version</a></sub>

</div>

## Highlights

- **Headless** — no window, no clicks, no mouse. Runs on a machine nobody is watching.
- **Agent-friendly** — stdout is always **one `jy.v1` JSON object**; `ok` is the single truth, errors carry a code and `retriable`. ffmpeg logs go to stderr.
- **Honest output** — every file-producing command re-probes its output with ffprobe, so `ok:true` means a real, playable file.
- **39 commands** (56 with subcommands) — probe, trim, concat, speed, montage/beat cut, subtitles, ASS text animations, 50+ transitions, filters/LUTs, curves/HSL, chroma key, picture-in-picture, audio mix/ducking/loudnorm, local ASR, covers, GIF, batch export, background jobs.
- **Two engines** — **jianying** renders drafts through the 剪映 app you already have installed, with its real transitions, filters, effects, 花字 and text animations (fetched on demand); **local** (ffmpeg, offline, deterministic) for preprocessing or when you explicitly want no 剪映.
- **Finds and fetches 剪映 materials** — `jy effects search-remote 推近` searches 剪映's full live material catalog (transitions, filters, effects, 花字, text animations, stickers; cached listings as offline fallback), `jy effects fetch 推近` downloads the package into 剪映's cache (md5-checked, atomic), and a spec can just say `"transition":{"name":"推近"}` — `jy draft build --fetch` resolves and downloads by name.
- **Speaks 剪映** — `jy transition --type 叠化`, `jy filter --preset 日系`: Chinese 剪映 names resolve to a local implementation and report how close they are (`fidelity: exact | close | rough`).
- **Zero Python dependencies** — standard library + ffmpeg. Ships an [agent skill](skills/jianying-native/SKILL.md) so your agent knows how to use it.

## Install

```sh
git clone https://github.com/risingjerry/jianying-agent-cli.git && cd jianying-agent-cli && python3 install_global.py
```

Needs Python 3.9+ and ffmpeg. `brew install ffmpeg-full` is recommended (libass for subtitles/text, x264/x265, zimg); plain `ffmpeg` works and `jy` degrades per capability instead of pretending. The installer symlinks `jy` into `~/.local/bin` (an existing non-`jy` file there is moved to `jy.bak-<time>`, never deleted). Then:

```sh
jy doctor     # ffmpeg builds + capability matrix + 剪映 engine state
```

## Quick start

```sh
jy probe a.mp4 b.mp4                                   # codec / size / fps / duration / audio + concat compatibility
jy trim a.mp4 --start 4 --duration 8 -o seg.mp4
jy concat seg.mp4 b.mp4 -o rough.mp4                   # stream copy when specs match, otherwise normalised
jy transition a.mp4 b.mp4 --type 叠化 --duration 0.5 -o joined.mp4
jy text joined.mp4 --content "Title" --position top --animation slideup -o titled.mp4
jy subtitle titled.mp4 --srt subs.srt --style cinematic -o subbed.mp4
jy export subbed.mp4 -o final.mp4 --resolution 1080p --fps 30 --quality 20 --faststart
jy frame final.mp4 --at 50% -o check.jpg               # look before you ship
```

A failure is still one JSON object:

```json
{"schema":"jy.v1","command":"export","ok":false,"outcome":"unknown","data":null,
 "error":{"code":"NOT_FOUND","message":"input not found: missing.mp4","retriable":false,"details":{}},
 "warnings":[],"meta":{"ffmpeg":"/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg","elapsed_ms":3}}
```

## Use with Claude Code / Codex / other agents

The repo ships an [Agent Skill](skills/jianying-native/SKILL.md) (rules, the `jy.v1` envelope, recipes, limits) plus a tool-agnostic [AGENTS.md](AGENTS.md).

```sh
python3 install_global.py --with-skill     # copies the skill to ~/.claude/skills and ~/.agents/skills (backs up existing)
# or, with the skills CLI:
npx skills add risingjerry/jianying-agent-cli
# or by hand:
cp -r skills/jianying-native ~/.claude/skills/     # Claude Code
cp -r skills/jianying-native ~/.agents/skills/     # Codex and other Agent Skills hosts
```

Then just ask: *"Cut these three clips into a 15-second vertical video with a title and subtitles."* The agent calls `jy`, branches on `ok` / `error.code`, and checks frames before delivering.

## Commands

| Area | Commands |
|---|---|
| Inspect | `doctor` `probe` |
| Edit | `trim` `concat` `speed` `reverse` `rotate` `flip` `crop` `montage` `beats` `overlay` |
| Look | `transition` `filter` `adjust` `curves` `hsl` `hdr` `mix` `key` `beautify` |
| Text & subtitles | `text` `subtitle` (burn in / export SRT) `asr` (local whisper.cpp) |
| Audio | `audio` (volume, fades, BGM mix/replace, ducking, loudnorm, denoise) `extract-audio` |
| Output | `export` `gif` `frame` `cover` `batch` |
| 剪映 engine | `draft build` (auto-lists in 剪映) `draft register/import` `render` `effects list/search/params` `material status` `fonts` |
| Sound effects | `sfx index/search/add/list/label/forget` — harvest the SFX you downloaded in 剪映, mix one into a video at `--at` (no network, no GUI) |
| Projects | `draft list/rename/duplicate/delete/move/history` `draft register/import` `asset import/list/prune` |
| App hygiene | `update status/lock/unlock` — stop 剪映's Sparkle auto-updater reversibly (no binary patch) |
| Unattended | `--async` + `jobs list/status/wait/kill` `clean` |

Full reference: [USAGE.md](USAGE.md) (Chinese) · `jy <command> --help` · [docs/CLI-HELP.txt](docs/CLI-HELP.txt) · long-form per-command notes in [docs/REFERENCE.zh-CN.md](docs/REFERENCE.zh-CN.md).

## How it works

```
             ┌── local engine ──▶ ffmpeg / ffprobe (argv lists, no shell) ──▶ out.mp4 (re-probed)
jy <verb> ───┤
             └── jianying engine ─▶ jy draft build ─▶ draft_info.json (plain JSON)
                                    jy render ─────▶ 剪映's local bridge (127.0.0.1, per-launch token)
                                                     ─▶ 剪映's own exporter ─▶ out.mp4 (re-probed)
```

- **Local engine**: picks the most capable ffmpeg on the machine, validates every parameter, builds filter graphs, and verifies the result.
- **jianying engine**: writes a timeline as a plain 剪映 draft and asks the **running** 剪映 app to export it through the localhost bridge it already exposes. No UI automation, no pointer movement. A focus guard hands the foreground back to your app if 剪映 raises its window. Protocol notes: [docs/BRIDGE.md](docs/BRIDGE.md).
- `jy` itself makes **no network requests**. 剪映 may talk to its own servers (material downloads); that traffic is the app's.

### Sound effects, locally

Downloading a sound effect once in 剪映's panel leaves a **plain, playable MP3** in
`~/Movies/JianyingPro/User Data/Cache/music/` (measured: 2,970–7,568 B, 0.34–0.47 s, 128/65 kbps).
`jy sfx index` harvests those files (read-only) plus your own `~/Movies/jy-sfx/`, and `jy sfx add`
mixes one into a finished video at an exact time point with `amix(normalize=0)`, then re-probes the
output and compares window RMS so a drowned-out effect is reported instead of silently shipped.

```bash
jy sfx index                                        # user dir + 剪映 cache
jy sfx search 叮                                     # 中文 or English, returns id/duration/bytes
jy sfx add 3aa742bdae1c --at 2.3 -i in.mp4 -o out.mp4 --gain -6
jy sfx label 592be87899cd --name 叮-清脆提示 --tags ding,hint   # cache files are md5-named
```

No network, no GUI, no credits. These are **剪映-cached assets**: fine locally, not redistributable.
Details, ranking rules and the read-only guarantee: [docs/SFX.md](docs/SFX.md).

## What works, honestly

| Capability | Status |
|---|---|
| Local editing, encoding, subtitles, text, transitions, grading, keying, audio, batch | Works offline; covered by `tests/smoke.sh` (267 checks) in CI on macOS |
| 剪映 Chinese names for transitions / filters | Mapped to ffmpeg approximations, each reported with a fidelity grade — **not** the 剪映 material library |
| `beautify` | Global soft-skin look, **not** face-aware beauty |
| `key` | Single-colour chroma key, **not** smart matting |
| `asr` | Local whisper.cpp only; reports `UNSUPPORTED` if no binary/model; never uploads audio |
| `sfx` | Indexes local audio only (your dir + 剪映's download cache, read-only); mixing is ffmpeg — it never writes to a 剪映 draft audio track |
| 剪映 render of clips + styled text | Verified with 剪映专业版 11.6 on macOS (frame diff) |
| 剪映 materials (effects, transitions, filters, stickers, 花字, beauty, matting, keyframes, masks, color wheels, text animations) | Draft structure matches what the 剪映 GUI writes; pixel-verified on the author's machine with materials already cached. Uncached materials can render as no change or a black layer **without an error** — always check frames |
| VIP / SVIP materials | Need your own membership. `jy` names the refused material and never bypasses entitlement checks |
| HDR tone mapping | Only where the local zimg build supports it; otherwise `UNSUPPORTED` |
| Windows / Linux | Not tested. The 剪映 engine is macOS-only |
| Uploading / publishing | Out of scope |

## FAQ

**How do I edit videos with Claude Code or another AI agent?**
Install `jy` and its skill (`python3 install_global.py --with-skill`), then describe the edit in plain language. The agent runs `jy` commands; each returns one JSON object, so it can check `ok`, read `data.output` (duration, size, codec) and retry only when `error.retriable` is true.

**Can I automate 剪映 / CapCut without the GUI?**
Partly. Everything an ffmpeg pipeline can do runs without 剪映 at all. For 剪映's own materials, `jy draft build` + `jy render` hand a draft to the running 剪映 desktop app through its local bridge — no clicking, no window to drive — but 剪映 must be open and logged in.

**Does it need a 剪映 VIP membership?**
No for the local engine — it does not use 剪映 at all. The 剪映 engine follows 剪映's own rules: VIP/SVIP materials need your membership, and `jy` reports which item was refused (`BLOCKED`). `--on-entitlement local` re-renders with local approximations and lists every downgrade.

**Does it work with CapCut (international)?**
It targets 剪映专业版 for macOS (`/Applications/VideoFusion-macOS.app`). The local engine is app-independent. CapCut international has not been tested.

**How does my AI agent learn to use `jy`?**
Through [skills/jianying-native/SKILL.md](skills/jianying-native/SKILL.md) (Claude Code, Codex and other Agent Skills hosts) and [AGENTS.md](AGENTS.md) (any agent that reads repo instructions). Both point to `jy doctor` and `jy <verb> --help` as ground truth.

**Is it safe to run on my real projects?**
Outputs are never overwritten without `--force`, inputs are read-only, draft-management commands are dry-run unless `--apply` and back up 剪映's index first, and `render` only renders drafts `jy` created. The bridge token is never written to disk or logs.

**Why not just call ffmpeg directly?**
You can. `jy` adds the boring, error-prone parts agents trip over: parameter validation, filter-graph escaping, capability detection, concat normalisation, output verification, a stable JSON contract and background jobs.

## Uninstall

```sh
rm ~/.local/bin/jy
rm -rf ~/.claude/skills/jianying-native ~/.agents/skills/jianying-native   # if you installed the skill
rm -rf ~/.cache/jy                                                         # jobs and scratch drafts
```

Then delete the cloned folder. `~/Movies/jy-staging` exists only if you used the 剪映 engine (`jy clean --staging --apply` removes it).

## Development

```sh
./tests/smoke.sh                     # end-to-end suite on synthetic media; 剪映 checks skip when the app is not running
JY_SMOKE_RENDER=1 ./tests/smoke.sh   # also run one real 剪映 export (needs 剪映 running)
python3 docs/render_demo.py          # regenerate docs/demo.gif + demo.mp4 with 剪映 running (--engine local: ffmpeg only); needs Chrome, ffmpeg, jq, gifsicle
```

## Disclaimer

This is an unofficial, independent tool. It is not affiliated with, endorsed by, or sponsored by ByteDance, 剪映 / JianyingPro or CapCut. All trademarks belong to their owners. The 剪映 engine only drives a copy of the app you installed and logged into yourself, within that app's own entitlement rules.

---

If `jy` saved you an afternoon of ffmpeg flags, a ⭐ helps other people (and their agents) find it.

[![Star History Chart](https://api.star-history.com/svg?repos=risingjerry/jianying-agent-cli&type=Date)](https://star-history.com/#risingjerry/jianying-agent-cli&Date)

[MIT](LICENSE)
