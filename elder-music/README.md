# elder-music —— 适老版音乐播放 CLI

按**歌名**搜索 → 自动挑「适老」版本（排除 DJ/舞曲/翻唱/伴奏…，优先正规院团/歌唱家）→ 播放。
**可移植**：Linux x86_64 / arm64 / armv7；扬声器由用户自行配置，不写死任何机器路径。

## 依赖

| 依赖 | 说明 |
|---|---|
| `ncm-cli` | `npm i -g @music163/ncm-cli`，并完成 `config set appId/privateKey` + `login` |
| `mpv` | 播放器（`apt install mpv` / `dnf install mpv` / `pacman -S mpv` …） |
| `python3` | 运行本工具（仅标准库，无第三方依赖） |

> `ncm-cli` 需要 Node.js ≥ 18。

## 安装

```bash
# 1. 放到 PATH（示例）
install -m 755 elder-music ~/.local/bin/elder-music

# 2. 生成默认配置
elder-music init

# 3. 选扬声器（列设备 → 设置）
elder-music speakers
elder-music set-speaker None          # 跟随系统默认
# 或
elder-music set-speaker "Analog"      # 唯一子串匹配
elder-music set-speaker "pipewire/alsa_output.pci-0000_xx.analog-stereo"  # 精确设备名
```

## 命令

| 命令 | 作用 |
|---|---|
| `elder-music search "<歌名>"` | 列出适老候选（名称/歌手/评分/链接） |
| `elder-music search "<歌名>" --json` | 同上，机器可读（供封装/agent 调用） |
| `elder-music play "<歌名>"` | 搜索→筛适老→取最佳→播放 |
| `elder-music play "<歌名>" --json` | 同上，返回 `{ok, picked, output}` |
| `elder-music speakers` | 列出本机可用扬声器（含精确设备名） |
| `elder-music set-speaker <值>` | 设置扬声器并应用 |
| `elder-music apply` | 按当前配置重新应用扬声器 |
| `elder-music config` | 显示当前配置 |
| `elder-music init` | 生成默认配置 |

## 配置：`~/.config/elder-music/config.env`

```ini
# 适老过滤规则（逗号分隔，可自由增删）
ELDER_EXCLUDE=DJ,舞曲,电音,慢摇,抖音,广场舞,加速,翻唱,cover,伴奏,纯音乐,0.8,1.5
ELDER_PREFER=中央乐团,中国交响乐团,军乐团,合唱团,韩红,王菲,廖昌永,德德玛,郭兰英
ELDER_LIMIT=50

# 扬声器：None=系统默认 | <唯一子串> | <精确设备名>
SPEAKER_DEVICE_NAME=None
```

- **`ELDER_EXCLUDE`**：命中即丢弃（名字里含这些词就不算适老）。
- **`ELDER_PREFER`**：命中加分（正规院团/知名歌唱家优先）。
- 排序还综合：完整时长优先 + 无损/高解析优先；且只保留 `visible:true`（有音源的）版本。

## 工作原理

```
elder-music play "南泥湾"
   │ ncm-cli search song --keyword 南泥湾 --output json
   │ 过滤 visible:true + 歌名匹配 + 排除 ELDER_EXCLUDE
   │ 按 ELDER_PREFER / 时长 / 码率 排序取最佳
   │ 解析 SPEAKER_DEVICE_NAME → 写 ~/.config/elder-music/mpv-home/mpv.conf
   │ 设 MPV_HOME 后调用: ncm-cli play --song --encrypted-id <enc> --original-id <orig>
   ▼
ncm-cli daemon → 网易云 OpenAPI 取音源 URL → mpv(读 mpv.conf 的 audio-device) → 声卡
```

## 在 ARM 上部署的注意点

1. **音频后端**：ARM 板常见纯 **ALSA**（无 PipeWire/Pulse）。本工具用 `mpv --audio-device=help`
   动态枚举设备，所以 ALSA/Pulse/PipeWire 都能识别；只是设备名不同，用 `elder-music speakers` 看后设置。
2. **`ncm-cli` 的 ffprobe**：`ffprobe-static@3.1.0` **没有 linux/arm64 二进制**（只有 ia32/x64）。
   搜索/播放通常不需要它；若某命令报 ffprobe 相关错误，可安装系统 ffprobe 并软链到
   `.../node_modules/ffprobe-static/bin/linux/arm64/ffprobe`。
3. **Node**：确认 `node -v` ≥ 18（arm64 装 Node 官方二进制或发行版包）。

## 故障排查

| 现象 | 排查 |
|---|---|
| `找不到 ncm-cli` | 确认已 `npm i -g @music163/ncm-cli` 且在 PATH |
| 播放没声音 | ① `elder-music speakers` + `set-speaker` 选对设备；② 该 sink 是否静音（`wpctl`/`alsamixer`） |
| 没有适老候选 | 该歌在开放平台无音源（`visible:false`），或排除词过滤过狠（调 `ELDER_EXCLUDE`） |
| 换扬声器没生效 | 工具会自动重启播放服务；若仍不行，`kill $(cat ~/.config/ncm-cli/daemon.pid)` 后重放 |
| 登录失效 | `ncm-cli login --background` 重新授权 |
