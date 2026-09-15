"""FastAPI 入口: /health /list /run 三接口, 全接口 IP 白名单 (缺省全拒).

config 启动读一次, 生命周期不变. 同步 def 端点 (subprocess 阻塞跑线程池).
/catalog 人类可读命令目录 (不进 Swagger, include_in_schema=False), 附可复制命令表.
"""

from __future__ import annotations

import html as _html
from itertools import combinations
import json
import os
from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi import Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from . import load_config
from . import runner
from .registry import Command
from .registry import Registry

_lock = threading.Lock()

STATIC_DIR = Path(__file__).parent / "static"  # 自托管 swagger-ui (离线, 不依赖 CDN)


def _bin_dir() -> Path:
    """包装脚本输出目录, 本地测试用 CMD_BIN 覆盖."""
    return Path(os.environ.get("CMD_BIN", "/usr/local/bin"))


class RunRequest(BaseModel):
    cmd: str


def _flag_for(p: dict, mode: str) -> str:
    """参数旗标展示. 位置参数显示裸名; 选项按 mode: full 长短 / long 长 / short 短 (无别名退长)."""
    if p.get("positional"):
        return str(p["name"])
    alias = p.get("alias")
    long_flag = f"--{p['name']}"
    if mode == "long":
        return long_flag
    if mode == "short":
        return f"-{alias}" if alias else long_flag
    return f"-{alias}/{long_flag}" if alias else long_flag


def _param_meta(p: dict) -> list[str]:
    """参数元信息 (必填/类型/choices/默认), 人类可读."""
    meta = []
    if p.get("required"):
        meta.append("必填")
    if p.get("type"):
        meta.append(p["type"])
    if p.get("choices"):
        meta.append("[" + "/".join(p["choices"]) + "]")
    if p.get("default") is not None:
        meta.append(f"默认 {p['default']}")
    return meta


def _param_text(p: dict, mode: str) -> str:
    """单参数单行描述 (命令表用)."""
    parts = [f"`{_flag_for(p, mode)}`"] + _param_meta(p)
    text = " ".join(parts)
    if p.get("help"):
        text += f" - {p['help']}"
    return text


def _example(c: Command, mode: str) -> str:
    """命令用法示例: 必填必给, 可选 `[--flag 默认]`."""
    parts = [c.name]
    for p in c.params:
        if p.get("positional"):
            parts.append(f"<{p['name']}>")
            continue
        flag = _flag_for(p, mode)
        if p.get("required"):
            placeholder = "<" + ("/".join(p["choices"]) if p.get("choices") else "值") + ">"
            parts.append(f"{flag} {placeholder}")
        elif p.get("default") is not None:
            parts.append(f"[{flag} {p['default']}]")
        else:
            parts.append(f"[{flag}]")
    return " ".join(parts)


def _grouped(commands: list[Command]) -> dict[str, list[Command]]:
    """按客户端库分组 (feature_dir 目录名)."""
    groups: dict[str, list[Command]] = {}
    for c in commands:
        groups.setdefault(c.feature_dir.name, []).append(c)
    return groups


_COLUMNS = ("描述", "参数", "示例")  # 命令列恒在, 这三列可 checkbox 勾显


def _markdown_text(commands: list[Command], mode: str, *, cols: tuple[str, ...]) -> str:
    """命令表 Markdown (复制到 agent 的调用规范里), 按库分组. cols 决定展示哪些列 (命令恒在)."""
    header = "| 命令 |" + "|".join(c for c in cols) + "|"
    rule = "|" + "---|" * (1 + len(cols))
    blocks: list[str] = []
    for lib in sorted(_grouped(commands)):
        lines = [f"## {lib}", "", header, rule]
        for c in sorted(_grouped(commands)[lib], key=lambda c: c.name):
            cell = {
                "描述": c.description,
                "参数": "<br>".join(_param_text(p, mode) for p in c.params) if c.params else "",
                "示例": f"`{_example(c, mode)}`",
            }
            lines.append("| " + " | ".join([c.name] + [cell[col] for col in cols]) + " |")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _col_key(on_cols: tuple[str, ...]) -> str:
    """列的布尔键 (如 full-101: 描述开/参数关/示例开) → data 键."""
    return "".join("1" if c in on_cols else "0" for c in _COLUMNS)


def _browse_html(commands: list[Command], test_enabled: bool) -> str:
    """浏览区 HTML: 按客户端库分组, 每行命令/描述/参数/示例 (+可选"测"按钮填命令骨架到测试框)."""
    header = "<tr><th>命令</th><th>描述</th><th>参数</th><th>示例</th></tr>"
    parts: list[str] = []
    for lib in sorted(_grouped(commands)):
        parts.append(f"<h3>{_html.escape(lib)}</h3>")
        rows: list[str] = []
        for c in sorted(_grouped(commands)[lib], key=lambda c: c.name):
            params_html = (
                "<ul>" + "".join(f"<li>{_html.escape(_param_text(p, 'full'))}</li>" for p in c.params) + "</ul>"
                if c.params
                else ""
            )
            # data-cmd 存骨架 (短参形式, 含必填占位与可选 [--flag]); 点命令名填进测试输入框
            skeleton = _html.escape(_example(c, "short"), quote=True)
            name_html = (
                f'<code><button type=button class=load-cmd data-cmd="{skeleton}" '
                "style=\"border:none;background:none;padding:0;font:inherit;cursor:pointer;color:#0645ad;"
                "text-decoration:underline\">"
                f"{_html.escape(c.name)}</button></code>"
            ) if test_enabled else f"<code>{_html.escape(c.name)}</code>"
            rows.append(
                "<tr>"
                f'<td style="white-space:nowrap">{name_html}</td>'
                f"<td>{_html.escape(c.description)}</td>"
                f"<td>{params_html}</td>"
                f"<td><code>{_html.escape(_example(c, 'full'))}</code></td>"
                "</tr>"
            )
        parts.append(
            '<div style="overflow-x:auto">'
            "<table border=1 cellspacing=0 cellpadding=6 style=\"width:100%\">"
            + header
            + "".join(rows)
            + "</table></div>"
        )
    return "".join(parts)


def create_app() -> FastAPI:
    config = load_config()
    registry = Registry(config, _bin_dir())
    registry.rescan()  # boot 扫描

    app = FastAPI(title="cmd_server", docs_url=None, redoc_url=None)  # 自带 /docs 指 CDN; 改自托管

    # 自托管 swagger-ui 静态资源 (机器人离线可用; 缺 static 目录则跳过, 本地裸跑也不崩)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    def swagger_docs() -> HTMLResponse:
        """Swagger UI (自托管 swagger-ui-dist, 不拉 CDN)."""
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
            swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
        )

    @app.middleware("http")
    async def ip_whitelist(request: Request, call_next):
        ip = request.client.host if request.client else ""
        if not config.is_allowed_ip(ip):
            return JSONResponse(
                status_code=403,
                content={"success": False, "error": f"来源 IP 不在白名单: {ip}"},
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "command_count": len(registry.commands)}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """根路径直接跳命令目录页."""
        return RedirectResponse("/catalog")

    @app.get("/list")
    def list_commands() -> dict:
        with _lock:
            refresh = registry.rescan()
        return {
            "success": True,
            "commands": registry.list_commands(),  # 详情已含 name; 命令集直接取 [].name, 不再单列 whitelist
            "refresh": refresh,
        }

    @app.get("/catalog", include_in_schema=False)
    def catalog() -> HTMLResponse:
        """人类可读命令目录页 (不进 Swagger; 命令集是 /run 的参数内容).

        顶部: 复制格式 (全/仅长参/仅短参) + 描述/参数/示例列 checkbox. 命令表先进文本
        框 (自动刷新), 选中即原生复制 (无 clipboard API 依赖, 兼容手机平板).
        """
        commands = list(registry.commands.values())
        data = {
            f"{mode}-{_col_key(cols)}": _markdown_text(commands, mode, cols=cols)
            for mode in ("full", "long", "short")
            for n in range(4)
            for cols in combinations(_COLUMNS, n)
        }
        data.update(
            {f"{mode}-111": _markdown_text(commands, mode, cols=_COLUMNS) for mode in ("full", "long", "short")}
        )

        test_panel = (
            ""
            if not config.catalog_enable_test
            else (
                "<h2>测试命令</h2>"
                '<p>点击浏览表首列, 把命令骨架填进输入框: 替换占位<值>, 选择[可选段]后, 再点击发送。'
                "点击发送即执行物理动作并同步等待结果, 等待期间勿关闭页面。</p>"
                "<textarea id=cmd-out rows=8 readonly wrap=off "
                "style=\"width:100%;font-family:ui-monospace,'Cascadia Mono',Consolas,monospace\"></textarea>"
                "<textarea id=cmd-line rows=2 wrap=off "
                "style=\"width:100%;font-family:ui-monospace,'Cascadia Mono',Consolas,monospace\"></textarea>"
                "<button type=button onclick=sendCmd()>发送</button>"
            )
        )
        test_script = (
            ""
            if not config.catalog_enable_test
            else (
                "document.querySelectorAll('.load-cmd').forEach(function(b){b.onclick=function(){"
                "var l=document.getElementById('cmd-line');l.value=b.dataset.cmd;window.scrollTo(0,0);l.focus()}});"
                "function loadCmdHint(){var line=document.getElementById('cmd-line'),first=document.querySelector('.load-cmd');"
                "if(!line.value)line.placeholder=first?('例: '+first.dataset.cmd):'在浏览表点\"测\"填入命令'}"
                "function sendCmd(){var line=document.getElementById('cmd-line'),out=document.getElementById('cmd-out'),cmd=line.value.trim();"
                "if(!cmd)return;"
                "if(!confirm('发送并执行: '+cmd+'\\n\\n注意: 可能触发机器人物理动作, 注意安全! 动作执行完毕之前不要关闭页面, 是否继续?'))return;"
                "out.value='发送中...';"
                "fetch('/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({cmd:cmd})})"
                ".then(function(r){return r.json()}).then(function(j){out.value=JSON.stringify(j,null,2)})"
                ".catch(function(e){out.value='请求失败: '+e})}"
                "loadCmdHint();"
            )
        )
        page = (
            "<!doctype html><html lang=zh><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            "<title>cmd_server 命令目录</title></head><body>"
            "<h1>cmd_server 命令目录</h1>"
            "<p><button type=button onclick=rescanCommands()>重扫命令</button>"
            "<span id=rescan-msg></span></p>"
            + test_panel
            + "<h2>浏览</h2>"
            + f"<p>{len(commands)} 条命令</p>"
            + _browse_html(commands, config.catalog_enable_test)
            + "<h2>复制命令表 (到 agent 的调用规范)</h2>"
            "<label for=fmt>复制格式:</label>"
            "<select id=fmt onchange=fmtTable_text()>"
            "<option value=full>全 (长短)</option>"
            "<option value=long>仅长参</option>"
            "<option value=short>仅短参</option></select>"
            " 列: "
            "<input type=checkbox id=c-def checked onchange=fmtTable_text()><label for=c-def>描述</label>"
            "<input type=checkbox id=c-arg checked onchange=fmtTable_text()><label for=c-arg>参数</label>"
            "<input type=checkbox id=c-ex checked onchange=fmtTable_text()><label for=c-ex>示例</label>"
            "<textarea id=cmd-table rows=26 readonly wrap=off "
            "style=\"width:100%;font-family:ui-monospace,'Cascadia Mono','SF Mono',Consolas,'DejaVu Sans Mono',monospace\"></textarea>"
            "<script>"
            "var D=" + json.dumps(data) + ";"
            "function fmtTable(){var m=document.getElementById('fmt').value;"
            "var bits=['c-def','c-arg','c-ex'].map(function(i){return document.getElementById(i).checked?'1':'0'}).join('');"
            "return D[m+'-'+bits]||D[m+'-111']}"
            "function fmtTable_text(){document.getElementById('cmd-table').value=fmtTable()}"
            "function rescanCommands(){var msg=document.getElementById('rescan-msg');msg.textContent='重扫中...';"
            "fetch('/list').then(function(r){return r.json()}).then(function(j){var r=j.refresh||{},t='新增 '+r.added.length"
            "+' 移除 '+r.removed.length+' 变更 '+r.changed.length+' (共 '+r.command_count+')';"
            "var e=r.errors||{};var en=Object.keys(e);if(en.length){t+=' | 错误 '+en.length+' 条: '+en.join(' ');"
            "var txt=en.map(function(n){return '['+n+']\\n'+e[n]}).join('\\n\\n');alert('命令加载错误 ('+en.length+' 条):\\n\\n'+txt);}"
            "msg.textContent=t;"
            "setTimeout(function(){location.reload()},800)}).catch(function(e){msg.textContent='重扫失败: '+e})}"
            + test_script
            + "fmtTable_text();"
            "</script></body></html>"
        )
        return HTMLResponse(page)

    def _resolve(argv: list[str]) -> list[str] | None:
        """argv 首 token 查白名单: 扫描命令前置 python3, 系统命令原样; 未知返回 None."""
        name = argv[0]
        if name in registry.commands:
            return ["python3", str(registry.commands[name].py_path), *argv[1:]]
        if name in registry.whitelist:  # 系统命令
            return argv
        return None

    def _resolve_chain(chain):
        """整条链的每段都解析成 argv; 任一段首 token 不在白名单 -> (None, 那个 name)."""
        resolved = []
        for joiner, pipeline in chain:
            stages = []
            for argv in pipeline:
                exe = _resolve(argv)
                if exe is None:
                    return None, argv[0]
                stages.append(exe)
            resolved.append((joiner, stages))
        return resolved, None

    @app.post("/run")
    def run(request: RunRequest) -> dict:
        cmd_str = request.cmd.strip()
        if not cmd_str:
            return JSONResponse(status_code=400, content={"success": False, "error": "空命令"})
        try:
            chain = runner.parse_chain(cmd_str)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})

        resolved, bad = _resolve_chain(chain)
        if bad is not None:
            # 多 worker 各持独立内存注册表: /list 重扫只刷新命中的 worker,
            # /run 可能落在另一旧 worker → 新命令误报不在白名单. 就地重扫再查一次, 免重启容器.
            with _lock:
                registry.rescan()
            resolved, bad = _resolve_chain(chain)
        if bad is not None:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"命令不在白名单: {bad}",  # 命令集从 /list 取, 不内联列表 (既丑又刷屏)
                },
            )
        result = runner.run_chain(resolved)
        return {"success": result.success, "output": result.output}

    app.state.config = config
    app.state.registry = registry
    return app


app = create_app()


def run() -> None:
    """entrypoint/gunicorn 绑定点."""
    uvicorn.run(app, host="0.0.0.0", port=8080)
