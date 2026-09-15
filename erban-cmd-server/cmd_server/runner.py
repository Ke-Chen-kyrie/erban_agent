"""命令执行 (runner). 校验放 main, 这里只负责把已解析好 argv 的链跑起来.

chain 形如 [(前置连接符, [argv, ...]), ...] — 一条管道 = 一个 argv 列表 (段间 stdin/stdout 相接),
连接符 ';' / '&&' / '||' 决定该管道跑不跑 (同 shell 短路). 全程 shell=False, 无元字符解释.
"""

from __future__ import annotations

from dataclasses import dataclass
import shlex
import subprocess
import tempfile
import time


@dataclass
class RunResult:
    success: bool
    output: str


_PUNCT = "();<>|&"  # shlex punctuation_chars 默认集: 连续标点合成一个 token (&&, >> 等)
_OPS = ("|", "&&", "||", ";")  # 放行的连接符, 其余标点一律拒 (重定向/后台/子 shell 没有 shell 可借)


def parse_chain(cmd_str: str) -> list[tuple[str, list[list[str]]]]:
    """切命令链: 'a | b && c' -> [(';', [[a],[b]]), ('&&', [[c]])]. 非法则抛 ValueError.

    shlex 负责引号/转义, 所以 `--text "a|b"` 里的竖线仍是字面参数, 与 shell 一致.
    """
    try:
        lexer = shlex.shlex(cmd_str, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:  # 引号未闭合
        raise ValueError(f"命令解析失败: {exc}") from exc

    pipelines: list[list[list[str]]] = [[]]  # 每条管道 = 若干段 argv (按 | 切)
    joiners: list[str] = []  # joiners[i] 连接 pipelines[i] 与 pipelines[i+1]
    for token in tokens:
        if token and all(c in _PUNCT for c in token):
            if token not in _OPS:
                raise ValueError(f"不支持的操作符 {token!r}: 仅支持 {' '.join(_OPS)}, 不支持重定向/后台/子 shell")
            if not pipelines[-1] or not pipelines[-1][-1]:
                raise ValueError(f"操作符 {token!r} 左侧缺少命令")
            if token == "|":
                pipelines[-1].append([])
            else:
                joiners.append(token)
                pipelines.append([])
            continue
        if not pipelines[-1]:
            pipelines[-1].append([])
        pipelines[-1][-1].append(token)

    if not pipelines[-1] or not pipelines[-1][-1]:
        pipelines.pop()  # 尾随连接符
    if not pipelines or len(joiners) != len(pipelines) - 1:
        raise ValueError("命令为空或不完整")
    if any(not argv for pipeline in pipelines for argv in pipeline):
        raise ValueError("存在空命令段")
    return list(zip([";", *joiners], pipelines))


def _pipeline(stages: list[list[str]], env: dict[str, str] | None, deadline: float) -> tuple[str, str, int | None]:
    """跑一条管道 (stages 已解析 argv). 返回 (末段 stdout, 全段 stderr, 末段退出码).

    stderr 统一倒进临时文件, 免得中间段 stderr 写满管道阻塞 (无 shell 可借, 只能自己接).
    退出码 None = 超时或被 kill.
    """
    procs: list[subprocess.Popen] = []
    with tempfile.TemporaryFile(mode="w+") as errf:
        prev = None
        for argv in stages:
            try:
                proc = subprocess.Popen(
                    argv, stdin=prev, stdout=subprocess.PIPE, stderr=errf, text=True, env=env
                )
            except OSError as exc:
                for p in procs:
                    p.kill()
                return "", f"无法执行 {argv[0]}: {exc}", None
            if prev is not None:
                prev.close()  # 子进程已 dup, 父进程这份要放掉, 否则末段收不到 EOF
            procs.append(proc)
            prev = proc.stdout

        timed_out = False
        try:
            out, _ = procs[-1].communicate(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            out, timed_out = "", True
        for proc in procs[:-1]:  # 末段退了, 前段写管道会 EPIPE 自行结束; 卡住的按剩余时间 kill
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                timed_out = True
        errf.seek(0)
        err = errf.read()
    if timed_out:
        for proc in procs:
            proc.kill()
        return out, err, None
    return out, err, procs[-1].returncode


def run_chain(
    chain: list[tuple[str, list[list[str]]]],
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> RunResult:
    """按 shell 语义执行整条链. 阻塞到返回, 不抛异常.

    超时是整条链的总预算. stdout 取各管道末段 (中间段已喂给下一段), stderr 全收.
    success = 实际跑过的管道末段退出码全 0.
    """
    deadline = time.monotonic() + timeout
    parts: list[str] = []
    success = True
    for joiner, stages in chain:
        if joiner == "&&" and not success:
            continue
        if joiner == "||" and success:
            continue
        out, err, code = _pipeline(stages, env, deadline)
        if code is None:
            parts.append((out + err).strip())
            return RunResult(False, "\n".join(p for p in (*parts, f"超时 ({timeout}s)") if p).strip())
        parts.append((out + err).strip())
        success = code == 0
    return RunResult(success, "\n".join(p for p in parts if p).strip())
