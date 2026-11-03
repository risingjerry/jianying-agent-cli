---
name: jianying-native
description: Edit and render videos with 剪映专业版 / JianyingPro (CapCut China desktop) from the command line via the `jy` CLI — the agent writes a draft and 剪映's own native engine renders it, no GUI clicks. Search and auto-download 剪映 materials by name (transitions, filters, video effects, 花字 text effects, text animations, stickers, beauty, matting, masks, keyframes, curve speed). Also ffmpeg tooling for preprocessing (probe, trim, concat, speed, reverse, transcode, local speech-to-subtitles) and 剪映 draft management. Use for "edit this with 剪映 / CapCut", "cut this footage into a video", "add transitions, a filter, effects, an animated title", "make a promo / demo video", "batch export", 用剪映剪 / 剪映出片 / 加转场滤镜特效花字. Not for uploading or publishing to platforms.
---

# Edit videos with 剪映 (`jy`)

`jy` turns 剪映专业版 into a CLI: you write a draft, **剪映's own engine renders it**, so the result looks
like a hand-made 剪映 edit. `jy` must be on PATH (`jy doctor` works). Flags live in `jy <verb> --help`
and the repo's `USAGE.md` — don't guess them from memory.

## Rule one: if the user wants 剪映, use 剪映

- The user says "use 剪映 / CapCut", wants 剪映 transitions/filters/effects, or wants a polished finished
  video (promo, demo, short) → **use the 剪映 engine**.
- A material isn't on this machine → **search and download it yourself** (`jy effects search-remote` /
  `fetch`). Don't ask the user to click around in 剪映, and don't swap in an ffmpeg approximation because
  something wasn't cached.
- 剪映 isn't running → open it yourself (below). A 剪映 render fails → work through "Engine pitfalls"; if
  it still fails, **stop and tell the user why** and let them decide whether to degrade. Never quietly
  deliver an ffmpeg version instead.
- Use the local engine end-to-end only when the user explicitly asks for no 剪映 / offline, and say how
  it differs from real 剪映 materials.

## Standard workflow (render with 剪映)

```bash
# 1) Check; if 剪映 isn't running, open it in the background and wait for health_ok
jy doctor | jq '.data.jianying | {running, health_ok, logged_in}'
open -g -a VideoFusion-macOS        # only if needed; re-run jy doctor until health_ok is true

# 2) Preprocess footage with the local engine (fast, offline; only when needed)
jy probe a.mp4 b.mp4 | jq '.data.files[] | {path, duration_s, width, height}'
jy reverse sunrise.mp4 -o sunset.mp4
jy speed clip.mp4 --factor 2 --no-audio -o fast.mp4
jy asr talk.mp4 -o talk.srt                     # local speech-to-subtitles

# 3) Pick 剪映 materials: what's installed, then 剪映's full live catalog (offline: falls back to cached panels)
jy effects cached --kind transition
jy effects search-remote 推近 --kind transition   # status: cached | remote, plus a vip flag
jy effects fetch 推近 --kind transition           # downloads into 剪映's cache; usable by name afterwards

# 4) Write spec.json (materials by name) and build the draft; --fetch downloads anything not cached yet
jy draft build --spec spec.json -o ./draft --fetch | jq '.data | {engine, tracks, materials_resolved}'

# 5) Render with 剪映 (long jobs always async)
jy render ./draft -o final.mp4 --async          # returns a job id
jy jobs wait <job-id>

# 6) Verify every material actually shows up (mandatory — see "Delivery discipline")
jy frame final.mp4 --at 10% -o f1.png ; jy frame final.mp4 --at 50% -o f2.png ; jy frame final.mp4 --at 90% -o f3.png
```

Minimal spec, materials referenced by name:

```json
{"canvas":{"width":1920,"height":1080,"fps":30},
 "clips":[{"path":"clips/a.mp4","source_start":2,"duration":3,"transition":{"name":"推近"}},
          {"path":"clips/b.mp4","duration":3}],
 "filters":[{"name":"暗灰电影感","value":0.6}],
 "effects":[{"name":"暗角"}],
 "texts":[{"content":"NIGHTFALL","start":0.3,"duration":2.5,"position":{"x":0,"y":0.1},
           "style":{"color":"#FFFFFF","font_size":14},
           "text_effect":{"name":"梦幻渐变紫边白色发光花字"},
           "animations":[{"type":"in","name":"扫光"}]}]}
```

Clip timing: `source_start` = where to start reading the source; `duration` = how long; `start` = position
on the timeline — **omit it to append after the previous clip** (if given, clips must be in order and not
overlap, else `VALIDATION`). A transition goes on the **left** clip of the cut.

Usually just reference materials by `name` in the spec. Items from `jy effects cached` (including anything
you fetched) also carry a `spec` field whose key is where it
belongs (`clips[i].transition`, `filters[]`, `effects[]`, `texts[i].animations[]`, …) — copy it as is.
Material names are 剪映's Chinese names; search with short keywords.

## Delivery discipline

1. **Frame-check every 剪映 material.** 剪映 silently ignores (or blacks out) materials it can't compose;
   "render succeeded" ≠ "effect applied". Look at the frame at each cut for transitions, compare with/without
   for filters and effects, look at the first frames of animated text. If one didn't apply, pick another
   剪映 material and re-render — don't switch to a local approximation.
2. **Report which 剪映 materials you used** (name + kind) and which steps were local preprocessing.
3. **Touch only your own drafts.** Build drafts in your working directory (`draft build -o`); never edit
   the user's own 剪映 drafts, and don't enumerate them; delete the drafts you created when done. Downloaded
   material packages staying in 剪映's cache is expected. The one exception is an explicit user request to
   put a draft into 剪映's home list: then use `jy draft import/register` (see below), which only touches
   the index and leaves every existing draft alone.
4. One render per draft at a time. 剪映's export may raise its window; `jy render`'s focus guard hands
   focus back. **Never click the 剪映 UI.**
5. Check the output for account names, avatars or watermarks before delivering.

## Hard rules

- **Trust only `ok`.** stdout is always exactly one `jy.v1` JSON object; on failure `data` is null — branch
  on `error.code`.
- Retry only when `error.retriable` is true. `BLOCKED` needs a human (membership/authorization) — never
  loop on it.
- File outputs in `data.output` are re-probed with ffprobe. Existing outputs are never overwritten without
  `--force`; user media is read-only, write to new paths.
- 剪映's port/token change on every launch and `jy` discovers them; never put tokens in files or args.
- Material downloads only use the ByteDance CDN URLs 剪映 itself recorded, with no user cookies/tokens;
  `jy` never reads 剪映 account data.

## Envelope

```json
{"schema":"jy.v1","command":"export","ok":true,"outcome":"executed",
 "data":{"output":{"path":"out.mp4","size_bytes":123456,"duration_s":3.0,
                   "codec":"h264","width":1920,"height":1080,"fps":30.0}},
 "error":null,"warnings":[],"meta":{"ffmpeg":"/path/to/ffmpeg","elapsed_ms":1234}}
```

`outcome` is `readonly` / `preview` / `executed` / `noop` / `unknown`. Exit codes: `0` ok, `1` failure,
`2` validation.

## Engine pitfalls

- **剪映 must be running.** Otherwise `UNAVAILABLE` with "open 剪映 and retry" — open it, don't spin.
- **Cold start**: the first export after launch often times out; `jy` retries once and sets
  `data.cold_start`. Wait for it instead of retrying manually. `jy render --warmup` pays that cost upfront.
- **Bridge hung**: `jy render --relaunch-app --warmup`; don't hammer a dead bridge.
- **Focus**: report `data.focus_guard` honestly; `jy` never moves the pointer.
- **Membership (VIP / SVIP)**: tiers are server-side. A refusal names the material (id + name).
  `--on-entitlement local` re-renders with local approximations and lists every loss in
  `data.degraded_items[]` — only with the user's consent, and tell them. `jy` never bypasses membership.
- **Downloads**: `effects fetch` network errors are `UNAVAILABLE` (retriable); CDN 401/403 → `BLOCKED`;
  md5 mismatch or an expired URL → pick a similar material. `search-remote` queries 剪映's live catalog
  (`source: live`) and falls back to the locally cached listings (`source: cache`, or `--offline`) with a
  warning when the catalog is unreachable; if nothing matches, try a synonym or a similar material.
- **One render at a time**: a cross-process lock serialises renders; never render in parallel.
- **剪映's own updater may be frozen on purpose.** `jy update status` shows whether auto-update is off and
  the installed bundle is `UF_IMMUTABLE`-locked (the owner can `jy update unlock --apply` to install an
  update by hand). Never try to update or reinstall 剪映 to "fix" a render error — read `error.code` first;
  an update would also change the version the draft format was verified against.
- After 剪映 opens a `jy` draft it may migrate it to a multi-timeline layout (`Timelines/<uuid>/`);
  `jy` resolves that automatically.
- **Getting the draft into 剪映's home list** (so a human can fine-tune it by hand): `draft build -o <draft
  root>/name` registers automatically; for a draft living in a project folder use
  `jy draft import <dir> --name "<title>" --apply`. Do it with 剪映 **closed**: a running 剪映 rewrites
  `root_meta_info.json` from memory when it saves/closes and can drop the new entry, so the order is
  quit 剪映 → `--apply` → open 剪映. Both verbs are dry-run by default, back the index up twice, and never
  overwrite an existing draft folder.

## Local engine: tooling, not a substitute

`jy trim/concat/speed/reverse/transition/filter/text/subtitle/montage/key/beautify/export` are ffmpeg-based:
use them for **preprocessing** (cut, reverse, speed, transcode, normalise specs, speech-to-subtitles,
batch resize) and when the user explicitly wants offline. Their 剪映-style names (叠化, 青橙, …) are
**approximations** reported with `fidelity: exact|close|rough` — not 剪映's material library. `beautify`
is not face-aware beauty, `key` is not smart matting, `transition` is not 剪映's transition library. Say
which one you used.

## Troubleshooting

```bash
jy doctor                       # capabilities, 剪映 state, jobs, membership notes
jy render ./draft --preflight   # read-only bridge/route/focus check, no export
jy jobs status <id>             # job state and log path
jy clean --dry-run              # what caches hold (never touches user dirs by default)
```

For the full spec field reference (keyframes, masks, color wheels, curve speed, stickers, beauty, matting,
text templates) see `USAGE.md`.
