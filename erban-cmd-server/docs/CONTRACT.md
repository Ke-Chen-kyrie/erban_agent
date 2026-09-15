# cmd_server 运行契约

> 接口、鉴权、白名单、执行语义 —— 接它的人 (写 agent 的、调接口的、排障的) 看这份。加命令看 [../README.md](../README.md), 结构/决策看 [STRUCTURE.md](STRUCTURE.md)。

## 是什么

命令执行服务。FastAPI + gunicorn, 2 worker (uvicorn worker), host 网络, 监听 `0.0.0.0:<server.port>`。

对外只做一件事: 收一个命令串, 校验白名单, 跑, 同步回输出。**没有裸 shell** —— 能跑什么由白名单定死 (见下)。

镜像进 `cmd_server/` 本体; `config/` 与 `runtime/` 是挂载卷, 改命令热更免重建 (见 [STRUCTURE.md](STRUCTURE.md))。

## 接口

| 端点 | 方法 | 入参 | 出参 |
|---|---|---|---|
| `/health` | GET | — | `{"status":"ok","command_count":N}` |
| `/list` | GET | — | `{"success":true,"commands":[{"name","description"...}],"refresh":{added,removed,changed,command_count,errors}}`。**副作用 = 重扫命令** |
| `/run` | POST | `{"cmd":"<命令串>"}` | 200 `{"success":bool,"output":str}`; 400 校验失败 `{"success":false,"error":str}` |
| `/catalog` | GET | — | 人类可读命令目录页 (不进 Swagger) |

`/run` 的 400 只有两类: 命令解析失败 (非法操作符/空段)、命令不在白名单。IP 不在白名单是 **403**。

## 网络与鉴权

- **端口是配置项, 不是契约**: `config/config.yaml` 的 `server.port` (当前 **8088**), 改端口重启容器即可, 不用重建镜像。改后记得同步调用的 agent。
- host 网络, 监听地址即容器所在主机的 IP。
- **IP 白名单** `config.yaml` 的 `allow_ips`, 缺省全拒; `127.0.0.1` 恒兜底。当前含 `192.168.217.100` / `192.168.217.0/24`。调用方换机部署要把它加进去, 否则全接口 403 `{"success":false,"error":"来源 IP 不在白名单: <ip>"}`。
- **无 token / 无密钥**, 鉴权只有 IP 白名单。改 `allow_ips` 必须重启容器 (启动读一次)。

## 白名单与执行语义

白名单 = **两源合并**, 缺一不可:

1. **扫描命令**: `runtime/<client>/commands/*.py` 的文件名 (命令名 = 文件名, 全仓唯一)
2. **系统命令**: `config.yaml` 的 `system_commands` (基础 shell 工具: `ls` `cat` `grep` `head` `sort` ...)

首 token 不在这两源里 → 400 `{"success":false,"error":"命令不在白名单: <名>"}`, 不退化为 shell。

`cmd` 支持**有限管道链**, 语法同 shell 的子集:

| 支持 | 语义 |
|---|---|
| `\|` | stdin/stdout 相接, 真管道 |
| `;` | 顺序执行, 不管前一条成败 |
| `&&` | 前一条成功才跑 |
| `\|\|` | 前一条失败才跑 |

- **每段首 token 各自过白名单**, 任一段不在 → 整条 400, **前半段也不会跑**。拿不到白名单外的能力, 只是省了多轮往返。
- **拒绝**: 重定向 `> >> < <<`、后台 `&`、子 shell `( )` / `$( )`。反引号是字面文本, 不执行。
- 全程 `shell=False`, 无变量展开/通配符/命令替换。引号按 shell 规则解析: `echo "a|b"` 里的竖线是字面参数, `--text 'a b'` 是一个参数。
- 超时是**整条链的总预算** (不是每段一份)。
- `success` = 实际跑过的管道**末段**退出码全 0。所以 `ls | grep 无匹配` 是 `success:false` (grep 退出码 1), 这是 shell 语义。

## 行为语义

- **同步阻塞**: HTTP 响应即执行结果。物理动作 (转向/伸手/喂食) 期间连接一直挂着, 直到机器人返回或超时。
- **一次一个动作**: 调用方应发一条、等回复、再发下一条。服务端允许并行发, 并发/乱序的拦截在**机器人侧**。
- `output` 是命令的 stdout + stderr 合并 (管道链里中间段的 stdout 已喂给下一段, 收的是各段 stderr + 末段 stdout)。
- 包装脚本 `/usr/local/bin/<name>` (`exec python3 <py> "$@"`) 在 entrypoint 启动时和每次 `/list` 重生成 —— 所以加了新命令要先触发一次重扫。

## 命令表

**唯一真源 = `/catalog` 页面**, 每次请求按当前注册表现场生成。浏览器打开 `http://<主机>:<port>/catalog` 看命令/参数/示例/choices, 顶部可选列和格式, 生成可复制的 Markdown 表。

**不要手写维护命令清单** —— 任何文档里的命令表都会过期。给 LLM 用的命令表从 catalog 复制, 每次改完命令重新粘一份。

`/list` 只回命令名 + 一行描述 (给机器发现用), **不含参数 schema**; 要参数就得看 `/catalog`。

## 验证

```bash
BASE=http://<主机>:<port>
curl -s $BASE/health          # {"status":"ok","command_count":N}
curl -s $BASE/list            # 命令清单; 同时触发重扫
curl -s -X POST $BASE/run -H 'Content-Type: application/json' -d '{"cmd":"cur-time"}'
# 管道链 (每段各自过白名单)
curl -s -X POST $BASE/run -H 'Content-Type: application/json' -d '{"cmd":"ls /config | head -3"}'
# 负向: 不存在的命令 → 400 (注意 ls/cat 这些在 system_commands 里, 是合法的, 不是负向用例)
curl -s -X POST $BASE/run -H 'Content-Type: application/json' -d '{"cmd":"no-such-command"}'
# 负向: 白名单外的段 → 整条 400, 前半段不跑
curl -s -X POST $BASE/run -H 'Content-Type: application/json' -d '{"cmd":"ls | rm -rf /tmp/x"}'
# 负向: 重定向 → 400
curl -s -X POST $BASE/run -H 'Content-Type: application/json' -d '{"cmd":"ls > /tmp/x"}'
```

`Content-Type` 必须带, 否则 FastAPI 回 422 (参数校验错), 不是 400。
