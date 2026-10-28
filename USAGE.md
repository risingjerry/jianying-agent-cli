# jy — 使用手册（USAGE）

`jy` 是一套**无 GUI 的视频 CLI**：本地 ffmpeg 引擎负责确定性的剪辑与编码，剪映引擎（可选）负责
剪映独占的素材渲染。所有命令在 stdout 输出**恰好一个** `jy.v1` JSON 对象，`ok` 是唯一成功真相。

> 逐命令的长篇说明与实测记录见 [docs/REFERENCE.zh-CN.md](docs/REFERENCE.zh-CN.md)；剪映 bridge 协议见 [docs/BRIDGE.md](docs/BRIDGE.md)。

```bash
jy doctor                      # 先跑这个：两套 ffmpeg 能力 + 剪映状态 + 异步任务 + 会员注意事项
```

---

## 1. 安装

```bash
git clone https://github.com/risingjerry/jianying-agent-cli.git && cd jianying-agent-cli
./install_global.py            # 软链到 ~/.local/bin/jy（已存在的非 jy 文件会先备份）
./install_global.py --with-skill   # 同时把 agent skill 装进 ~/.claude/skills 与 ~/.agents/skills
./install_global.py --dry-run   # 只看会做什么
```

依赖：macOS + Python 3（标准库，无 pip）+ ffmpeg/ffprobe。
推荐 `brew install ffmpeg-full`（带 libass/libx264/libx265/svtav1/videotoolbox）。`jy` 会自动在两套构建里
选能力足够的那一个，并按能力降级，不会假装支持。

---

## 2. 输出契约（jy.v1）

```json
{"schema":"jy.v1","command":"export","ok":true,"outcome":"executed",
 "data":{...},"error":null,"warnings":[],
 "meta":{"ffmpeg":"/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg","elapsed_ms":1453}}
```

| 字段 | 含义 |
|---|---|
| `ok` | 唯一成功真相。失败时 `data=null` 且 `error` 非空 |
| `outcome` | `readonly` / `preview` / `executed` / `noop` / `unknown`（与 ok 正交） |
| `error.code` | `VALIDATION` `NOT_FOUND` `UNSUPPORTED` `UNAVAILABLE` `BLOCKED` `INTERNAL` |
| `error.retriable` | 是否值得重试（网络/服务类为 true） |
| `data.output` | 执行类命令恒返回 `{path,size_bytes,duration_s,codec,width,height,fps}`，且**跑完用 ffprobe 回验** |
| `meta.ffmpeg` | 本次实际使用的 ffmpeg 二进制 |

退出码：`0` 成功，`1` 失败，`2` 参数校验失败（`VALIDATION`）。
`--verbose` 时 ffmpeg 日志走 stderr；stdout 永远是单个 JSON。

---

## 3. 命令总览

### 3.1 检查与规划
| 命令 | 说明 |
|---|---|
| `jy doctor` | ffmpeg 构建/能力矩阵、jobs、焦点守卫、剪映在线状态、会员档位说明 |
| `jy probe <file...>` | 编码/分辨率/帧率/时长/码率/音轨/旋转 + 拼接兼容分组 |

### 3.2 剪辑（本地引擎）
| 命令 | 说明 |
|---|---|
| `jy trim <in> --start --end\|--duration [-o]` | 裁切（默认重编码；`--copy` 走流拷贝，受关键帧限制） |
| `jy concat <a> <b> ... -o` | 拼接；规格一致自动 `-c copy`，否则归一化重编码 |
| `jy montage <clip...> --per-clip S \| --sync-beats F` | 蒙太奇/踩点：等长切分或按节拍点切分，可带转场 |
| `jy speed <in> --factor 2 [-o]` | 变速（视频 setpts + 音频 atempo 级联） |
| `jy reverse <in> [-o]` | 倒放（含音频） |
| `jy rotate <in> --degrees 90\|180\|270` | 旋转 |
| `jy flip <in> --axis h\|v` | 水平/垂直镜像 |
| `jy crop <in> --x --y --w --h \| --aspect 9:16` | 裁剪（像素框或居中比例） |
| `jy overlay <base> <pip> --position --scale --start --duration --opacity` | 画中画 / 图片贴纸叠加 |

### 3.3 画面与声音
| 命令 | 说明 |
|---|---|
| `jy adjust <in> --brightness --contrast --saturation --gamma --temperature --sharpness --vignette` | 逐项调色（0-100 语义，映射见 README） |
| `jy curves <in> --preset \| --points` | 曲线 |
| `jy hsl <in> --hue --saturation --lightness` | 全局 HSL |
| `jy hdr <in> --from --to` | 色彩空间转换（709↔2020 可用；HDR 输出受本机 zimg 能力限制，不支持时明确报 UNSUPPORTED） |
| `jy mix <a> <b> --mode multiply\|screen\|overlay\|...` | 混合模式（14 种） |
| `jy filter <in> --preset vivid\|warm\|cool\|film\|bw\|vintage --intensity \| --lut x.cube` | 调色预设 / LUT |
| `jy beautify <in> --smooth --brighten --sharpen` | **全局柔肤近似**（非人脸感知，真美颜需剪映引擎） |
| `jy key <in> --color 00FF00 --similarity --blend --bg` | 色度抠图（绿幕） |
| `jy audio <in> --volume --fade-in --fade-out --bgm --replace --duck --loudnorm --denoise` | 音量/淡入淡出/BGM 混音/闪避/响度/降噪 |
| `jy extract-audio <in> -o out.m4a\|mp3\|wav\|flac` | 抽音轨 |
| `jy sfx index\|search\|list\|label\|forget` | **本地音效库**：索引你自己的 `~/Movies/jy-sfx` + 收割剪映已下载的音效（缓存里的普通 MP3），中英文检索 |
| `jy sfx add <id\|名字> --at 2.3 -i in.mp4 -o out.mp4 [--gain -6] [--fade-in 0.05]` | 把一条音效混进成片指定时间点（保留原声，ffprobe 回验） |

### 3.4 字幕与文字
| 命令 | 说明 |
|---|---|
| `jy subtitle <in> --srt x.srt --style --position --max-chars-per-line` | 烧录字幕（SRT/ASS，libass） |
| `jy subtitle <in> --export-srt -o out.srt` | 导出字幕文件 |
| `jy text <in> --content --font-size --color --outline --position --animation` | 单条文字（ASS 动效） |
| `jy asr <in> -o out.srt` | **本地语音转字幕**（whisper.cpp；不联网；无引擎/模型时明确 UNSUPPORTED） |

### 3.5 封面与导出
| 命令 | 说明 |
|---|---|
| `jy frame <in> --at 3.5\|50% -o cover.jpg` | 抽单帧 |
| `jy cover <in> --at 50% -o out.mp4` | 抽封面帧并写入成片封面（`attached_pic`） |
| `jy export <in> -o out.mp4 --resolution 1080p --fps 30 --codec h264\|h265\|prores ...` | 主导出动词（见 §3.7） |
| `jy gif <in> --width --fps --start --duration -o out.gif` | 调色板 GIF |
| `jy batch <in...> -o DIR --resolution ...` | 同一配方批量导出到目录 |

### 3.6 剪映引擎（需要剪映在运行）
| 命令 | 说明 |
|---|---|
| `jy draft build --spec spec.json -o DIR` | 从时间线规格生成草稿工程（含 `.jy-spec.json` 记录，供降级重渲染） |
| `jy render DIR -o out.mp4 [--async] [--on-entitlement fail\|local] [--preflight] [--focus-guard watch\|restore\|off]` | 调剪映原生导出器渲染草稿；带焦点守卫、冷启动重试、崩溃重发现、跨进程渲染锁 |
| `jy effects list\|search\|params` | 剪映素材目录（内置 269 条：id / 中文名 / 类型 / 参数；可用 ~/.config/jy/effects_catalog.json 覆盖） |
| `jy effects cached [--kind K] [--search NAME] [--limit N] [--refresh]` | **本机剪映缓存里真正已下载、能渲染的素材**：kind / 中文名 / resource_id / `status:"cached"` / 可直接粘进 spec 的片段（见 §9.2） |
| `jy effects search-remote KEYWORD [--kind K] [--limit N] [--offline]` | 搜剪映在线素材目录（+本机缓存列表回退）：`status cached\|remote`、`source live\|cache`、`vip`、`downloadable`（见 §9.3） |
| `jy effects fetch NAME\|RESOURCE_ID [--kind K] [--offline]` | 把素材包下载进剪映缓存（md5 校验、原子解压、不覆盖已有包），之后 `cached` / 按名字解析都能用（见 §9.3） |
| `jy material status` | 素材图能力自检：哪些类型已证明能渲染、哪些未验证、字体路径 |
| `jy material register DIR [--apply]` | 把草稿注册进剪映草稿面板（默认 dry-run，自动备份索引） |

#### spec.json 形状（文字样式必须放在 `style` 里）

```json
{"canvas":{"width":1080,"height":1920,"fps":30},
 "clips":[{"path":"/abs/a.mp4","start":0,"duration":4}],
 "texts":[{"content":"标题","start":0.5,"duration":3,
           "position":{"x":0,"y":0.3},
           "style":{"color":"#FFD400","font_size":14,"stroke_width":0.08}}]}
```

- `position` 是**归一化到半画布、y 轴向上**：`screen_x = W/2 + x·W/2`，`screen_y = H/2 − y·H/2`（实测误差 <0.5 px）。
- `style.font_size` 是剪映自己的单位，1080 宽下约 **6.7 px/单位**（14 ≈ 94 px）。
- `style.alignment` 只影响文本框内部对齐，**不会移动文本框**；要移动请用 `position`。
- 样式键写在外层会被判 `VALIDATION` 并提示——避免"静默渲染成默认白字"。

#### 剪映引擎的已知行为

| 现象 | 说明 |
|---|---|
| 冷启动首次渲染失败 | 剪映刚启动的**第一次** `film-export` 常报 `native_export_window_close_timeout`（约 184 s）。`jy` 检测 pid 年龄（默认 180 s 窗口）并**自动重试一次**；`data.cold_start` 会标记 |
| 焦点 | `--focus-guard watch`（默认）在渲染期间把前台还给用户原来的 app，指针不动；`data.focus_guard` 报 `checks/reactivations/restored` |
| 并发 | 同一时刻只允许一个渲染（跨进程 `flock`）；并发请求等待或返回 `UNAVAILABLE` |
| 文字/字幕样式 | **已验证可用**（颜色/字号/描边/位置） |
| 转场 / 滤镜 / 画面特效 / 贴纸 / 美颜 / 抠像 / 花字 | 已按剪映 GUI 草稿的结构接线，并在作者机器（剪映 11.6，素材包已缓存）上逐帧像素验证（见 §9、`jy material status`）。**素材包未缓存或容器写错时，剪映会静默无效或渲染黑帧且不报错**——出片后务必用 `jy frame` 抽帧核对；只要确定性效果时用本地 `jy transition`/`jy filter` |


### 3.8 音效库（`jy sfx`，纯本地）

在剪映音效面板**点一次下载**，文件就是一个普通 MP3 落在
`~/Movies/JianyingPro/User Data/Cache/music/`（实测 2,970–7,568 B / 0.34–0.47 s）。
`jy sfx index` 把这些 + 你自己的 `~/Movies/jy-sfx/` 一起索引，之后 agent 可以「判断要不要音效 →
`jy sfx search 叮` → `jy sfx add <id> --at <节拍点>` → 出片」全程无人：

```bash
jy sfx index                                   # 建/刷新索引（只读剪映缓存）
jy sfx search 叮                                # 中文/英文、按名字+标签，返回 id/时长/大小
jy sfx add 3aa742bdae1c --at 2.3 -i in.mp4 -o out.mp4 --gain -6 --fade-in 0.05
jy sfx label 592be87899cd --name 叮-清脆提示 --tags ding,hint   # 缓存文件是 md5 名，命名一次即可搜
jy sfx forget 592be87899cd                     # 只改索引，不删文件
```

- 混音链：`volume → afade → adelay → amix(duration=first,normalize=0)`，画面 `-c:v copy`，
  回验 ffprobe + 比较音效窗口 RMS（被原声盖住会 warning）。
- 选择器命中多个候选会**报错并列候选**，不会替你挑；输出默认拒绝覆盖（`--force`）。
- 剪映缓存**只读**、不联网、不用额度；但那些音效是剪映缓存资产，**不可再分发**。详见 [docs/SFX.md](docs/SFX.md)。
- 明确不做：把音效写进剪映草稿音轨（结构未逐字段验证）；现在的链路是成片后 ffmpeg 混音。

### 3.7 导出参数（`jy export`）
`--resolution 480p|720p|1080p|1440p|2k|4k|8k|original|WxH` · `--fit contain|cover|stretch` ·
`--fps 24|25|29.97|30|50|59.94|60|same` · `--codec h264|h265|h264_vt|hevc_vt|prores` ·
`--quality 1-51`（CRF）或 `--bitrate 12M` · `--preset ultrafast..veryslow` ·
`--audio-codec aac|copy|none` `--audio-bitrate` `--audio-sample-rate` · `--bitdepth 8|10` ·
`--faststart/--no-faststart` · `--hw` · `--dry-run`（返回 argv，不执行）`--force`

---

## 4. 后台任务（长时间渲染必用）

```bash
jy render draft/ -o out.mp4 --async      # 立刻返回 data.job.id
jy jobs list                             # 全部任务 + running 计数
jy jobs status <id>                      # 单任务
jy jobs wait <id>                        # 阻塞至结束，返回子任务完整 jy.v1 结果
jy jobs kill <id>                        # SIGTERM→SIGKILL（进程组）
```

任务状态落在 `~/.cache/jy/jobs/`（可用 `JY_CACHE_DIR` 改），每个任务一份 JSON + 一份日志；不跑守护进程，状态按需计算。
`--async` 是全局参数，可放在子命令前或后。

## 5. 清理

```bash
jy clean --dry-run                 # 看会删什么
jy clean --drafts-only             # 只清 jy 生成的草稿
jy clean --older-than-hours 24     # 只清旧记录
```

`render` 默认会删掉自己刚渲染的临时草稿（`--keep-draft` 保留）。**永远不动用户自己的草稿目录。**

---

## 6. 剪映引擎的前提与边界

- **剪映必须在运行**（可最小化）。端口与 token 每次启动都变，`jy` 每次重新发现，不缓存。
- **焦点**：剪映的导出任务会把自己的窗口带到前台。`jy` 用焦点守卫在渲染期间把前台还给用户原来的 app
  （每 200ms 一次，指针全程不动），结束后恢复。信封里的 `data.focus_guard` 会如实报告
  `{checks, reactivations, restored}`，仍有极短暂闪烁。
- **会员**：剪映区分 **VIP / SVIP**。`jy` 离线读不到档位（服务端字段，本机无缓存），所以当某个素材被拒时，
  错误里会点名是哪个素材（id + 中文名）、并提示去剪映看该素材角标确认需要哪一档。
  `--on-entitlement local` 可显式退回到本地近似渲染，并在 `data.degraded_items[]` 里逐条说明丢了什么保真度
  （绝不静默替换）。
- **素材未缓存**：报 `UNSUPPORTED` 并点名资源，建议打开剪映一次让它下载。
- **崩溃**：剪映崩过一次是常态风险。`jy` 会重新发现 bridge 并重试一次；`--relaunch-app` 才会重启剪映。

## 7. 已知限制（诚实清单）

| 限制 | 说明 |
|---|---|
| 剪映素材层 | 转场库/滤镜库/画面特效/花字/贴纸/美颜/智能抠像 依赖 material graph 进度；本地已有近似（`transition`/`filter`/`beautify`/`key`） |
| `beautify` | 全局柔肤，不是人脸感知美颜 |
| HDR | 本机 zimg 无 transfer 路径，PQ/HLG→SDR tone mapping 报 UNSUPPORTED；709↔2020 SDR 可用 |
| `asr` | 需本机 whisper.cpp + 模型（`~/.whisper-models/ggml-base.bin`）；不联网、不上传 |
| `beats` | 能量通量检测，实测节拍误差约 ±1 帧；强节奏电子乐最准 |
| 未验证 | 剪映导出 gif/mov、多时间线草稿、非 VIP 账号的完整降级路径 |

## 8. 给 agent 的调用建议

```bash
jy doctor | jq -e '.ok'                        # 先自检
jy probe a.mp4 | jq '.data.files[0]'            # 再决策
jy export a.mp4 -o out.mp4 --resolution 1080p   # 简单路径
jy render draft/ -o out.mp4 --async && jy jobs wait <id>   # 长任务
```

- 只信 `ok`，不要解析人话。
- `retriable=true` 才重试；`BLOCKED` 表示需要用户处理（会员/素材下载），不要循环重试。
- 需要"像剪映"的素材效果时走 `draft build` + `render`；只要确定性剪辑时走本地动词（更快、不耗额度）。

---

## 9. 剪映素材层与精修字段（`jy draft build --spec`）

八类素材在作者机器上像素验证过（剪映 11.6、素材包已缓存），字段结构与剪映 GUI 自己写的草稿一致：文本样式、画面特效、转场、美颜、贴纸、滤镜、抠像、花字；另有**关键帧、曲线变速、蒙版、色轮、文字动画**。

| 字段 | 位置 | 说明 |
|---|---|---|
| `clips[].keyframes` | 片段 `common_keyframes[]` | `[{property:"KFTypeScaleX", points:[{time, values, left_control?, right_control?}]}]`；缓动 = 每帧贝塞尔手柄 |
| `clips[].curve_speed` | `materials.speeds[]` | `{name, id?, points:[[x,y],…]}`，替换恒定倍速 |
| `clips[].mask` | `materials.common_mask[]` | `{resource_type:"line", name, resource_id, config:{width}}`；另有 `constant_material_id` 伴生素材 |
| `clips[].color_wheel` | `materials.primary_color_wheels[]` | `{segment:"gamma\|lift\|gain\|offset", red, green, blue}`，可选 `hsl` 伴生（其字段集尚未像素验证） |
| `clips[].transform/opacity/volume/crop/speed/render_index` | 逐片段属性 | 未知字段会 `VALIDATION` 并给出最近似建议 |
| `texts[].text_effect` | `materials.effects[] type=text_effect` + `content.styles[].effectStyle` | 花字**两处都要写** |
| `texts[].text_template` | `materials.text_templates[]` + 文字段 `material_id` 换成模板 id、`render_index` 14001 | `{resource_id, name?, resources?:[{panel:"default\|sticker\|fonts\|text", resource_id}]}`；模板自带的 text_effect 与 in/loop 动画由 builder 自动挂上（属模板必需组成） |
| `texts[].animations` | `materials.material_animations[].animations[]` | `[{type:"in\|out\|loop", resource_id, duration}]` |
| `effects[] / stickers[] / transitions[]` | 各自独立轨道 / 左片段 refs | 见 §9.1 |

### 9.1 容器速查（错了就黑帧，剪映不报错）

- 画面特效 / 滤镜 / 花字 → `materials.effects[]` + **独立轨道**
- 转场 → **左片段** 的 `extra_material_refs`
- 美颜 → `materials.effects[] type=figure` + 视频 material 的 `is_set_beauty_mode`
- 抠像 → `videos[].matting{flag:3,…}`（逐帧蒙版必须由剪映生成，jy 会明确报错而不是编造路径）

### 9.2 按名字找素材（`jy effects cached`）

剪映只会渲染**已下载到本地缓存**的素材包（`…/User Data/Cache/{effect,artistEffect}/<resource_id>/<md5>`），
没缓存的 id 会被静默忽略。`jy effects cached` 只读地扫描这些包，再从 `Cache/ressdk_db/*/rp.db`
（剪映缓存的面板列表）的**临时副本**里解析中文名——不原地打开剪映的数据库、不写剪映目录、不读任何草稿。
结果按缓存目录和数据库的 size/mtime 记忆在 `~/.cache/jy/jianying_materials.json`（首次约 2–3 s，之后 <0.1 s）。

- `--kind`：`transition` / `filter` / `video_effect` / `text_effect`（花字）/ `text_animation`（`animation_type` 取自入场/出场/循环分类）/ `text_template`，以及 spec 暂不支持的 `face_beauty`、`video_mask`、`matting_stroke`、`color_preset`、`text_glow`、`voice_change`、`other`（这些条目的 `spec` 为 `null`）。
- 每条的 `spec` 是可直接粘贴的片段，键名指明位置：`clips[i].transition`、`filters[]`、`effects[]`、`texts[i].text_effect`、`texts[i].animations[]`、`texts[i].text_template`。
- 剪映未安装/从未打开 → `UNAVAILABLE`；缓存里没有素材包 → `NOT_FOUND`（先在剪映里点开要用的素材让它下载）。`$JY_JIANYING_CACHE` 可改扫描目录。
- `status` 目前恒为 `"cached"`，为以后的远程素材（未下载）预留。

**spec 里可以只写名字**：`"transition":{"name":"叠化"}`、`"filters":[{"name":"暗灰电影感","value":0.6}]`、
`"effects":[{"name":"暗角"}]`、`"animations":[{"type":"in","name":"渐显入场"}]`、`"text_effect":{"name":"…"}`。
`jy draft build` 会从缓存补全 `resource_id` 和 `path`，并在 `data.materials_resolved[]` 里逐条报告；
给了 `resource_id` 但没给 `path` 时也会补上 `path`。名字不存在 → `NOT_FOUND`（附可用名字），同名多个 id → `VALIDATION`（请改用 resource_id）。

### 9.3 搜索与下载素材（`jy effects search-remote` / `jy effects fetch` / `draft build --fetch`）

`search-remote` 查的是**剪映的在线素材目录**（与剪映 App 同一套接口）：转场（`transitions`，约 590）、
滤镜（`filter`，约 330）、画面特效（`effects`）、花字（`flower`，约 556）、文字动画（`text`，约 456）、贴纸（`sticker`）；
关键词搜索 + 面板列表合并，并且合并本机剪映缓存的列表作为回退（离线、接口不可用时）。

```bash
jy effects search-remote 烟雾 --kind transition       # source: live|cache，status: remote|cached，vip
jy effects search-remote 烟雾 --offline               # 只查本机缓存的列表，不联网
jy effects fetch 烟雾转场 --kind transition           # 下载 → Cache/effect/<resource_id>/<md5>/
jy effects fetch 7202575978646737469                 # 按 id：先查在线面板，再查 Artist 单条接口
jy draft build --spec spec.json -o d --fetch          # spec 里按名字引用、未缓存的素材自动下载
```

- **在线接口**：Loki 目录 `lv-effect.ulikecam.com`（面板 `/effect/api/v3/effects`、搜索 `/effect/api/search/effects`），
  按 id 查 Artist `lv-api.ulikecam.com/artist/v1/effect/mget_artist_item`（带剪映客户端同样的 md5 `sign` 头）。
  参数是**剪映发行版 App 的公开客户端标识**（`jy_cli/jianying_live.py`，与每份剪映安装包里的相同），不是用户凭据；
  jy 只发一个**随机生成**的 19 位 `device_id`/`iid`（首次使用时写入 `~/.config/jy/device.json`），不读剪映的真实设备号、
  cookie、token 或账号数据。请求串行、超时 12 s，面板列表在 `~/.cache/jy/live/` 缓存一天。`$JY_LIVE_OFFLINE=1` 全局关闭联网。
- **怎么下载的**：用接口返回的字节 CDN 地址做普通 HTTPS GET（主机白名单），下载后校验 md5，安全解压（拒绝 `..`/绝对路径），
  先解到同目录临时文件夹再原子改名为 `<resource_id>/<md5>/`，并写 `<md5>_modity_time.txt`。已存在的包**不覆盖**（`outcome:"noop"`）。
  Loki 素材放 `effect/`，Artist 素材放 `artistEffect/`（与剪映一致）。下载过的素材名字记在 `~/.config/jy/fetched_materials.json`，
  所以 `jy effects cached` 和 `draft build` 能按名字找到它。
- **结果**：`data.fetched/md5/bytes/files/cache_dir/path/source` + 可粘贴的 `spec`。
- **会员素材**：`vip:true` 照样能下载，但非会员账号导出时剪映可能拒绝（`jy render` 报 `BLOCKED`）；jy 只如实标注，不绕过。
- **失败**：在线目录不可用 → 自动回退本机缓存的列表并给出 warning；下载网络错误 → `UNAVAILABLE`（`retriable:true`）；CDN 401/403 →
  `BLOCKED`；md5 不符/非白名单主机/没有下载地址 → `UNAVAILABLE`/`UNSUPPORTED`（`retriable:false`）；签名地址过期 → `UNAVAILABLE`。
  `details.attempts[]` 逐个列出尝试过的地址和原因。
- 实机验证（剪映 11.6，2026-10-25）：在线搜索并下载 `滚动立方`（会员转场）、`推近`，以及 `梦幻渐变紫边白色发光花字`（花字）、
  `扫光`（文字动画，经 `--fetch`），`draft build` + `jy render` 剪映引擎出片，逐帧可见。

## 10. 工程管理

| 命令 | 说明 |
|---|---|
| `jy draft list \| rename \| duplicate \| delete \| move \| history` | 默认 dry-run，`--apply` 才动；改前备份到 `.jy-backups/`；删除进 `.jy-trash/`；草稿根之外的路径拒绝 |
| `jy asset import \| list \| prune` | jy 自己的素材箱（sha256 去重），默认 dry-run |
| `jy fonts [--check 名字]` | 枚举系统+剪映字体并校验，拼错即失败 |
| `jy clean [--staging\|--assets\|--all] [--apply]` | `~/.cache/jy` 历史行为不变；`~/Movies` 下的 staging/assets **只有 `--apply` 才删** |

**任意路径素材**：`draft build` 默认把 clip 源 hardlink/copy 到 `~/Movies/jy-staging/<hash>/` 并在草稿里引用 staging 路径，所以 `~/Downloads` 直接可用（TCC 保护路径会弹模态框把剪映卡死）。`--no-staging` 关闭，`--staging-dir` 改目录（相对路径按当前工作目录解析）。

**多时间线迁移**：剪映打开过 jy 草稿后会把 `draft_info.json` 迁到 `Timelines/<uuid>/`；`jy` 自动解析（根 → `project.json` 的 `main_timeline_id` → 最新），并在 `data.draft.layout` 里报告 `single`/`multi-timeline`。
