# elder-music · ARM Linux 部署指南

> 目标：在一台 ARM 架构 Linux 上部署「按歌名搜索 → 选适老版本 → 播放」的 CLI。
> 全程复制粘贴即可。每步都给了「apt / dnf / pacman / apk」分支，按你的发行版选。

---

## 0. 先做只读检查（不改任何东西）

```bash
{
echo "===== 系统 ====="
cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|ID|VERSION)='
uname -m; uname -a
echo "===== 包管理器 ====="
for c in apt dnf yum pacman apk zypper; do command -v $c >/dev/null && echo "有: $c"; done
echo "===== Node / npm ====="
node -v 2>/dev/null || echo "无 node"; npm -v 2>/dev/null || echo "无 npm"
echo "===== python3 ====="; python3 --version 2>/dev/null || echo "无 python3"
echo "===== mpv ====="; mpv --version 2>/dev/null | head -1 || echo "未安装 mpv"
echo "===== 音频后端 ====="
for c in pipewire pulseaudio wpctl pactl aplay; do command -v $c >/dev/null && echo "有: $c"; done
echo "===== 声卡 ====="; aplay -l 2>/dev/null
echo "===== mpv 音频设备 ====="; mpv --audio-device=help 2>/dev/null
} 2>&1 | tee /tmp/arm-check.txt
```

把 `/tmp/arm-check.txt` 留好，后面配扬声器要用到 `mpv 音频设备` 那段。

---

## 1. 安装 Node.js（≥ 18）

### 方案 A：发行版包（简单，但版本可能偏低）

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt update && sudo apt install -y nodejs npm

# Fedora
sudo dnf install -y nodejs npm

# Arch / Manjaro
sudo pacman -S --noconfirm nodejs npm

# Alpine
sudo apk add nodejs npm

# openSUSE
sudo zypper install -y nodejs npm
```

### 方案 B：nvm（推荐，任意发行版、无需 root、版本可控）

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install --lts
```

### 验证（必须 ≥ 18）

```bash
node -v && npm -v
```

> 若 `node -v` 低于 v18：用方案 B 的 nvm 装一遍。

---

## 2. 安装 ncm-cli

```bash
npm install -g @music163/ncm-cli
ncm-cli --version
```

### 若提示 `ncm-cli: command not found`（npm 全局 bin 不在 PATH）

```bash
# 把 npm 全局 bin 加进 PATH（永久）
echo "export PATH=\"$(npm config get prefix)/bin:\$PATH\"" >> ~/.bashrc
source ~/.bashrc
ncm-cli --version
```

---

## 3. 安装 mpv 和 python3

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt install -y mpv python3

# Fedora
sudo dnf install -y mpv python3

# Arch / Manjaro
sudo pacman -S --noconfirm mpv python3

# Alpine
sudo apk add mpv python3

# openSUSE
sudo zypper install -y mpv python3
```

验证：

```bash
mpv --version | head -1
python3 --version
```

---

## 4. 配置凭证 + 登录

> 用你申请好的**同一个** appId / privateKey（网易云开放平台）。

```bash
ncm-cli config set appId <你的AppId>
ncm-cli config set privateKey <你的PrivateKey>
ncm-cli config set player mpv

# 确认写入
ncm-cli config list

# 登录（后台模式，会输出一个链接，点开授权）
ncm-cli login --background
# 授权完成后检查
ncm-cli login --check
```

---

## 5. 部署 elder-music 工具

### 5.1 把工具文件传到 ARM 机器

在你**当前这台电脑**上执行（改成 ARM 机器的用户名/IP）：

```bash
scp "/home/mxy/Desktop/音乐播放/elder-music/elder-music" <用户>@<ARM_IP>:~/
```

如果两台机器没有互通，就用 U 盘/网盘把 `elder-music` 这个文件拷过去。

### 5.2 在 ARM 机器上安装到 PATH

```bash
mkdir -p ~/.local/bin
install -m 755 ~/elder-music ~/.local/bin/elder-music

# 确保 ~/.local/bin 在 PATH（多数发行版已默认包含；没有就加）
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

elder-music --help
```

### 5.3 生成默认配置

```bash
elder-music init
elder-music config
```

---

## 6. 配置扬声器（用户自定义）

先看这台机器有哪些设备：

```bash
elder-music speakers
```

然后三选一设置：

```bash
# ① 跟随系统默认（最省事，推荐先用这个）
elder-music set-speaker None

# ② 用设备名里的唯一子串（如 USB / HDMI / Analog / 某个 card 名）
elder-music set-speaker "Analog"

# ③ 用 mpv 的精确设备名（从 `elder-music speakers` 里复制）
elder-music set-speaker "alsa/plughw:CARD=Headphones,DEV=0"
```

> 设置会立即写入配置并重启播放服务，下次播放生效。
> 想看/改配置：`elder-music config` 或直接编辑 `~/.config/elder-music/config.env`。

### 若没声音

```bash
# PipeWire 用户：查默认输出、解静音、加音量
wpctl status
wpctl set-mute @DEFAULT_AUDIO_SINK@ 0
wpctl set-volume @DEFAULT_AUDIO_SINK@ 70%

# ALSA 用户：用 alsamixer 检查是否静音
alsamixer
```

---

## 7. 验证播放

```bash
# 搜索，看有没有「适老」候选
elder-music search "南泥湾"

# 直接播放（自动挑适老版本）
elder-music play "南泥湾"

# 查看播放状态
ncm-cli state
```

`play` 成功时会打印：
```
🎵 选中: 南泥湾 - 中央乐团合唱团  (评分 ...)
▶️  正在播放 | https://music.163.com/#/song?id=1304301128
```

---

## 8. 常见问题 / 排错

| 现象 | 原因 / 解决 |
|---|---|
| `找不到 ncm-cli` | 第 2 节的 PATH 没配好；重跑那段 |
| `找不到 mpv` | 第 3 节没装成功 |
| 播放没声音 | ① `elder-music speakers` + `set-speaker` 选对设备；② 该设备是否静音（第 6 节） |
| `没有找到 ... 的适老可播版本` | 该歌在开放平台无音源（`visible:false`）；或 `ELDER_EXCLUDE` 过滤太狠（放宽它） |
| 换扬声器没生效 | 工具会自动重启播放服务；仍不行就 `kill $(cat ~/.config/ncm-cli/daemon.pid)` 后重放 |
| 登录失效 | `ncm-cli login --background` 重新授权 |
| **报 ffprobe 相关错误** | 见下方「ffprobe on ARM」 |
| `ncm-cli` 启动即报错/退出 | 见下方「ffprobe on ARM」，或 `node -v` 是否 <18 |

### ffprobe on ARM（`ffprobe-static` 没有 arm64 预编译二进制）

`ncm-cli` 依赖的 `ffprobe-static@3.1.0` 只带 `linux/ia32`、`linux/x64`，**没有 `linux/arm64`**。
如果搜索/播放正常，可忽略；一旦报 ffprobe 错误，用系统 ffprobe 软链兜底：

```bash
# 装系统 ffprobe（一般随 ffmpeg）
sudo apt install -y ffmpeg        # Debian/Ubuntu；其它发行版换成对应包

# 找到 ffprobe-static 目录并补一个 arm64(或 arm) 二进制
FS="$(npm root -g)/@music163/ncm-cli/node_modules/ffprobe-static"
ARCH_DIR="$FS/bin/linux/arm64"     # 32 位 ARM 用 linux/arm
mkdir -p "$ARCH_DIR"
ln -sf "$(command -v ffprobe)" "$ARCH_DIR/ffprobe"
"$ARCH_DIR/ffprobe" -version | head -1
```

---

## 9. 附录

### 9.1 适老过滤规则（可自由修改）

配置文件：`~/.config/elder-music/config.env`

```ini
# 排除：命中即丢弃
ELDER_EXCLUDE=DJ,舞曲,电音,慢摇,抖音,广场舞,加速,翻唱,cover,伴奏,纯音乐,0.8,1.5
# 优先：命中加分（正规院团/歌唱家）
ELDER_PREFER=中央乐团,中国交响乐团,军乐团,合唱团,韩红,王菲,廖昌永,德德玛,郭兰英
# 搜索条数
ELDER_LIMIT=50
# 扬声器
SPEAKER_DEVICE_NAME=None
```

### 9.2 elder-music 命令表

| 命令 | 作用 |
|---|---|
| `elder-music play "<歌名>"` | 搜索→筛适老→取最佳→播放 |
| `elder-music play "<歌名>" --json` | 同上，返回 `{ok, picked, output}` |
| `elder-music search "<歌名>" [--json]` | 列出适老候选 |
| `elder-music speakers` | 列出可用扬声器 |
| `elder-music set-speaker <值>` | 设置并应用扬声器 |
| `elder-music apply` | 按配置重新应用扬声器 |
| `elder-music config` | 显示当前配置 |
| `elder-music init` | 生成默认配置 |

### 9.3 关键路径

| 内容 | 路径 |
|---|---|
| elder-music 工具 | `~/.local/bin/elder-music` |
| 适老规则/扬声器配置 | `~/.config/elder-music/config.env` |
| 生成的 mpv 配置 | `~/.config/elder-music/mpv-home/mpv.conf` |
| ncm-cli 凭证/登录 | `~/.config/ncm-cli/` |

### 9.4 一键自检脚本（可选）

```bash
echo "node   : $(node -v 2>/dev/null)"
echo "ncm-cli: $(command -v ncm-cli >/dev/null && ncm-cli --version || echo 未安装)"
echo "mpv    : $(mpv --version 2>/dev/null | head -1)"
echo "python : $(python3 --version 2>/dev/null)"
echo "登录   : $(ncm-cli login --check 2>/dev/null | tr -d '\n')"
echo "扬声器 : $(elder-music config 2>/dev/null | grep 扬声器)"
```
