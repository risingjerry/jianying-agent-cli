# `jy sfx` — 本地音效库（收割剪映已下载的音效）

目标：让 agent 在剪辑时**自己判断该不该加音效 → 搜到一条 → 混进片子**，全程不联网、不打开剪映、不用剪映的额度。

## 1. 为什么本地就有音效

在剪映的音效面板里**点一次下载**，剪映会把这条音效作为**普通 MP3** 落到自己的缓存目录：

```
~/Movies/JianyingPro/User Data/Cache/music/<md5>.mp3
```

本机实测三条（2026-10，剪映 6.x）：

| 文件 | 大小 | 时长 | 码率/声道 |
|---|---|---|---|
| `0291b72047769e085e7595ce5d65dbd2.mp3` | 7,568 B | 0.47 s | 128 kbps 立体声 |
| `5bb4c18515e6059da16432af0db0f1dc.mp3` | 5,600 B | 0.34 s | 128 kbps 立体声 |
| `daf3d009e593da8f606c1b329833de2b.mp3` | 2,970 B | 0.37 s | 65 kbps 单声道 |

`ffprobe`/`ffmpeg` 直接能读、能播。一条 1–3 s 的音效约 10–100 KB，几百条也就几 MB。

同一个目录里还有 `downLoadcfg`（JSON），把缓存文件映射到剪映的资源 id：

```json
{"list": [{"cacheType": 3, "path": "5bb4c185….mp3", "resourceId": "6896679322715786510"}]}
```

`jy sfx index` 会读它，把 `resource_id` 记进条目——这是缓存文件唯一的稳定身份（文件名是 md5，没有语义）。

## 2. 两个来源

| 来源 | 路径 | 说明 |
|---|---|---|
| `user` | `$JY_SFX_DIR`，默认 `~/Movies/jy-sfx/`（递归） | 你自己的音效包；**文件名就是搜索名**，`叮-提示音.wav` → 名字 `叮 提示音`，标签 `ding/hint/notification` |
| `cache` | `~/Movies/JianyingPro/User Data/Cache/{music,effect,artistEffect,AITextTemplate}` | 剪映里点过下载的音效；**只读收割**，文件名是 md5，条目先带 `resource_id` |
| `extra` | `jy sfx index --source DIR`（可重复） | 临时挂一个目录 |

索引写在 `~/Movies/jy-sfx/index.json`（schema `jy.sfx.v1`，原子替换），条目：

```json
{"id": "sha256 前 12 位", "path": "...", "name": "叮 提示音", "source": "user",
 "resource_id": "", "duration_s": 0.4, "sample_rate": 44100, "channels": 2,
 "bitrate": 128000, "bytes": 34567, "mtime": 1793000000,
 "tags": ["bell", "ding", "hint", "提示音"], "labelled": false, "user_tags": []}
```

重复音效按音效内容去重：同一份字节既在缓存又在你的目录里时，保留 `user` 那份。

## 3. 命令

```bash
jy sfx index                     # 建/刷新索引（用户目录 + 剪映缓存）
jy sfx index --source ~/Desktop/更多音效
jy sfx search 叮                  # 中文/英文都能搜，返回 id/时长/大小/标签
jy sfx search whoosh --max-duration 1.0 --limit 5
jy sfx list --source cache        # 只看剪映缓存里的
jy sfx label 592be87899cd --name 叮-清脆提示 --tags ding,hint   # 给缓存音效命名（只改索引）
jy sfx forget 592be87899cd        # 从索引里移除（不删文件）
jy sfx add 3aa742bdae1c --at 2.3 -i in.mp4 -o out.mp4 --gain -6 --fade-in 0.05
```

### 检索怎么排序

`score_entry()` 给每条 0..1 分：名字完全相等 1.0 → 名字包含查询 0.8 → 标签相等 0.75 → 标签包含 0.55 →
token 重叠（至少 2 个字符的 token，中文按整段 + 单字拆，英文按词）× 0.25–0.7 → `resource_id` 命中 0.4。
**单个汉字不参与 token 打分**（`音`/`示` 会命中一切），只保留子串匹配。

中文→英文的同义词表（`SYNONYMS`）让「叮」也能被 `ding`/`bell`/`chime`/`hint` 找到：转场→whoosh/transition、
笑→laugh、砰→bang/hit/impact、点击→click、鼓掌→applause、心跳→heartbeat、爆炸→explosion 等 40 多个词。

### 混音怎么做的（`jy sfx add`）

1. 解析选择器：先按 `id` 精确匹配，再按名字精确匹配，最后按检索分 ≥ 0.55 找候选。
   **匹配到多个直接报 `VALIDATION` 并列出候选（id ← 名字）**，不会替你挑一个。
2. ffmpeg 滤镜链（有原声时）：

   ```
   [1:a]volume=<gain>dB,afade=t=in:st=0:d=<fade-in>,afade=t=out:st=<dur-fade-out>:d=<fade-out>,adelay=<ms>|<ms>[sfx];
   [0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]
   ```

   `normalize=0` 保住原声电平（默认 amix 会把两路都压到一半），`duration=first` 让输出长度跟视频一致。
   视频没有音轨时改成 `[1:a]…[aout]` 直接出，并给出 warning。
3. `-c:v copy`（不动画面）+ `-c:a aac -b:a 192k`，`-movflags +faststart`。
4. **回验**：ffprobe 输出（音轨数/时长），再用 `astats` 量音效窗口的 RMS，和原片同一窗口的 RMS 比：
   报告 `verification.window_rms_db` / `original_window_rms_db`；如果没比原声高 0.2 dB，给 warning
   （说明被盖住了，建议 `--gain +6`）。

输出默认拒绝覆盖，`--force` 才行；`--at` 超出视频时长直接 `VALIDATION`。

## 4. `jy doctor`

```json
"sfx": {"index_path": "~/Movies/jy-sfx/index.json", "index_present": true, "indexed": 7,
        "sources": {"user": "...", "cache": ["..."], "extra": []},
        "cache_audio_files": 3, "labelled": 0, "index_mtime": 1793000000}
```

`cache_audio_files` 是**实时**数出来的（不看索引），所以能一眼看出「剪映里又下了一批、该重新 index 了」。

## 5. 边界与合规

* **只读剪映缓存**：`jy` 从不写、不删、不改 `User Data/Cache/**` 里的任何文件；测试里有一条断言会比较
  index 前后缓存文件的集合和 mtime，确保零改动。
* **只写自己的目录**：索引在 `$JY_SFX_DIR/index.json`（默认 `~/Movies/jy-sfx/`）。
* **不联网**、不调用任何 AI、不需要剪映运行。
* 这些音效素材是**剪映缓存资产**：在本机自用没问题，但**不可再分发**（不要打包进仓库、不要上传、不要
  随成片发给第三方素材库）。发布成片时按剪映自身的授权条款处理。
* 明确不做：把音效写进剪映草稿的音轨（`segment:audio` 结构还没从 GUI 里逐字段验证过）。
  现在的链路是「成片后用 ffmpeg 混音」，不依赖剪映草稿格式。
