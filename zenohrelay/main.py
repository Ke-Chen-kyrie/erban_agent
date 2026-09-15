"""兼容入口：等价于运行 ``server.py``。"""

from server import main


if __name__ == "__main__":
    raise SystemExit(main())
