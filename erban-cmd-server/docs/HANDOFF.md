# Handoff: 执行工具从 cli_server 切换到 cmd_server

> 接替本模块的维护者参照。本文解释 proactive_agent 如何从旧的 `cli_server`(Flask, 直接 shell 执行) 切换到新的 `cmd_server`(FastAPI, 命令白名单 + IP 白名单)。只看本文件 + 内嵌 skill 即可上手。

## 背景

旧执行工具: `http://192.168.217.100:8088/run` (docker 端口映射 8088→容器8080), 把 LLM 给的命令字符串 shell 执行, 无鉴权、无命令白名单、无发现接口。

新执行工具: `cmd_server` (`agent/cmd_server/`), FastAPI + gunicorn, host 网络, 监听 `0.0.0.0:<server.port>`。**端口是配置项, 不是契约**: 在 `agent/cmd_server/config/config.yaml` 的 `server.port`(当前值为 **8081**, 暂定, 随时可改; 改端口重启容器即可, 不用重建镜像)。下面的地址均按当前 8081 演示 —— 实际接的时候以 cmd_server 实际的 `server.port` 为准, 别写死。同一份 `/run` 契约: `POST {"cmd": "..."}` → `{"success": bool, "output": str}`。

## 唯一必改: 端点地址

`config.py` 缺省值和部署环境的 `.env` 里都有一份, 两处都改成新地址(只换端口, 主机名不变):

```python
# config.py
SHELL_PROXY_URL = os.getenv("SHELL_PROXY_URL", "http://192.168.217.100:8081/run")
```

```bash
# .env  (环境覆盖优先于代码缺省)
SHELL_PROXY_URL=http://192.168.217.100:8081/run
```

改完重启 proactive_agent 进程。

## 契约核对: 客户端代码不用动

`agent/tools.py` 的 execute 工具已兼容, 逐条核对:

| 旧行为 | 新行为 | 是否要改 |
|---|---|---|
| `POST {"cmd": command}` | 同 | 否 |
| 读 `resp.json().get("output")/get("success")` | 同字段 | 否 |
| HTTP 200 + `success=false` → 报错 | 同 | 否 |
| 非 200 → 报 `(Status: N)` + body | cmd_server 会回 400/403, body 是错误 JSON, 逻辑照走 | 否 |
| `SHELL_PROXY_TIMEOUT=180` 超时 | 命令同步阻塞至机器人返回, 180s 足够 | 否 |
| 客户端 `dangerous_patterns` 黑名单 | cmd_server **另加**服务端白名单, 双保险 | 否(保留) |
| `_looks_like_error(output)` 拦截 Traceback | cmd_server output 仍是命令 stdout/stderr, 照拦 | 否 |

结论: **tools.py 零改动**, 只有 `SHELL_PROXY_URL` 一处地址。

## 网络与鉴权

- cmd_server host 网络, 监听 `0.0.0.0:8081`(即运行该容器的主机 IP:8081)。
- **IP 白名单** `config.yaml` 的 `allow_ips` 缺省全拒; 当前已含 `127.0.0.1`、`192.168.217.100`、`192.168.217.0/24`。proactive_agent 运行主机必须落在白名单里, 否则 `/run` 回 **403** `{"success":false,"error":"来源 IP 不在白名单: ..."}`。agent 若换机部署, 记得往 `allow_ips` 加它的 IP。
- 无 token/密钥, 只有 IP 白名单。

## 命令集变化: LLM 实际能调的命令

cmd_server 白名单 = 扫描到的命令文件名 + `system_commands`。**不在白名单的命令名 → 400** `{"success":false,"error":"命令不在白名单: <名>"}`, 不再退化为直接 shell。旧 cli_server 依赖的命令名有改名/粒度变化:

| 旧 cli_server | 新 cmd_server | 说明 |
|---|---|---|
| `turn -t user/table` | `turn-to --target/-t user/table` | 改名, flag 一致 |
| `navigate-to-person -u <id> [-d front/..]` | `move-to-person -u <id> [-d front/back/left/right]`(默认 front) | 改名 |
| `cur_time` | `cur-time` | 改名 |
| `water start/more/stop` | `lower-cup` / `deliver-cup -u <id>` / `retract-cup` | 去掉 start/stop 包装, 粒度化 |
| `food start/more/stop` | `scoop` / `deliver-spoon -u <id>` / `retract-spoon` | 同上 |
| `pick/place cup\|bowl`、`get-status`、`ident`、`search`、`weather` | 同名 | 保留 |
| `snap` | 不存在 | 已移除 |
| (新增) | `move-step`、`rotate-step`、`extend-hand`、`raise-hand`、`lower-hand`、`retract-hand`、`list-trace`、`exec-trace` | 新能力 |

**命令表唯一真源 = `/catalog`**: 浏览器开 `http://<cmd_server主机>:8081/catalog` 看人类可读命令表(含参数/示例/choices, 可勾选列再复制)。`/list` 返回所有命令名+一行描述, 供机器读。**不要手写维护命令清单** —— 加命令文件后 `/catalog` 自动更新; 加了新命令先触发重扫(点 catalog 顶部"重扫命令"或调一次 `/list`)。

## 行为/安全语义提醒

- `/run` **同步阻塞**: HTTP 响应即执行结果; 物理动作(转向/伸手/喂食)期间连接保持, 超时 `SHELL_PROXY_TIMEOUT`。LLM 侧应一次只发一个动作、等回复再发下一个。
- 服务端二次校验: 合法的命令名+参数, 参数仍需 LLM 填对(见 skill 命令表)。
- 客户端 `dangerous_patterns` 建议保留(纵深防御), 即便服务端已从根上杜绝裸 shell。

## 验证

```bash
# 健康检查
curl -s http://192.168.217.100:8081/health
# 发现命令(机器可读)
curl -s http://192.168.217.100:8081/list
# 跑一条(应成功, 回 {"success":true,"output":...})
curl -s -X POST http://192.168.217.100:8081/run -H 'Content-Type: application/json' \
  -d '{"cmd":"get-status"}'
# 不在白名单 → 400
curl -s -X POST http://192.168.217.100:8081/run -d '{"cmd":"ls -la /"}'
```
(注意: 看 400/403 做负向测试时用浏览器/本机 IP, 别把系统命令误当可用命令给 LLM 用。)

---

## 嵌入 skill: 命令调用规范 (给 LLM agent 用)

> 下面是按 proactive_agent 现有 `agent/skills/<name>/SKILL.md` 格式写的完整技能 (frontmatter `name`/`description` + markdown 正文), 由 `agent/skill_manager.py` 的 `SkillLoader` 扫描 `**/SKILL.md` 自动加载: brief 注入 system prompt, 按需取正文。落地就建 `agent/proactive_agent/agent/skills/command_reference/SKILL.md`。命令表以 `/catalog` 为准, 本 skill 给稳定不变项 + 用法约束。

````markdown
---
name: command-reference
description: 机器人命令调用规范 (cmd_server 白名单执行, 命令表 + 安全感约束)
---
# 机器人命令调用参考

## 基本规则
1. 每个动作是**一次性同步动作**: 发一条命令、等返回、再发下一条。物理动作执行中不要并发发命令。
2. 只调用下面命令表里的命令 + 参数。命令不在表 → 该命令不存在或已改名, 不要猜, 去查 catalog (http://<cmd_server主机>:<server.port>/catalog)。
3. 命令名/参数与机器人端一一对应; choices 只填列出的值 (`user|table`、`front/back/left/right`、`cup|bowl` 等)。
4. `<>`=必填值, `[ ]`=可选段; 可选参不填用默认, 填了覆盖默认。
5. **禁止**: 反引号/`$()` 命令替换、重定向、管道组合、`sudo`、`rm -rf`、`dd`、`chmod`, 以及任何非命令表的裸 shell —— 都会被拒, 且是危险动作。
6. 返回 `success=false` 或非 200: 读 `output`, 按错误修正参数重试, 不要盲目重发物理动作。

## 命令参考

### 导航/朝向
| 命令 | 说明 |
|---|---|
| `get-status` | 查机器人/底盘状态 |
| `turn-to -t <user\|table>` | 转向用户或桌子 (user=面向用户, table=面向桌子) |
| `move-to-person -u <用户ID> [-d front\|back\|left\|right]` | 导航到指定人员面前 (默认 front) |
| `move-step -d <front/back/left/right> [-l 语义词] [--distance 米]` | 底盘平移一步 (level 定距, distance 覆盖为调试用) |
| `rotate-step -d <left/right> [-l 语义词] [--angle-deg 度]` | 底盘原地转向 (level 定角度, angle-deg 覆盖为调试用) |

### 上肢/交互
| 命令 | 说明 |
|---|---|
| `pick -t <cup\|bowl>` / `place -t <cup\|bowl>` | 抓/放水杯或碗勺 |
| `extend-hand [-o 高度偏移] [--hand both\|left\|right]` | 伸单手/双臂 |
| `raise-hand [-l 语义词]` / `lower-hand [-l 语义词]` / `retract-hand` | 抬/放/收手 |
| `list-trace` | 列出可用轨迹 |
| `exec-trace -t <轨迹名> [-s 幅度] [--speed 速度] [-r 次数]` | 执行康复轨迹 (0=跳过本次) |

### 喂食
| 命令 | 说明 |
|---|---|
| `scoop` / `deliver-spoon -u <用户ID>` / `retract-spoon` | 舀/递勺/收勺 |
| `lower-cup` / `deliver-cup -u <用户ID>` / `retract-cup` | 放杯/递杯/收杯 |

### 信息/识别
| 命令 | 说明 |
|---|---|
| `ident -u <用户ID>` | 人脸识别核对用户 |
| `search <词...>` | 联网搜索 (多词空格拼合) |
| `weather <城市...>` | 查天气 |
| `cur-time` | 当前时间 |

## 发命令前自问
- 命令名在白名单表里吗?
- 参数 choices 填对了吗?必填给了吗?
- 这是物理动作吗?会不会一次发多个动作?
````

> 迁移提醒: 现有 `agent/skills/feed_food|feed_water|rehab_exercise/SKILL.md` 仍用 cli_server 旧名 (`turn`、位置参 `pick bowl`) — 命令名/参数需按上表对齐 (turn→`turn-to -t`、`pick bowl`→`pick -t bowl` 等)。