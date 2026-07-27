"""Heartbeat 入口。启动 pyobjc 桌宠壳(NSApplication + 透明桌宠窗 + 后台 uvicorn)。"""
from __future__ import annotations


def main():
    from shell.app import main as shell_main
    shell_main()


if __name__ == "__main__":
    main()
