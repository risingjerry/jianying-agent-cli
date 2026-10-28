# jy — 详细参考（中文）

> 这是逐命令的长篇参考与开发记录。若与 [USAGE.md](../USAGE.md)、`jy <verb> --help` 或 `jy material status` 的输出冲突，**以后者为准**（本文部分「剪映引擎层」段落写于素材层接线之前）。


`jy` 是一个**双引擎**视频命令行工具：导入探测 → 裁剪/拼接/变速/蒙太奇 → 转场/文字/字幕/调色/抠像/音频 →
分辨率等导出参数 → 导出。全流程 headless、无 GUI、无网络、无遥测；每次调用在 stdout 输出**恰好一个** `jy.v1`
JSON 对象，方便 agent 直接消费。**38 个命令**，`./tests/smoke.sh` 223 项断言全绿。

- **本地引擎（默认）**：ffmpeg 驱动，确定性剪辑与编码，快、离线、不耗会员额度。
- **剪映引擎（可选）**：`jy draft build` + `jy render` 把时间线写成剪映草稿，交给正在运行的剪映原生渲染出片——
  不点鼠标、不开面板、不需要 agent server。**文字/字幕的样式（颜色/字号/描边/位置）已验证由剪映真实渲染**；
  转场/滤镜在无本地素材目录时会被静默忽略、画面特效目前会黑帧，需要这些效果时请先用本地
  `jy transition` / `jy filter`（见「剪映引擎层」与 `USAGE.md` 的能力现状表）。

特效部分是**离线可渲染的那一半剪映语汇**：能用 ffmpeg 老实做出来的就做（xfade 转场、ASS 文字/字幕、
调色预设、色度抠图、柔肤感），必须 VESDK 的（素材特效、智能抠像、真人美颜）不假装能做，并在
README/`--help`/`data.note` 里写清楚。

> 完整命令参考、spec 形状、错误码与限制：见 [USAGE.md](../USAGE.md)。给 agent 的操作规则：见 [AGENTS.md](../AGENTS.md)。
> 剪映 bridge 协议：见 [BRIDGE.md](BRIDGE.md)。

## 为什么是 ffmpeg，而不是驱动剪映

对剪映专业版（`/Applications/VideoFusion-macOS.app`）的分析结论：

- 随包自带的 `lyra-cli` **有宿主进程门禁**：在 shell 里运行任何命令（含 `--help`）都返回
  `err_code -100015 "unauthorized host process"`，只信任 `VideoFusion-macOS` / `JianyingPro` 等祖先进程。
- 它**没有通用的"草稿 → MP4 导出"命令**；唯一的 MP4 导出入口是 `draft template render`，且导出规模受会员/积分限制。
- 剪映真正不可替代的是**素材特效 / 模板 / 云 AI**（需要 VESDK 渲染引擎或联网额度），这些不在本工具范围。

而用户的优先级链路（本机视频导入 → 剪辑/拼接 → 分辨率等导出参数 → 导出）ffmpeg 100% 覆盖，
30s 1080p 合成源约 3.5s 出片。因此 `jy` 把 ffmpeg 的能力产品化成稳定 CLI，而不是去绕剪映的门禁。

## 环境要求

- macOS（开发机为 arm64 / macOS 26.6.2），Python 3（仅标准库，无需 pip）
- ffmpeg / ffprobe。推荐 Homebrew 的 `ffmpeg-full`（带 libass/libfreetype/fontconfig/zimg）：

  ```bash
  brew install ffmpeg-full
  # /opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
  ```

- 本机同时存在两个构建时，`jy` 会自动选择能力更全的那个（见下文「ffmpeg 发现」）。

## 快速开始

```bash
cd jianying-agent-cli
./bin/jy doctor                                   # 看两个 ffmpeg 构建、版本、滤镜/编码器矩阵 + 特效能力

# 3 个最常用示例
./bin/jy probe ~/Movies/demo.mp4                  # 看编码/分辨率/帧率/时长/音轨/旋转
./bin/jy export ~/Movies/demo.mp4 -o out.mp4 --resolution 1080p --fps 30 --quality 20
./bin/jy concat a.mp4 b.mp4 -o joined.mp4         # 参数一致自动 -c copy，否则重编码归一

# 离线特效（不需要剪映）
./bin/jy transition a.mp4 b.mp4 --type dissolve --duration 0.5 -o out.mp4
./bin/jy text in.mp4 --content "标题" --position top --font-size 64 --animation slideup -o out.mp4
./bin/jy montage a.mp4 b.mp4 c.mp4 --per-clip 2 --transition fade -o out.mp4

# 剪映引擎（需要剪映在运行）：写草稿 -> 剪映原生渲染
./bin/jy draft build --spec spec.json -o /tmp/my-draft
./bin/jy render /tmp/my-draft -o out.mp4
```

所有命令失败时也输出 JSON（`ok:false`），不会把 traceback 打到 stdout：

```bash
./bin/jy export in.mp4 -o out.mp4 --resolution 4k
# -> {"schema":"jy.v1","command":"export","ok":false,"outcome":"unknown", ...}
```

## 统一输出契约（jy.v1）

stdout **永远只有一个 JSON 对象**（`--help` 除外：人类帮助写 stderr，stdout 为空）：

```json
{
  "schema": "jy.v1",
  "command": "export",
  "ok": true,
  "outcome": "executed",
  "data": {"output": {"path": "...", "size_bytes": 123, "duration_s": 3.0,
                       "codec": "h264", "width": 1920, "height": 1080, "fps": 30.0}},
  "error": null,
  "warnings": [],
  "meta": {"ffmpeg": "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "elapsed_ms": 1234}
}
```

| 字段 | 说明 |
|---|---|
| `schema` | 固定 `jy.v1` |
| `command` | 实际执行的动词（`doctor`/`probe`/`export`…） |
| `ok` | **唯一成功判据**；`true` 时 `error` 必为 `null` |
| `outcome` | `readonly`（只读查询）· `preview`（dry-run）· `executed`（已产出文件）· `noop` · `unknown`（失败） |
| `data` | 成功时的载荷；失败时恒为 `null` |
| `error` | 失败时 `{code, message, retriable, details}`；`details.tail` 带 ffmpeg stderr 最后 ~20 行 |
| `warnings` | 非致命提示（如 copy 关键帧、能力回退、参数被忽略） |
| `meta` | 本次实际使用的 ffmpeg 路径 + 耗时 |

错误码：`VALIDATION`（参数/输出冲突）、`NOT_FOUND`（输入不存在）、`UNSUPPORTED`（能力不足/格式不支持）、
`UNAVAILABLE`（没有可用的 ffmpeg）、`INTERNAL`（ffmpeg/ffprobe 执行失败）、`BLOCKED`（保留）。

退出码：`0` 成功；`2` 参数校验失败（`VALIDATION`）；`1` 其它失败。

- 每个 `executed` 命令的 `data.output` 都经过**执行后 ffprobe 复核**：`ok:true` 必然对应一个真实可解析的文件。
- `--verbose` 时把 ffmpeg 日志（`-loglevel info`）写到 **stderr**；不开时 stderr 保持安静。
- 所有 ffmpeg 调用都是 argv 列表（`shell=False`），不联网、不写输入目录。

## 命令

### 素材层（material-graph，由 `jy_cli/material.py` + `verbs/material.py` 提供）

```bash
jy effects list [--family 转场|滤镜|特效]     # 会里能查到的剪映素材/特效条目
jy effects search "清晰"                     # 按中文名/英文名/id 搜
jy effects params <effect-id>               # 该素材的参数 schema（强度等）
jy material status                          # 本机素材缓存/注册状态
jy material register <draft-dir>            # 把 jy 草稿注册进剪映的草稿列表
```

`draft build` 的 spec 现在支持每个 clip 上的 `transition` / `filters` / `video_effects` /
`face_beauty`（字段与剪映明文草稿的 material schema 对齐）：

> **重要警告（早期实测）**：剪映会**接受并保留**这些
> material，但渲染结果可能是"没有效果"或黑块，而且**不报任何 native error**（不是
> `film_export_dependency_missing`）。已关闭的结论：
>
> | 写入的 bucket | 剪映是否消费 | 实测结果 |
> |---|---|---|
> | `materials.video_effects` + segment ref | **是** | 素材包不完整时**渲染成黑块**（同一 clip 对比：1,036,800 像素变黑），无报错 |
> | `materials.effects` + `bind_segment_id` | 否（no-op） | 0 像素变化；补 `md5`/`path`/`category_name` 都不改变 |
>
> 所以"`render` 成功" ≠ "效果生效"：只有逐帧 diff（`jy frame` 取同一时间点对比）才能确认；
> 本机缓存里只有通用引擎模板、没有逐特效的 scene/texture 资产，这是外部无法补的。
> `jy` 不会把"渲染成功"当作"特效应用成功"来报告；`--on-entitlement local` 的本地近似会把
> `filters`/`video_effects`/`face_beauty` 逐项标成 `dropped (no local equivalent)`。
>
> **美颜**：字段挂在 **video material** 上（`beauty_face_preset_infos`、`beauty_face_auto_preset{name,preset_id,rate_map,scene}`、
> `is_set_beauty_mode`/`is_unified_beauty_mode`），不是独立 bucket。它从人脸检测开始，所以用合成色块
> 无法验证（正确输出就是"无变化"）；要验证必须用带人脸的真实素材。**离线近似仍然只有 `jy beautify`**（全局柔肤感，
> 不是人脸美颜）。最高价值的下一步：在剪映 GUI 里开一次画面特效面板/导入一个已带特效或美颜的草稿，再把生成的
> material 原样读出来——那一个样本能同时定死特效资产集与美颜字段。

### `jy doctor`

报告所有候选 ffmpeg：路径、是否存在、版本、配套 ffprobe、缺失的关键滤镜/编码器、能力矩阵
（`drawtext`/`subtitles`/`ass`/`zscale`/`palette*`/`xfade`/`blend`/`colorkey`/`lut3d`/`unsharp` 等），以及最终选中的二进制。

`data.effects` 是特效能力的**结构化自检**，供 verb 诚实降级：`libass`（看 configure 里的
`--enable-libass`）、`ass`/`subtitles`、`xfade`（含 `transition_count` + 完整名称表）、`lut3d`、
`unsharp`、`colorkey`、`blend`、`gblur`、`acrossfade`，每项都带 `available` 和提供它的候选路径。
本机实测：`ffmpeg-full` 有 libass/ass/subtitles，普通 `ffmpeg` 没有；`xfade` 名称表 58 个。

`data.jianying` 是剪映引擎的自检（应用是否运行、描述文件/端口、health、登录态、以及 film-export 路由的**干跑**响应）。
干跑只 POST 空 body 让"剪映"的请求校验器返回 409 `film_export_invalid_request`，不会创建导出任务。详见「剪映引擎层」。

### `jy probe <file...>`

返回每个文件的：`codec`、`width`/`height`、`fps`（`r_frame_rate` + `avg_frame_rate` + 数值）、`pix_fmt`、
`duration_s`、`bitrate`、`audio_streams`、`size_bytes`、`rotation`（按旋转矩阵换算显示尺寸），
以及 `concat.compatible`/`key`/`mismatches` —— 判定这几个文件能否直接 `-c copy` 拼接。

### `jy trim <in> --start 1.5 --end 4.0 -o out.mp4`

- `--end` 或 `--duration` 二选一；都不给则剪到片尾。
- 默认重编码（libx264 crf20 veryfast + aac 192k）；`--copy` 走流拷贝，**起点会对齐到最近关键帧**
  （warning 会明确提示），需要帧级精确请用默认重编码。

### `jy concat <a> <b> [...] -o out.mp4`

- 所有输入的 `codec/width/height/fps/pix_fmt` 一致 → concat demuxer `-c copy`（快、无损）。
- 不一致 → concat 滤镜重编码，**统一到第一个输入**的分辨率/帧率，补边黑底，`-pix_fmt yuv420p`。
- `--mode copy|reencode` 可强制模式；强制 copy 但输入不一致会返回 `VALIDATION` 并附 mismatch 明细。
- concat 列表文件写在 `tempfile.TemporaryDirectory` 内，文件名含单引号也已正确转义，用完即删。
- 输入音频不一致（部分无音轨）时输出为纯视频，并给 warning。

### `jy speed <in> --factor 2.0 -o out.mp4`

`setpts=PTS/factor`；音频用 atempo 链（>2 或 <0.5 自动拆成多级），`--no-audio` 丢弃音轨。

### `jy extract-audio <in> -o out.m4a|.mp3|.wav|.flac`

按扩展名选编码器：m4a→aac、mp3→libmp3lame、wav→pcm_s16le、flac→flac；有损格式默认 192k（`--bitrate` 可改）。

### `jy sfx index|search|add|list|label|forget`

本地音效库（不联网、不开剪映、不用额度）。两个来源：`$JY_SFX_DIR`（默认 `~/Movies/jy-sfx/`，递归，
文件名即搜索名）和剪映缓存 `User Data/Cache/{music,effect,artistEffect,AITextTemplate}`（**只读收割**，
文件名是 md5，条目保留 `downLoadcfg` 里的 `resourceId`）。

```bash
jy sfx index [--source DIR]...
jy sfx search 叮 [--limit 10] [--max-duration 2] [--source user|cache|extra]
jy sfx add <id|名字> --at 2.3 -i in.mp4 -o out.mp4 [--gain -6] [--fade-in 0.05] [--fade-out 0.1] [--force]
jy sfx list [--source ...] [--limit N]
jy sfx label <id> --name 叮-清脆提示 [--tags ding,hint]
jy sfx forget <id|名字>
```

- 索引：`~/Movies/jy-sfx/index.json`（`jy.sfx.v1`，原子写）；条目
  `{id(sha256 前 12), path, name, source, resource_id, duration_s, sample_rate, channels, bitrate, bytes, mtime, tags[], labelled}`。
- 检索：名字相等 1.0 → 名字包含 0.8 → 标签相等 0.75 → 标签包含 0.55 → token 重叠（≥2 字符）×0.25–0.7 →
  `resource_id` 0.4；内置中文→英文同义词（叮→ding/bell/hint、转场→whoosh、砰→bang/hit…）。
- 混音：`volume/afade/adelay` + `amix(duration=first,normalize=0)` 保留原声；`-c:v copy`；
  输出 ffprobe 回验 + `astats` 比较窗口 RMS（见 `verification.window_rms_db` vs `original_window_rms_db`）。
- 歧义：匹配到多个候选 → `VALIDATION` + `details.candidates`（id/名字/时长/大小），绝不替你挑。
- 只读保证：不写/不删剪映缓存；`forget` 只改索引。音效是剪映缓存资产，**不可再分发**。
- `jy doctor` → `data.sfx{index_path, index_present, indexed, sources, cache_audio_files, labelled, index_mtime}`。

### `jy frame <in> --at 3.5 -o cover.jpg`

`--at` 支持秒数（`3.5`）或百分比（`10%`），输出 `.jpg`/`.png` 单帧。`--at` 超出时长会 `VALIDATION`。

### `jy export <in> -o out.mp4 [参数]`

主命令，导出参数对齐剪映导出面板，纯本地实现：

| 参数 | 取值 | 默认 |
|---|---|---|
| `--resolution` | `480p`/`720p`/`1080p`/`1440p`/`2k`/`4k`/`8k`/`original`/`WxH` | `1080p` |
| `--fit` | `contain`（保比例补边）/`cover`（保比例裁切）/`stretch`（拉伸） | `contain` |
| `--no-pad` | contain 时不补边（输出尺寸可能小于目标） | 关 |
| `--fps` | `24/25/29.97/30/50/59.94/60/same` | `same` |
| `--codec` | `h264`(libx264)/`h265`(libx265)/`h264_vt`/`hevc_vt`/`prores`(prores_ks 422HQ) | `h264` |
| `--quality` | CRF 1–51（与 `--bitrate` 互斥） | `20` |
| `--bitrate` | `12M`/`8000k`/`600000`（ABR） | 无 |
| `--preset` | `ultrafast`…`veryslow` | `veryfast` |
| `--audio-codec` | `aac`/`copy`/`none` | `aac` |
| `--audio-bitrate` / `--audio-sample-rate` | `192k` / `48000` | 同左 |
| `--bitdepth` | `8`/`10`（10 需 h265 → `yuv420p10le`） | `8` |
| `--faststart` / `--no-faststart` | mp4/mov 的 moov 前置（网络播放优化） | 开 |
| `--hw` | h264→`h264_videotoolbox`、h265→`hevc_videotoolbox` | 关 |
| `--dry-run` | 只输出 `data.ffmpeg_argv`，`outcome=preview`，不执行 | 关 |

说明：
- `--resolution` 用档位时会跟随源方向（竖屏 720p → 720×1280）；显式 `WxH` 按字面执行。
- 横竖比例不一致时 contain 会补黑边，cover 会裁切；`scale` 使用 lanczos。
- VideoToolbox 没有 CRF 语义：`--quality` 会线性映射到 `-q:v`（1–100，越大越好）；要精确码率请用 `--bitrate`。
- ProRes 只能输出 `.mov`/`.mkv`；`--bitdepth 10` 需要 `--codec h265`（或 `--hw` 下的 hevc_vt）。

### `jy gif <in> --start 0 --duration 3 --width 480 --fps 12 -o out.gif`

`palettegen` + `paletteuse` 两阶段调色板，`--duration` 缺省为剩余时长。

### `jy subtitle <in> --srt file.srt|file.ass -o out.mp4`

libass 烧录字幕，`--font-size`（默认 48）、`--font-name`（默认 `Hiragino Sans GB`）、`--margin-v`（默认 40）。
**如果所有候选 ffmpeg 都没有 `subtitles` 滤镜，直接返回 `UNSUPPORTED`，绝不静默丢字幕。**

- `--srt` 接受 `.srt` 也接受 `.ass`/`.ssa`（直通，不再重排）：`.ass` 输入时 `--max-chars-per-line` 会被忽略并 warning。
- `--style default|bold|cinematic|karaoke`：以 libass `force_style` 覆盖 ASS 样式实现
  （`karaoke` 是**静态**亮黄填充＋粗描边的"卡拉OK感"，逐字高亮需要字级时间戳，SRT 不带，别当智能字幕）。
- `--position top|center|bottom`：整轨对齐到上/中/下（写 `Alignment`）。
- `--max-chars-per-line N`：烧录前把 `.srt` 按 N 字重排（CJK 按字符断，拉丁尽量按空格断；解析不了就 warning 并跳过）。
- 字号单位注意：**subtitle 走 libass 默认的 288p 脚本坐标**，实际像素 ≈ `--font-size × 视频高 / 288`
  （720p 下 48 ≈ 120px）。这是历史行为，保持不变；要按真实像素控字号用 `jy text`。
- 字体注意：本机没有 PingFang（`/System/Library/Fonts` 里没有 PingFang.ttc），而这一版 libass 用 CoreText
  取字**不做逐字回退**，字体缺了就出豆腐块。所以默认改成 macOS 自带的中文字体 `Hiragino Sans GB`；
  换字体前先用 `fc-match "<family>"` 确认本机真有。

### `jy transition <a> <b> --type 叠化|闪白|fade|dissolve|… --duration 0.5 -o out.mp4`

`--type` 同时接受 **剪映中文名**（叠化/闪白/闪黑/黑场/白场/模糊/光晕/圆形/矩形/推移/滑入/拉近/拉远/旋转/
信号故障/毛刺/闪动/胶片烧灼/电视雪花/录像带/叠影/光效扫光/漏光/闪光/星光/漫画/弹跳/抖动/翻滚/声音转场/
云朵/水墨/翻页 等 **41 个**）和 **ffmpeg xfade 名**（58 个）。
`jy transition --list` 同时打印两栏：xfade 名称 + 剪映中文名 → 本地 xfade → fidelity。

- 两个输入先用 **concat 同一条归一化链**统一到第一个输入的分辨率/帧率（scale+pad+setsar+fps），
  再 `format=yuv420p` + `settb=AVTB`，**输出恒为 yuv420p**。
- `--offset` 默认 = 第一个片段时长 − 转场时长（转场落在两段接缝处）；越界会 clamp 并 warning。
- 两边都有音轨才做 `acrossfade`；只有一边有音轨时输出纯视频并 warning（与 `concat` 一致）。
- **中文名解析链**：xfade 原名 → 剪映中文名（忽略大小写/空格/全角，可带「转场」后缀）→ 英文 `id_hint` →
  `VALIDATION` 并给出最接近的名字（含按剪映分组给的候选，例如 `电影感` → 电影组的 `暗调/变形宽银幕/莫兰迪`）。
- 每次解析都写进信封：`data.resolution{requested, resolved, engine:"local", fidelity, matched_by,
  catalogue_grade, group, note}`。`fidelity` 是**本地近似与剪映原效果的接近程度**，被本地原语能力封顶
  （所以可能比 `catalogue_grade` 低，例如原版用 lutrgb 做的「闪动」在 xfade 里只能算 rough）。
  `rough` 会在 `warnings` 里明确提示；`no`（本地没有等价原语，如 星形/心形/时钟/3D运镜/视差/粒子/火焰/
  水墨晕染）直接 `UNSUPPORTED` 并给出最接近的可渲染选项。
- `--type` 不在当前 ffmpeg 的名称表里 → `VALIDATION`；片段比转场短 → `VALIDATION`。
- **这不是剪映转场库**：剪映的转场包是 GLSL/shader + lua 参数（VESDK 渲染），`xfade` 只给像素级基础转场。

### `jy text <in> --content "标题" [样式] -o out.mp4`

用 **ASS 脚本 + libass**（不是 drawtext）烧一行文字，所以描边/动画/CJK 都成立；没有 libass 时 `UNSUPPORTED`。

| 参数 | 取值 | 默认 |
|---|---|---|
| `--content` | 文本；`\n` 转硬换行；`{`/`}` 会被转义成字面量 | 必填 |
| `--start` / `--duration` | 出现时间与时长（秒） | `0` / `3` |
| `--position` | `top`/`center`/`bottom` 或精确像素 `X,Y`（越界 → VALIDATION） | `bottom` |
| `--font-size` | **真实像素**（PlayRes = 视频尺寸） | `64` |
| `--font-name` | 字体族 | `Hiragino Sans GB` |
| `--color` | 颜色名 / `RRGGBB` / `#RRGGBB` | `white` |
| `--outline` | 描边宽度（px，颜色固定黑） | `2` |
| `--animation` | `none`/`fadein`(`\fad`)/`slideup`(`\move`)/`typewriter`(逐字事件) | `none` |

### `jy montage <clip...> (--per-clip SEC | --sync-beats FILE) [-o out.mp4]`

蒙太奇/踩点：先把每个片段裁成一段，再拼起来（`--transition` 给了名称就走 xfade 链，否则用 `concat` 滤镜）。

- `--per-clip 2.0`：每段 2s（片长不足时用整段并 warning）。
- `--sync-beats beats.txt`：**每行一个切割时间戳（毫秒）**，严格递增；第 k 行是第 k 段的结束点
  （`700/1200` → 0.7s + 0.5s 两段）。**一段对应一个 clip**：cuts 多于 clips 会 `VALIDATION`（不循环复用素材），
  clips 多于 cuts 会 warning 报告未使用的片段。
- 所有片段归一化到第一个输入；`--transition-duration` 必须小于每一段时长（否则 `VALIDATION`）。
- 全部片段都有音轨才保留音频（`acrossfade` 或 concat 音频），否则纯视频 + warning。

### `jy filter <in> --preset 日系|黑白|暗调|vivid|film --intensity 0..100 -o out.mp4`

`--preset` 接受 **剪映中文滤镜名**（28 个：原片/清晰/自然/白皙/粉嫩/冷白/奶油/港风/胶片/日系/黑白/
高对比黑白/褪色/暖阳/青橙/清透/美食/质感动感/赛博/烟灰/暗调/变形宽银幕/莫兰迪/油画/素描/黑白漫画/马赛克/镜像）、
旧的本地预设名（`vivid`/`warm`/`cool`/`film`/`bw`/`vintage`），以及 `--lut file.cube`。
`jy filter --list` 打印「剪映名 → 本地预设 → fidelity」+ 32 个本地预设名。

- 解析链：剪映中文名 → 英文 `id_hint` → 本地预设名 → `VALIDATION` + 最接近候选（含分组提示）。
- 信封同样带 `data.resolution{…, fidelity, matched_by, catalogue_grade, note}`；`rough` 进 `warnings`，
  `no`（目前只有 `油画`：需要风格化笔触，ffmpeg 无等价实现）→ `UNSUPPORTED` + 建议（素描/黑白漫画/烟灰）。
- 为对齐这些名字，本地预设从 6 个扩到 **32 个**（新增 `clarity`/`natural`/`fair_skin`/`pink`/`cool_white`/
  `creamy`/`hk_retro`/`japanese`/`mono`/`mono_hc`/`faded`/`warm_sun`/`teal_orange`/`crisp`/`food`/`texture`/
  `cyber`/`ash`/`dark_cinema`/`anamorphic`/`morandi`/`sketch`/`comic_bw`/`mosaic`/`mirror`/`none`），
  实现全是 `eq`/`curves`/`colorbalance`/`hue`/`unsharp`/`gblur`/`noise`/`edgedetect`/`pixelize`/`drawbox` 等原生滤镜。
- **诚实边界**：这是本地近似，不是剪映滤镜库/LUT 商城；`--lut` 用你自备的 .cube 才能更接近原版。
  数据为人工整理的「剪映中文名 → 本地实现」对照，放在
  `jy_cli/data/effect_aliases.json`（只存别名表，不含剪映 497 KB 素材清单）。

- 每套用 ffmpeg 原生 `eq`/`colorbalance`/`curves`/`hue` 链在**全强度**下调色，
  `--intensity` 用 `split` + `blend=all_mode=normal:all_opacity=1-k` 在原片与调色片之间线性混合
  （`0` = 原片，`100` = 全强度）。
- `--lut` 走 `lut3d`（`.cube`/`.3dl`），与 `--preset` 互斥；路径含空格/单引号已转义。
- 强度 0 与源片差异只剩编码噪声（实测 mean|diff| ≈ 0.04/255）。

### `jy key <in> --color 00FF00 --similarity 0.3 --blend 0.1 --bg color:black -o out.mp4`

**色度抠图**（`colorkey` + `overlay`）：把一种颜色抠成透明，铺到纯色或图片背景上。

- `--bg color:<name|#RRGGBB>|图片路径`：图片按 **cover** 缩放裁切到画面尺寸，并 `-loop 1` 保持整段；
  传视频会被拒（`VALIDATION`，需要动态背景请先自己合成）。
- `--similarity`/`--blend` 直接映射 `colorkey` 的同名参数（0..1，越大越宽松/边缘越软）。
- 输出恒为 yuv420p，时长取源片。
- **只做色度抠图**：智能抠像/自定义抠像/抠像描边/蒙版跟踪都在剪映的 VESDK 引擎里，ffmpeg 无法解释。

### `jy beautify <in> --smooth 40 --brighten 20 --sharpen 30 -o out.mp4`

> **这是全局"柔肤感"调色，不是人脸美颜。** 没有面部检测、没有瘦脸/美型/美妆；
> 真正的美颜需要剪映渲染引擎（VESDK）。`data.face_aware` 恒为 `false`，`data.note` 里也写着这句。

实现：`gblur`（sigma 随 `--smooth` 到 8）→ `blend` 回原片（最多 60% 模糊，保留细节）→
`eq`（`--brighten` 控 brightness/饱和度）→ `unsharp`（`--sharpen` 控锐化量，把被磨掉的边缘找回来）。
`--smooth/--brighten/--sharpen` 都是 0 时输出等于重编码并 warning。

## 剪映引擎层（draft + render）

`jy` 现在有**两套引擎**，按需要选：

| 引擎 | 入口 | 依赖 | 适合 |
|---|---|---|---|
| 本地 ffmpeg（`--engine=local` 对应这一侧） | `jy export`/`trim`/`concat`/`subtitle`/… **34 个 ffmpeg 动词** | 只要 ffmpeg | 剪辑/拼接/转码/字幕/特效/调色/音频/批处理：快、离线、结果确定 |
| 剪映 `--engine=jianying` | `jy draft build` + `jy render` | **剪映专业版正在运行且已登录** | 用剪映自己的渲染器出片：视频片段 + 文字（当前已验证） |

剪映这条路**不开 GUI**：`jy` 把时间线写成剪映的明文 `draft_info.json`，再调用剪映主进程里的
native bridge（`127.0.0.1:<每次启动随机端口>`，token 在 `$TMPDIR/infinite-canvas-native-bridge.json`）
的 `POST /custom-agent/film-export`，由剪映原生任务渲染 MP4。不需要切窗口、不需要点导出、不联网（localhost 除外）。

### 前提

1. 剪映专业版（11.6 验证）**必须正在运行**；未登录时 `accountScope` 取不到，会明确报 `UNAVAILABLE`（`retriable:true`）。
2. 端口和 token 每次启动都会变：`jy` 每次调用都重新读描述文件，**不缓存、不落盘、不打印 token**；`jy doctor` 可自检。
3. 草稿只写你指定的目录；`jy` 从不读写 `~/Movies/JianyingPro/...` 里的真实草稿，也不注册剪映的草稿列表。

### `jy draft build --spec spec.json -o <draft-dir>`

spec 是最小时间线（clips 里的相对路径按 spec 文件所在目录解析）：

```json
{
  "canvas": {"width": 1280, "height": 720, "fps": 30},
  "clips": [
    {"path": "a.mp4", "start": 0, "duration": 3.0},
    {"path": "b.mp4", "start": 3.0, "duration": 2.0, "source_start": 1.5}
  ],
  "texts": [
    {"content": "标题", "start": 0.5, "duration": 2.0,
     "style": {"color": "#FFD400", "alignment": "center", "bold": true}}
  ]
}
```

- `canvas` 可省略：默认取第一个 clip 的宽高/fps；只有文字时必须给。
- `clips[]` 必须按时间顺序且不重叠；`start` 省略 = 接在上一个片段之后；`duration` 省略 = 源文件剩余时长；
  `source_start` 是源文件内的入点。超出源时长会 `VALIDATION`（先 `jy trim` 或缩短 `duration`）。
- **未知字段一律报错**（顶层和每个 clip 都校验）。曾经因为静默忽略字段，把文字样式写成了默认白字；
  现在拼错的 key 会 `VALIDATION` 并列出允许的字段 + 最接近的名字。
- **素材 staging（默认开）**：clip 源文件先 stage 到 `~/Movies/jy-staging/<hash>/`（同卷优先硬链接，跨卷复制），
  草稿里引用 stage 后的路径。原因：`~/Downloads` 之类受 macOS TCC 保护的目录会在剪映里弹模态授权框，
  把剪映整体卡死。`--no-staging` 关闭，`--staging-dir` / `$JY_STAGING_DIR` 改目录。
  信封：`data.staging{enabled,root,total_bytes,linked,copied,clips[{original,staged,copied,bytes,linked}]}`。
  本地动词（trim/concat/export/…）仍然原地读原文件，不复制。
- **逐片段属性**（写进 `draft_info.json`，`data.clip_properties` 报告写了几段）：

  | 字段 | 含义 |
  |---|---|
  | `transform{x,y,scale,rotation}` | 画面位移（±1 归一化）、缩放、旋转（度） |
  | `opacity` | 0..1 → `clip.alpha` |
  | `volume` | 0..8 → segment volume（原声增益） |
  | `crop{x,y,w,h}` | 0..1 归一化裁切框 → 画面素材的 `crop` 四角多边形 + `crop_ratio:"free"` |
  | `speed` | 0.05..20 倍速（draft.py 生成 speed 材质） |
  | `render_index` | 图层渲染顺序（越小越靠后；文字默认 15000） |
  | `keyframes[{property,points}]` | 关键帧通道，`property` 用剪映通道名（`KFTypeScaleX`/`KFTypePositionX`/`KFTypeAlpha`/`KFTypeVolume`…）；`points[]` 至少 2 个：`{time(秒), values:[…], left_control, right_control}`（草稿里存成 µs + 贝塞尔手柄） |
  | `curve_speed{name,id,points:[{x,y}]}` | 变速曲线（替代恒定 speed 材质），至少 2 个点 |
  | `mask{resource_type,name,resource_id,path,config}` | 蒙版；`resource_type` 必填（`linear`/`line` 线性、`circle` 圆形、`rect` 矩形…），落在 `materials.common_mask` |
  | `color_wheel{segment,red,green,blue,resource_id?,path?,hsl?}` | 色轮（调色）：`segment` ∈ `gamma`/`lift`/`gain`/`offset`，三个通道各 -1..1；写 `materials.primary_color_wheels[]`，`hsl:true`（默认）再加一个 `materials.hsl[]` 伴随材质并用 `constant_material_id` 双向配对 |

  例：`{"path":"a.mp4","start":0,"duration":2,"transform":{"scale":1.2,"x":0.05},"opacity":0.9,"speed":1.5}`。
- `texts[]`：`start` 默认 0、`duration` 默认 3s；同一文字轨道不允许重叠。`style` 支持
  `font_size`/`color`/`alignment(left|center|right)`/`bold`/`italic`/`underline`/`letter_spacing`/`line_spacing`
  （字段会写进草稿；实际渲染限制见下）。文字项同样**校验未知字段**。
- `texts[].animations`：`[{type:"in"|"out"|"loop", resource_id, name?, duration?, path?, category_id?,
  category_name?}]`——入场/出场/循环动画；`resource_id` 必填（剪映从自己的资源缓存解析，jy 不编造 id），
  生成 `materials.material_animations`（`duration` 单位秒，草稿里存 µs）。四个时间线材质
  （keyframes / curve_speed / mask / 文字动画）都已在剪映里像素级验证过（缩放轨迹、单片段内 700× 快慢比、
  蒙版只压暗下半、文字动画逐帧像素一致）。
- `texts[].text_template`：`{resource_id, name?, path?, category_id?, category_name?, resources?: [{panel,
  resource_id, path?, source_platform?}]}`——文字模板（新桶 `materials.text_templates[]`，`resource_id` 必填）。
  `resources[].panel` ∈ `default`/`sticker`/`fonts`/`text`，每项都要自己的 `resource_id`；未知子键和错的
  `panel` 都会 `VALIDATION` 并给最近似建议（`dafault` → `default`，`resourceid` → `resource_id`）。
  文字项白名单从 `draft.py` 的 `_TEXT_ITEM_KEYS` 动态读取，schema 校验在本模块，两边不会各写一份。
- 输出目录：不存在则创建；**非空目录拒绝**。`--force` 只对 jy 自己的草稿生效（目录里有 `.jy-draft.json` 标记，
  或就是只有 `draft_info.json` 的旧形态）；重建时会清掉剪映在上一轮渲染里生成的文件，避免下次渲染重放旧时间线。
- 信封返回 `data.draft_path`、`data.draft_sha256`（与文件实际 sha256 一致）、`duration_s`、`canvas`、`tracks`。

### `jy draft list|rename|duplicate|delete|move|history`

管理剪映草稿根目录（默认 `~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft`，
可用 `--draft-root` 或 `$JY_DRAFT_ROOT` 指向别的根，测试就用临时目录）：

```bash
jy draft list                       # 读 root_meta_info.json：名字/路径/时长/修改时间/文件夹
jy draft rename 旧名 新名 [--apply]  # 改名（目录 + 索引）
jy draft duplicate 名字 [--name 新名] [--apply]
jy draft delete 名字 [--apply]       # 移到 <root>/.jy-trash/<时间>-<名字>，不真删
jy draft move 名字 文件夹 [--apply]
jy draft history                    # 列出 .jy-backups/root_meta_info.*.bak 与草稿内的 .backup/draft_info.json.bak*
```

安全规则（都强制）：**默认 dry-run**（`outcome=preview`，只报告将要做什么），`--apply` 才执行；
每次改动先把索引备份到 `<root>/.jy-backups/`；删除只移到 `.jy-trash`（可手动移回）；
任何解析后落在草稿根目录之外的路径一律 `VALIDATION` 拒绝（防符号链接/恶意索引越界）。

### 草稿索引备份（`material register` / 草稿动词共用约定）

任何写 `root_meta_info.json` 的操作都会先落**两份**时间戳备份并校验非空，然后才写入：
`<root>/.jy-backups/root_meta_info.json.<ts>.json`（`jy draft history` 会列出来）和
`<root>/root_meta_info.json.bak-<ts>`（沿用旧的恢复步骤）。信封 `data.backup` / `data.backups[]` 报告实际路径；
dry-run 不产生任何备份。

### `jy asset import|list|prune`

jy 自己的素材箱（不是剪映素材库）：`$JY_ASSET_DIR` 或 `~/Movies/jy-assets`，清单是 `index.json`。

```bash
jy asset import a.mp4 logo.png      # 按 sha256 去重复制
jy asset list                       # 每个条目的 staged 路径、大小、present/missing、age_hours
jy asset prune --older-than-hours 168 [--apply]   # 默认 dry-run
```

### `jy fonts [--family TEXT] [--source system|jianying] [--check FAMILY]`

枚举系统字体（`/System/Library/Fonts`、`Supplemental`、`/Library/Fonts`、`~/Library/Fonts`）和剪映自带字体
（`VideoFusion-macOS.app/Contents/Resources/Font`，含 `SystemFont/`），直接用 stdlib 读 sfnt `name` 表拿字族名
（TTC 会逐个 face 读）。本机实测 394 个字族。
`--check`/`--font` 是**严格校验**：拼错就 `VALIDATION` 并给建议（`jy fonts --check "Hiragino Sans GB"`）。
`text`/`subtitle` 的 `--font-name` 找不到时会**警告**（不阻断），提示用 `jy fonts` 排查——
libass 自己会静默回退，警告是为了让"豆腐块"可诊断。

### `jy render <draft-dir> -o out.mp4 [--force] [--timeout-ms 600000]`

- 调剪映 native film-export，**同步等它渲染完**，再用 ffprobe 复核；`data.output` =
  `{path,size_bytes,duration_s,codec,width,height,pix_fmt,fps}`。
- 默认不覆盖已有输出（`VALIDATION`/退出码 2）；先写同目录临时文件，成功后原子替换，失败不留半成品。
- 剪映渲染时会往草稿目录里补 staging 文件（`draft_content.json`、`template-2.tmp`、`draft_meta_info.json` 等）
  并规范化 `draft_info.json`，所以**渲染后草稿 SHA 会变**；这是剪映的正常行为，`jy` 不删这些文件（下次 `--force` 重建时才清理）。
- 本机实测：4.5s / 720p 草稿约 1.7s 出片（剪映原生任务耗时，不含打开应用）。
- 错误映射（`error.code` 可直接给 agent 决策）：
  - `film_export_entitlement_denied` / `..._bypass_forbidden` → `BLOCKED`「该素材/导出需要会员额度」；
  - `film_export_dependency_missing` → `UNSUPPORTED`，消息点名缺少的资源（先在剪映里下载/用一次该素材）；
  - `film_export_unsupported_timeline` → `UNSUPPORTED`（多时间线/模板草稿）；
  - `film_export_cancelled_or_timeout` / 客户端超时 / 端口拒绝 / 401 → `UNAVAILABLE` + `retriable:true`；
  - `film_export_invalid_request` → `VALIDATION`（草稿/字段不合法）。
- `--engine=jianying`（默认）走剪映；`--engine=local` 表示"本地 ffmpeg 引擎"，但 `render` 不实现草稿的本地渲染，
  所以会**明确拒绝**并指向 `jy export`/`trim`/`concat` 等动词，不会悄悄换一种渲染方式。
- **焦点守卫**：剪映渲染时会把 剪映 窗口拉到最前（实测：即使先把剪映隐藏，frontmost 仍会变成
  `com.lemon.lvpro`）。`--focus-guard` 三档：

  | 档位 | 行为 |
  |---|---|
  | `watch`（默认） | 渲染前记录 frontmost，渲染期间每 `--focus-interval-ms`（默认 200）用 `lsappinfo` 检查、被抢就 `open -b <bundle>` 拉回，结束后恢复 |
  | `restore` | 完全不轮询，只在收到结果后把原来的前台应用拉回一次（最保守） |
  | `off` | 不碰焦点（`--no-focus-guard` 是它的别名） |

  **不动鼠标、不点界面、不注入事件**。信封 `data.focus_guard = {enabled,mode,available,previous_app,checks,reactivations,restored,note}`。
  残余行为（诚实说明）：`watch` 下剪映仍会短暂抢到前台（一次渲染实测约 0.2-1s 的闪烁，3 次重激活），
  做不到 100% 不闪；要完全避免只能 `--focus-guard off` 或不用剪映引擎。
  **排查记录**：曾怀疑"导出期间轮询拉回前台会让剪映关不掉导出窗口"，A/B 矩阵（同一草稿、热启动剪映：
  guard off→ok 3s、guard on→ok 3s、guard off→ok 2s）**推翻了该假设**，轮询保留为默认。
- **bridge 健康门（快速失败）**：导出前先做一次 **≤5s** 的 health 探测（`--health-timeout-ms`，默认 5000）。
  bridge 不答就不发导出请求，立刻返回 `UNAVAILABLE`，`error.details.bridge{state,pid,port,reachable:false}`，
  并给出对应补救措施——不再出现"等了 10 分钟"。三态（`jy doctor` / `render --preflight` 同样区分）：

  | state | 含义 | 建议动作 |
  |---|---|---|
  | `not_running` | 没有描述文件 / pid 已死 | 打开剪映（`--relaunch-app` 会替你开） |
  | `wedged` | 进程还活着，但 native bridge 不响应（多半卡死） | `--relaunch-app` 或手动重启；不要在同一个 bridge 上反复重试 |
  | `ok` | health 200 | 直接渲染 |

- **`--relaunch-app` 的完整动作**：SIGTERM → 等 8s → SIGKILL → 等 5s → `open` 重开 → 轮询
  `pid alive + health 200`（上限 `--relaunch-wait-ms`，默认 45s），然后再正式导出。卡死的剪映不响应 SIGTERM，
  所以这一步必须能升级到 SIGKILL。
- **`--warmup`（opt-in）**：检测到冷启动 / 刚 relaunch / health 偏慢时，先用一个 1s 纯色草稿做一次
  一次性导出，把"冷启动第一次导出必超时"的代价付掉，再跑真正的导出；`data.warmup{performed,elapsed_ms,failed}`
  如实报告（失败也不阻断正式导出）。
- **单实例锁**：剪映的 film-export 只有一个导出窗口，两个 render 同时进来会互相抢窗口（一个 `native_export_busy`，
  另一个排队/超时）。所以每次 render 都在 `$JY_CACHE_DIR/render.lock`（默认 `~/.cache/jy/render.lock`）上拿 `flock`；
  第二个 render 会等 `--lock-wait-ms`（默认 30000），等不到就返回 `UNAVAILABLE` 并点名"另一个 render 正在占用
  导出窗口"。信封 `data.lock = {path,waited_ms}`。（注意：串行锁解决的是**并发**；今天反复出现的首次超时
  另有原因，见下一条"冷启动"。）
- **冷启动（今天反复踩的真因）**：剪映**刚启动后的第一次** film-export 常以
  `native_export_window_close_timeout`（409，进程空转 ~150% CPU，空的 `film-export-*` snapshot）失败，
  紧接着重试就 2-3s 成功（同草稿、同一实例的 A/B 复现）。jy 会读剪映进程的 uptime（`ps -o etime`）：
  小于 `--cold-start-window-ms`（默认 180000）就判定为冷启动，**只对首次的 window_close_timeout 自动重试一次**
  （其它情况仍然不循环重试），并在信封 `data.cold_start = {detected,app_uptime_s,window_ms,behaviour}` 和
  warnings 里说清楚。`--cold-start-window-ms 0` 可关闭该策略。
- **运输层韧性**：先做 pre-flight（health + 登录 + film-export 路由干跑 + 焦点快照），每次尝试都重新发现
  bridge（端口/token 每次启动都变）。失败梯度是有界的：`native_export_busy` 等 2s 重试一次；传输类失败
  （端口拒绝/401/5xx）重新发现后重试一次；`--relaunch-app` 才会退出并重启剪映再试一次。
  `native_export_window_close_timeout` 这类"导出窗口卡死"默认**不循环重试**，直接 `UNAVAILABLE` 并指向
  重启/`--relaunch-app`；**唯一例外**是冷启动（剪映进程 uptime < `--cold-start-window-ms`）时允许重试一次。
  所有等待都有上限，不会挂住。
  信封 `data.preflight` / `data.attempts[]`（每次的耗时、状态、native_error、是否触发 relaunch）。
- **多时间线迁移（剪映打开草稿后会改写布局）**：一旦剪映打开过 jy 写的草稿，根目录的
  `draft_info.json` 会消失，真正的文件移到 `<draft>/Timelines/<uuid>/draft_info.json`
  （主时间线由 `Timelines/project.json` 的 `main_timeline_id` 指定）。`jy render` 现在按
  「根目录优先 → Timelines/*/draft_info.json（优先 main_timeline_id）」解析：SHA/时长读迁移后的文件，
  草稿目录仍然交给 bridge，`data.draft.layout`（`single`/`multi-timeline`）和 `timeline_id` 如实报告。
  `jy draft list` 的时长同样跟着迁移后的文件走。
- `--preflight`：**只做只读体检**（health / 登录 / film-export 路由 / 焦点 / 锁状态），`outcome=readonly`，
  绝不启动导出——适合在批处理前确认"剪映现在是好的"，也适合 CI/冒烟测试。
- **会员档位（VIP vs SVIP）**：剪映的档位在服务端，本机没有可读缓存（`Config/vipRemindViewedInfo.ini`
  只是 UI「已看过提醒」标记）。所以 `jy` 从不猜档位：被拒时信封会点名**哪个素材**（`data.blocked_item` +
  `error.details.identifiers`：素材 id、能查到时的中文名/文件名）、草稿路径和**原始 exporter code**
  （`native_error`），消息里写明"需要更高的会员档位，剪映区分 VIP 与 SVIP，请在该素材的角标/导出提示里确认
  需要哪一档"。`film_export_dependency_missing` 同样点名缺哪个资源，并提示"打开剪映一次让它在素材库下载"。
- `--on-entitlement fail|local`（默认 `fail`）：`local` 时**不会静默替换**——剪映因档位拒绝后，jy 用记录的
  timeline spec（`draft build` 会写 `.jy-spec.json`）在本机 ffmpeg 上重渲染同一时间线（片段/滤镜/转场/文字），
  并返回 `data.degraded=true` + `data.degraded_items[]`，逐项写明「替换成了什么、丢了什么保真度」
  （剪映滤镜/转场库/花字/贴纸/素材特效没有本地等价物，会被标成 `dropped` 或近似）。没有记录 spec 的旧草稿
  会明确失败并说明原因，而不是偷偷出片。
- `--relaunch-app`（默认关）：bridge 不可达或导出队列卡死时，退出剪映再拉起，等 bridge 回来（上限
  `--relaunch-wait-ms`，默认 45000）后重试一次。**会丢失剪映未保存的界面状态**（草稿是自动保存的），
  所以只有显式给这个 flag 才会做。
- **临时草稿清理**：写进 `$JY_CACHE_DIR/drafts/` 的草稿会被打上 `.jy-draft.json` 标记；渲染成功后
  自动删除（`--keep-draft` 可保留）。用户自己目录里的草稿永远不动。

### `jy doctor`

`data.jianying` 报告：`app_installed`、`running`、`descriptor_found`/`checked_paths`、`bridge_port`、
`health_ok`、`logged_in`，以及 `film_export{responded,status,native_error}`。这里的 film-export 检查是
**干跑**：POST 空 body 让剪映的请求校验器拒绝（409 `film_export_invalid_request`），只证明路由活着，
不会创建任何导出任务。

`jy doctor` 还有四块无人值守自检：

| 字段 | 内容 |
|---|---|
| `focus` | `provider`（lsappinfo/open）、`available`、`frontmost{name,bundle_id}`、焦点守卫的残余行为说明 |
| `jianying.bridge_state` | 三态 `not_running` / `wedged` / `ok` + `pid`/`port`/`reachable`/`health_ms`/`remedy`（补救动作） |
| `jobs` | `cache_root`/`jobs_dir`/`drafts_dir`、任务数、最近 5 个任务（id/命令/状态/alive/stale） |
| `asr` | 本地语音识别：`available`、`engine`（whisper.cpp）、`binary`、`model`、`network`（恒为 never）、缺失时的安装提示 |
| `registry` | 动词注册表：`verbs` 数量、`errors{}`（某个 verbs 模块导入失败时列在这里，而不是让 CLI 崩掉）、`skipped{}` |
| `hdr` | `tonemap`：本机 zimg 是否真的能做 PQ/HLG→线性光（本机为 `false`，见「已知限制」） |
| `entitlements` | `tier_readable_offline:"false"`、`tiers:["VIP","SVIP"]`、为什么读不到（服务端 + `vipRemindViewedInfo.ini` 只是 UI 标记）、被拒时的 error code 列表 |

### 剪映引擎层的已知限制

- **文字排版**：内容/时间轴/图层会渲染，但当前这版剪映的 headless 渲染器使用它自己的默认文字模板
  （实测字号/颜色/对齐字段不生效：文字约 222px、白色、居中）。细粒度排版要等 material-graph
  （字体资源、富文本 `words`/`shapes`）建模；现在请把字幕写短，长文本会被画布裁切。
- **素材特效/转场/花字/贴纸/模板**：需要 `resource_id` + 本机缓存包，jy 还没建模，`jy render` 目前只保证
  视频片段 + 文字的草稿能出片。ffmpeg 侧继续用 `jy transition`/`jy filter` 等做近似。
- **美颜/智能抠像**：仍是 VESDK 能力，剪映引擎层未建模。
- **剪映 GUI 草稿列表**：jy 不往 `root_meta_info.json` 注册草稿，所以 `draft build` 的产物是给
  `jy render` 用的，不保证出现在剪映首页列表。
- 会员额度是剪映自己的策略：需要额度时 jy 报 `BLOCKED`，不做任何绕过。**档位（VIP/SVIP）只能看剪映界面**：
  本机没有档位缓存，`vipRemindViewedInfo.ini` 只是 UI 状态；jy 的职责是把"哪一项被拒"点名清楚，以及
  （`--on-entitlement local` 时）给一个逐项标注的本地近似，而不是替你判断该买哪档。

## 无人值守硬化（焦点 / 韧性 / 异步任务 / 清理）

`jy` 现在的定位是"可以在没人看着的机器上跑"：不抢焦点、不挂住、可后台、会清理。

### 焦点守卫

剪映渲染会把 剪映 窗口拉到最前。`jy render` 默认用 `lsappinfo`+`open -b` 把用户原来的前台应用拉回来：
渲染前记录 frontmost → 渲染期间每 200ms 检查一次、被抢就拉回 → 结束后恢复。
**不动鼠标、不点界面**。实测（本机，Finder 当前台）：关掉守卫时 10 次采样里 7 次前台是剪映、结束后前台停在剪映；
开启守卫时 3 次重激活、`restored=true`、结束后前台仍是 Finder。残余：剪映仍会短暂闪一下，
做不到零闪烁；`data.focus_guard.reactivations/restored` 会把它如实报出来。

### 异步任务（`--async` + `jy jobs`）

任何动词都支持后台跑（`jy <verb> --async` 或 `jy --async <verb>`）：

```bash
jy export big.mp4 --async --resolution 4k -o out.mp4      # 立刻返回 job id
jy jobs list                       # 最近的任务（状态/存活/输出）
jy jobs status j-20261024-121503-ab12
jy jobs wait   j-20261024-121503-ab12 --timeout-ms 600000   # 阻塞到结束，成功时返回子任务的信封
jy jobs kill   j-20261024-121503-ab12                        # SIGTERM→SIGKILL（含进程组）
```

- 状态是 `$JY_CACHE_DIR/jobs/<id>.json`（默认 `~/.cache/jy/`）：没有守护进程、没有 socket。
- 子进程是同一个 CLI 的 detached 进程（`start_new_session`），stdout/stderr 写到 `<id>.log`；子进程写回
  完整的 jy.v1 信封，`jy jobs wait` 成功时把 `data.result` 带出来。
- `jobs list` 的 `alive`/`stale` 是现算的：`stale=true` 表示子进程没了但没写回结果（`jobs wait` 会报
  `UNAVAILABLE` 并给出日志路径，不会一直等）。
- `--timeout-ms` 在支持它的动词上同时是动作超时（如 render 的剪映导出超时）；`jobs wait` 的
  `--timeout-ms` 是等待上限。

### 清理（`jy clean`）

```bash
jy clean --dry-run                 # 只报告（缓存区）
jy clean --older-than-hours 24     # 删 24h 前的临时草稿 + 已结束任务记录/日志
jy clean --drafts-only / --jobs-only
jy clean --staging [--dry-run|--apply]   # draft build 的 staging（~/Movies/jy-staging）
jy clean --assets  [--dry-run|--apply]   # jy 素材箱（~/Movies/jy-assets）
jy clean --all --apply                   # 以上全部
```

注意默认值不同：缓存区（`~/.cache/jy`）沿用历史行为（默认就删，`--dry-run` 只报告）；
`~/Movies` 下的 staging/assets 属于用户可见数据，**默认 dry-run**，必须 `--apply` 才真的删。

扫描 `$JY_CACHE_DIR/drafts/` 里带 `.jy-draft.json` 标记的 jy 草稿，和 `$JY_CACHE_DIR/jobs/` 里非运行中的
任务记录；**永远不碰用户自己的目录**。`render` 成功后会顺手删掉它刚渲染的 jy 临时草稿（`--keep-draft` 保留）。

### 动词注册表

`jy_cli/verbs/*.py` 每个模块导出 `register(registry)`，`cli.py` 只负责组装 parser + 单一 stdout 写入口。
新增动词 = 新增一个文件，不碰别人；某个模块导入失败会被记进 `doctor.data.registry.errors` 并跳过，
不会让整个 CLI 起不来（并行开发时这很重要）。

## 本地动词（离线批处理那一半）

| 动词 | ffmpeg 映射 | 要点 |
|---|---|---|
| `overlay` | `scale`+`format=rgba`+`colorchannelmixer`+`overlay(enable=between)` | 画中画/贴图：`--position top-left\|…\|X,Y`、`--scale`（占底片宽度比例）、`--opacity`、`--start/--duration` 时间窗；图片输入用 `-loop 1` 并在底片时长处封口 |
| `adjust` | `eq`+`colorbalance`+`unsharp`+`vignette` | 7 个参数各 0-100（0=不动）：brightness→eq 0..0.30、contrast→eq 1..1.60、saturation→eq 1..1.80、gamma→eq 1..1.50、temperature→colorbalance ±0.20（暖）、sharpness→unsharp 0..1.5、vignette→angle PI/5..PI/3 |
| `curves` | `curves=preset=…` 或 `curves=master='0/0 0.5/0.7 1/1'` | 11 个 ffmpeg 曲线预设，或自己给控制点 |
| `hsl` | `hue=h=…:s=…`+`eq=brightness` | **全局**色相/饱和/明度，不是剪映那种按颜色区间的 HSL |
| `hdr` | `zscale`（矩阵/原色）+ `tonemap`（HDR→SDR） | 709↔2020 SDR 转换可用；PQ/HLG→709 的 tone map 会先探测本机 zimg，能力不足时**诚实 UNSUPPORTED**（见「已知限制」）；不产出 HDR |
| `mix` | `blend=all_mode=…` | 14 种混合模式（screen/multiply/overlay/lighten/…），`--opacity` 是混合结果权重 |
| `audio` | `volume`/`afade`/`amix`/`sidechaincompress`/`loudnorm`/`afftdn` | 音量、淡入淡出、BGM 混音或替换、ducking（原声压 BGM）、单遍 loudnorm、频谱降噪；视频流 `-c:v copy` |
| `beats` | 纯 Python 能量通量（ffmpeg 解码 PCM） | 输出"每行一个毫秒时间戳"的卡点文件，直接喂 `jy montage --sync-beats`；没有 ML，见准确度说明 |
| `cover` | `-ss` 抽帧 + `-map 0 -map 1 -c copy -disposition:v:1 attached_pic` | 抽一帧并写回 MP4 的 `covr`（`attached_pic`），`--cover-out` 另存 JPEG |
| `batch` | 与 `export` 同一套 `exportspec` 参数 + 输入侧 `-ss/-t` | 同一套 trim+导出参数跑 N 个文件进一个目录；单个失败不中断，逐文件结果在 `data.files`，任一失败 → `ok:false` |
| `reverse` | `reverse`+`areverse` | 视频+音频倒放；>120s 会提示先 trim（整段进内存） |
| `rotate` | `transpose=1/2`（90/270）、两次 transpose（180） | 90/270 会交换宽高 |
| `flip` | `hflip`/`vflip` | 镜像 |
| `crop` | `crop=w:h:x:y` | 像素框或 `--aspect 9:16` 居中裁切；尺寸自动取偶数 |
| `subtitle --export-srt` | ffmpeg 字幕 demuxer（.ass→.srt） | 只导出字幕文件，不渲染视频；`--max-chars-per-line` 会在导出前重排 |
| `asr` | 本地 whisper.cpp（二进制 + ggml 模型） | 解码 16k 单声道 WAV → 本地识别 → SRT；**从不上传**；没有引擎/模型时 UNSUPPORTED + 安装提示。**模型崩溃自动回退一次**：用户 `--model` 跑挂（返回码 <0 / Metal abort 等）→ 用自动发现的默认模型重试一次，成功则 ok + warning 点名失败模型，`data.model` 永远是真正产出 SRT 的那个；两个都不行 → UNSUPPORTED（建议 ggml-medium 或 faster-whisper），绝不再抛带原始 tail 的 INTERNAL |

`beats` 的准确度（诚实）：短时能量差 + 自适应阈值 + 最小间隔，属于经典 onset detection；对鼓点清晰的
4/4 流行/电子很准（合成 click track 实测 476/975/1474…ms，误差 ~1 帧），对连奏、管弦、弱拍会漏或抖。
需要真正的节拍网格/速度估计请用专门工具；`jy` 不联网也不内置 ML 模型。

## ffmpeg 发现与能力降级

1. 顺序：`$JY_FFMPEG` → `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` → `/opt/homebrew/bin/ffmpeg` → `PATH`；
   ffprobe 独立：`$JY_FFPROBE` → 选中 ffmpeg 同目录 → 两个标准路径 → `PATH`。
2. 每个进程内缓存 `-version`/`-filters`/`-encoders` 探测结果。
3. 命令需要特定滤镜/编码器时（如 `subtitles`、`libx265`），先检查首选构建；缺能力就**自动尝试下一个候选**，
   并在 `warnings` 里说明换了哪个、为什么。全部候选都不满足才返回 `UNSUPPORTED`（附每个候选缺什么）。
4. `jy doctor` 同时报告两个构建，方便排查"为什么字幕不可用/为什么选了另一个 ffmpeg"。

## 行为规则

- 输出已存在时拒绝覆盖（`VALIDATION`，退出码 2），除非 `--force`；输出父目录自动创建。
- 输入不存在 → `NOT_FOUND`；输入不是文件/无视频流 → `VALIDATION`/`UNSUPPORTED`。
- 构建滤镜前先 probe；所有临时文件都在系统临时目录，不在输入目录落盘。
- 失败时 stdout 仍是单个 JSON，ffmpeg stderr 最后 ~20 行在 `error.details.tail`。

## 已知限制（有意不做）

- **剪映素材特效 / 转场库 / 花字 / 贴纸 / 模板**：需要 VESDK（`libVECreator.dylib`）或 material-graph
  （`resource_id` + 缓存包），ffmpeg 无法解释，剪映引擎层也还没建模，明确不做。
  现在能做的是**基础近似**：`jy transition` 的 xfade 转场、`jy text` 的 ASS 文字、`jy filter` 的原生调色预设，
  都不是剪映素材库里的同款效果；剪映的 GLSL/材质/lua 特效包无法移植。
- **智能抠像 / 自定义抠像 / 抠像描边 / 蒙版跟踪**：需要 VESDK；`jy key` 只做色度抠图（单色）。
- **美颜美体美妆（磨皮/瘦脸/美型/妆容）**：字段在 video material 上（见「素材层」），但能力来自 VESDK 的人脸检测；`jy beautify` 只是**全局柔肤感**，不做人脸识别；用合成测试片无法验证剪映美颜（正确输出就是无变化）。
- **字幕识别 / 翻译 / 双语字幕 / 字幕模板**：需要联网 + 会员额度，本工具离线不做；`jy subtitle` 只烧本地字幕文件。
- **云 AI 能力**（超清、补分辨率、AI 补帧、智能调色）：需要联网 + 会员/积分，本工具全程离线，不做。
- **HDR Vivid 元数据 / 杜比 AC-4 / DD+**：超出 MVP 参数面；色彩空间只保留 ffmpeg 原生能力（需要时可用 `-vf` 手工扩展，本 CLI 不暴露）。
- **多音轨选择**：只处理默认音轨（`-map` 未暴露），MVP 范围内。
- `trim --copy` 的关键帧限制、VideoToolbox 的 quality 映射、ProRes 容器限制见上文。
- **字体**：libass 用 CoreText 取字且不做逐字回退，字体缺了就出豆腐块；默认 `Hiragino Sans GB`，
  换字体请先用 `fc-match "<family>"` 确认本机存在。
- **`jy key` 的图片背景**按 cover 裁切、无位移/缩放参数；动态背景需自己先做一段底片再合成。
- `jy montage` 的 beat 文件是"切割时间戳"语义；`jy beats` 能本地生成它，但不是节拍网格/变速对齐。
- **HDR tone mapping 在本机不可用**：这台机器的 zimg 对 `zscale=t=linear` 报 `ZIMG 3074 no path`（传函转换缺失），
  libplacebo 又因为没有可用 Vulkan 驱动而失败（`VK_ERROR_INCOMPATIBLE_DRIVER`）。所以 `jy hdr` 只保证
  709↔2020 的 SDR 矩阵/原色转换；`--from pq|hlg` 会先探测、能力不足时直接 `UNSUPPORTED`（不会偷偷输出
  一个看起来像 HDR 的假货）。`--to hlg|pq`（真 HDR 输出）明确不做：需要 10-bit + mastering metadata。
- **ASR 只走本地 whisper.cpp**：本机有 `whisper-cli` + `~/.whisper-models/ggml-base.bin`，所以 `jy asr` 能用；
  没有二进制或模型时是 `UNSUPPORTED` + 安装提示，**不会**回落到任何云端服务。base 模型对中文有错字，
  重要片子要人工校对。
- **剪映 film-export 是单实例的**：并发渲染会把剪映的导出窗口卡死（`native_export_busy` /
  `native_export_window_close_timeout`，进程空转）。jy 用 `render.lock` 串行化并给有界等待，但如果你绕过 jy
  直接调 bridge，或在多个终端同时渲染，仍可能把剪映卡住；卡死后只能重启剪映（或 `--relaunch-app`）。
- **焦点守卫做不到零闪烁**：剪映抢前台后 jy 最快 50ms（默认 200ms）拉回，短暂闪烁仍可能出现；
  鼠标指针永不被移动。
- **`jy adjust`/`curves`/`hsl` 是全局调色**：没有剪映的肤色保护、局部蒙版、按颜色区间的 HSL、色轮。
- **`jy batch` 的进度**：同步跑完才返回；长批次建议 `--async` + `jy jobs wait`。

## 测试

```bash
cd jianying-agent-cli
./tests/smoke.sh
```

`smoke.sh` 在临时目录用 lavfi 合成测试片（含音频；外加纯绿底片、红色贴图、背景图、.ass、
2×2×2 .cube、click track、PQ 标记片、`say` 合成的语音），覆盖 43 个命令标签，共 **265 条断言**（本机实测：**260-262 passed / 0 failed / 3-4 skipped**，差异只取决于剪映 preflight 是否可跑），断言分布：

- 既有 63 条：doctor、probe（含旋转/兼容性）、trim（重编码 + copy）、concat（copy + 重编码 + 文件名含单引号）、
  speed、extract-audio（m4a + mp3）、frame（秒 + 百分比）、export（720p/1080p/4k、h264/h265、--hw、
  --faststart、--dry-run、显式 WxH、覆盖保护与 --force）、gif、subtitle（srt 路径含空格）。
- 新增 58 条：doctor 的特效能力块、`transition --list` + fade/wipeleft（含输入不一致归一）+ `--offset` + 3 类参数错误；
  `text`（CJK+字面大括号、typewriter 事件数、slideup/十六进制颜色、非法颜色、坐标越界）；
  `subtitle`（cinematic+top+自动换行、karaoke、.ass 直通、换行下限）；
  `montage`（per-clip+fade 的时长/尺寸/段数、concat 且音轨保留、beats 时长、cuts>clips、转场长于段）；
  `filter`（vivid/bw 时长尺寸不变、identity LUT、preset/lut 互斥、强度越界）；
  `key`（纯色背景 + **实测角落像素变黑**、图片背景、视频当背景被拒、诚实 note）；
  `beautify`（默认参数、时长尺寸、honest note、强度越界）。
- 新增 12 条（剪映引擎层）：`draft build`（2 clip + 1 text 的 sha256/轨道数/时长/文件落盘、拒绝非 jy 目录）、
  `render --engine=local` 被拒、`doctor` 的剪映自检结构、`render --preflight`（只读体检、不产出文件、
  不抢前台）以及 preflight-only 的 `--async` 任务。
- 新增 88 条（Batch 1）：`overlay`（时间窗内**像素级验证**贴图出现/消失、video-on-video、非法位置）、
  `adjust`（7 参数/全零/越界）、`curves`（preset/points/互斥）、`hsl`、`hdr`（709→2020、pq 探测的
  ok-or-UNSUPPORTED、拒绝 HDR 输出）、`mix`、`audio`（音量+淡入淡出+loudnorm、BGM 混音/ducking/替换+降噪、
  无事可做被拒）、`reverse/rotate(90/180/270)/flip/crop(9:16 + 像素框 + 越界)`、`cover`（attached_pic 流 + 覆盖保护）、
  `batch`（2 文件 fragment、dry-run 计划、覆盖保护）、`beats`（≥3 个单调毫秒戳）+ **jy beats → jy montage**
  端到端、`subtitle --export-srt`（srt→srt 重排、.ass→srt、与 -o 互斥）、`asr`（本地 whisper，无模型时诚实 UNSUPPORTED）。
- 新增 37 条（剪映中文别名层，task-11）：`transition --list`/`filter --list` 的别名表（41 转场 / 28 滤镜）、
  中文名解析（叠化/闪白/模糊/拉远/黑白/日系/清晰/马赛克/暗调/镜像）、`data.resolution` 的
  requested/resolved/fidelity/matched_by、rough 进 warnings、`no` → UNSUPPORTED、未知名 → VALIDATION +
  分组建议（`电影感` → 电影组）、旧本地预设与 raw xfade 仍然可用。
- 新增 2 条（收尾，task-19 follow-ups）：一个脚本覆盖色轮材质（`primary_color_wheels` + hsl 伴随 +
  挂到 segment）、多时间线迁移（`Timelines/<uuid>` + `main_timeline_id` 选中、decoy 忽略、SHA/时长来自迁移文件、
  warning）、`draft list` 的 `tm_duration` 回退（加密草稿）、`clean --staging/--assets`（默认 dry-run、`--apply` 才删、
  普通 clean 仍只管缓存）、`material register --apply` 的双备份契约（dry-run 不备份），
  以及 `texts[].text_template`（有效模板 → `materials.text_templates[]`；错 panel / 未知子键 → VALIDATION + 建议）。
- 新增 1 条（Batch C，task-19）：一个不依赖剪映的检查脚本，覆盖 C1 staging（stage 进 `$JY_STAGING_DIR`、
  草稿引用 staged 路径、`--no-staging` 保持原路径）、C2 逐片段属性落盘（transform/opacity/volume/speed/
  render_index/crop 多边形）与未知字段报错（clip 级 + spec 级）、C3 六个草稿动词（dry-run 不动、`--apply` 才动、
  索引备份、删除进 `.jy-trash`、根目录外路径被拒）、C4 素材箱（导入/去重/列出/dry-run/apply 清理）、
  C5 字体（枚举、`--check` 成功与拼错、text 的字体警告）。
- 新增 1 条（韧性，task-13）：一个不依赖健康剪映的检查脚本，覆盖 bridge 三态（wedged / not_running）、
  wedged 时 **<5s 快速失败** 且带 `--relaunch-app` 提示与 `details.bridge.reachable:false`、
  `account_scope` 超时 ≥120s 的回归断言、以及 ASR 模型崩溃→自动回退默认模型（同时验证 UNSUPPORTED 兜底）。
- 新增 5 条（会员档位故事）：`doctor.entitlements`（VIP/SVIP、离线不可读、UI-only 配置文件）、
  `draft build` 落 `.jy-spec.json`、**权益错误点名**（素材 id/中文名 + 草稿路径 + 原始 code + "VIP 与 SVIP"）、
  **dependency_missing 点名资源 + 打开剪映提示**、`--on-entitlement local` 的本地近似（degraded/逐项明细/ffprobe 时长），
  以及**冷启动重试策略**（冷启动时 window_close_timeout 只重试一次，热启动不重试）。
- 新增 30 条（无人值守硬化）：`doctor` 的 registry/focus/jobs/asr/hdr、`jobs list/status/wait/kill`、
  子命令与全局两种 `--async`、kill 后的终态、`clean --dry-run` 与真实清理（临时草稿 + 任务记录）、
  任务记录清扫后 `jobs list` 归零。

**剪映渲染是 opt-in 的**：默认不调用 film-export（导出窗口单实例、且剪映冷启动时首次导出会超时），只验证
`--preflight` 的只读契约；要跑真实导出请 `JY_SMOKE_RENDER=1 ./tests/smoke.sh`，并保证同时只有一个 render。
测试把 `JY_CACHE_DIR` 指到自己的临时目录，不会碰用户真实的 `~/.cache/jy`。

每个成功用例都断言 `ok:true`、输出文件存在、时长/尺寸/编码/像素格式合理；本机没有带 libass 的 ffmpeg 时
字幕/文字用例 skip 并告警，没有 xfade+blend 时特效段整体 skip 并告警。失败时保留临时目录便于排查。

## 目录结构

```
bin/jy             可执行入口（#!/usr/bin/env python3）
jy_cli/envelope.py jy.v1 契约：payload 构建 + 唯一 stdout 发射点（后台任务复用同一 payload）
jy_cli/cli.py      argparse 组装（来自 verbs 注册表）+ 统一派发 + --async 派生
jy_cli/verbs/      动词注册表：每个模块导出 register(registry)，自动发现
  _common.py       动词共享的小工具（非动词）
  core.py          doctor probe trim concat speed extract-audio frame export gif
  effects.py       subtitle transition text montage filter key beautify
  engine.py        draft build / render（剪映引擎，含 focus/lock/preflight 参数）
  layers.py        overlay
  grade.py         adjust curves hsl hdr mix
  audio_mix.py     audio beats
  geometry.py      reverse rotate flip crop
  cover.py         cover
  batch.py         batch
  asr.py           asr（本地 whisper.cpp）
  maintenance.py   jobs list|status|wait|kill / clean
jy_cli/commands.py      既有本地动词实现 + doctor（含 focus/jobs/asr/registry 自检）
jy_cli/effects.py       ASS/观感预设/filter graph 纯函数
jy_cli/exportspec.py    导出参数校验与 argv 构建（纯函数，batch 复用）
jy_cli/ffmpeg.py        ffmpeg/ffprobe 发现、能力探测、执行、probe/output/stream 复核
jy_cli/focus.py         焦点守卫（lsappinfo/open，不动鼠标）
jy_cli/jobs.py          后台任务 + 缓存目录 + 剪映渲染互斥锁 + 临时草稿清理
jy_cli/jianying.py      剪映 native bridge 客户端（发现/健康/accountScope/film-export/错误映射；不落盘 token）
jy_cli/jianying_commands.py  draft build / render（焦点守卫、有界重试、relaunch、preflight、清理）
jy_cli/draft.py         明文 draft_info.json 生成
tests/smoke.sh          端到端冒烟（265 条断言；剪映真实渲染 opt-in）
```
