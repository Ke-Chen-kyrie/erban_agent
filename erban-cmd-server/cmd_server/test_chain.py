"""runner 命令链的解析/执行自检. 跑法: python3 cmd_server/test_chain.py (也兼容 pytest)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmd_server.runner import parse_chain, run_chain  # noqa: E402


def test_parse_ok() -> None:
    assert parse_chain("ls -la | grep py") == [(";", [["ls", "-la"], ["grep", "py"]])]
    assert parse_chain("a && b") == [(";", [["a"]]), ("&&", [["b"]])]
    assert parse_chain("a ; b || c") == [(";", [["a"]]), (";", [["b"]]), ("||", [["c"]])]
    assert parse_chain('echo "a|b"') == [(";", [["echo", "a|b"]])]  # 引号内竖线是字面参数
    assert parse_chain("a|b") == [(";", [["a"], ["b"]])]  # 无空格也认


def test_parse_reject() -> None:
    for bad in ("ls > /tmp/x", "ls >> x", "a & b", "ls $(whoami)", "ls < x", "(a)", "a ;; b", "| a", "a |", "a &&", "'未闭合"):
        try:
            parse_chain(bad)
        except ValueError:
            continue
        raise AssertionError(f"未拒绝: {bad}")


def test_run_pipeline() -> None:
    res = run_chain(parse_chain("printf 'b\\na\\n' | sort"))
    assert (res.success, res.output) == (True, "a\nb")

    res = run_chain(parse_chain("printf 'b\\n' | grep zzz"))  # grep 无匹配 -> 末段非 0
    assert res.success is False

    res = run_chain(parse_chain("false && echo no"))
    assert (res.success, res.output) == (False, "")

    res = run_chain(parse_chain("false || echo yes"))
    assert (res.success, res.output) == (True, "yes")

    res = run_chain(parse_chain("echo one ; echo two"))
    assert (res.success, res.output) == (True, "one\ntwo")

    res = run_chain(parse_chain("ls /nonexistent-zzz"))  # stderr 一并收进 output
    assert res.success is False and "nonexistent-zzz" in res.output


def test_run_timeout() -> None:
    res = run_chain(parse_chain("python3 -c 'import time;time.sleep(5)'"), timeout=1)
    assert res.success is False and "超时" in res.output


def test_run_shell_false() -> None:
    """元字符只作字面参数, 不进 shell."""
    res = run_chain(parse_chain("echo '$(id)'"))
    assert res.output == "$(id)"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("all passed")
