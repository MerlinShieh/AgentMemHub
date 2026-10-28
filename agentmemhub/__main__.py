"""python -m agentmemhub 入口。"""
from agentmemhub.cli import main

if __name__ == "__main__":
    # `SystemExit(main())` 才能把子命令的成败透给 shell —— 直接调 `main()` 时
    # 返回值会被丢弃，退出码恒 0（`feed lint` 的 P0 门槛就是这么失效的）。
    raise SystemExit(main())