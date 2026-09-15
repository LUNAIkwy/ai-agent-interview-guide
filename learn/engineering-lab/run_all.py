# -*- coding: utf-8 -*-
"""
一键按顺序跑完 V1 -> V7。

    python run_all.py                 # 离线全跑（不需要 Key、不花钱）
    python run_all.py --real-time     # 真的等退避与冷却（慢，但能体会）
    python run_all.py --concurrency 5 # 给 V5 改并发上限

每个脚本都会原样收到命令行参数。
注意：V7 的退出码 1 是**故意的** —— 它在演示「回归门禁把 CI 挡红」，不是跑挂了。
"""

import subprocess
import sys
from pathlib import Path

# 让自己的输出也是 UTF-8，否则 Windows 控制台里中文会变成乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent

SCRIPTS = [
    ("v1_naive.py", "裸奔版（反例）：没有容错、没有记账、没有 trace"),
    ("v2_router.py", "路由 + 重试退避 + 三态熔断 + 降级链"),
    ("v3_observe.py", "trace/span + 结构化日志 + 成本账本 + 双层缓存"),
    ("v4_secure.py", "注入检测 + 工具白名单 + schema 校验 + 两步授权 + 审计"),
    ("v5_perf.py", "并行 / Semaphore 限流 / L1-L2 缓存 / 流式 TTFB"),
    ("v6_release.py", "健康检查 + 金丝雀发布 + 自动回滚 + 优雅停机"),
    ("v7_eval.py", "评测集 + 引用核查 + 回归门禁（退出码 1 = CI 拦截）"),
]

SUMMARY = [
    ["V1", "第 1 节（反例）", "一次 429/超时 = 一次用户可见失败，且事后无法复盘"],
    ["V2", "第 1 节", "路由、退避重试、三态熔断、降级链：失败率 40% -> 0%"],
    ["V3", "第 2、3 节", "trace 定位瓶颈 + 账本按租户/模型/步骤算钱 + 缓存防串味"],
    ["V4", "第 4 节", "模型不是安全边界：白名单、schema、两步授权、审计"],
    ["V5", "第 6 节", "并行提速、限流防 429、L1/L2 缓存、流式首字"],
    ["V6", "第 5 节", "探针、金丝雀、SLO 自动回滚、checkpoint 优雅停机"],
    ["V7", "第 7、8 节", "评测集 + 引用核查 + 回归门禁（CI 会红）"],
]


def main() -> None:
    extra = sys.argv[1:]
    for name, desc in SCRIPTS:
        print("\n\n" + "#" * 78)
        print(f"#  {name}  ——  {desc}")
        print("#" * 78)
        subprocess.run([sys.executable, str(HERE / name), *extra], check=False)

    print("\n\n" + "=" * 78)
    print("跑完了。七版对照表（面试时这张表就是你的故事线）：")
    print("=" * 78)
    for row in SUMMARY:
        print(f"  {row[0]:<3} {row[1]:<14} {row[2]}")
    print()
    print("  建议回头精读两处：")
    print("      - v2_router.py 场景 B：一个请求怎么从「限流 -> 重试 -> 熔断 -> 降级」走完；")
    print("      - v7_eval.py 场景 C：为什么「悄悄变差」比报错更危险。")
    print()
    print("  再对照文档 docs/01-面试八股文/08-工程化实践.md 的小结检查清单自测一遍。")


if __name__ == "__main__":
    main()