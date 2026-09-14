# -*- coding: utf-8 -*-
"""
V3 | 民主讨论跑飞了（Joint Discussion 没有护栏）

!!! 这一版是故意做崩的，不要「修好」它 !!!

三个「没有」直接导致死循环：
  - 没有 Boss（缺少决策点）
  - 没有证据门槛（author 只口头说「已修复」，拿不出证据）
  - 没有终止条件（critic 被设定为必须每轮挑出新问题）

要观察的：
  1. 两个 Agent 都非常礼貌、都非常「努力工作」，但永远收敛不了。
  2. Token 在肉眼可见地增长 —— 这就是文档第 9 节的「死循环 + Token 成本」。
  3. 真实系统没有我这里的 --max-rounds 上限，它会一直转下去，直到你发现账单。

对标文档《06-多智能体》：第 2 节（民主讨论的致命伤）、第 5 节（光有对话/投票不够）、
                        第 9 节（死循环、Token 成本控制）

运行：
    python v3_deadloop.py                  # 真实模型，默认 12 轮硬上限
    python v3_deadloop.py --max-rounds 8
    python v3_deadloop.py --mock           # 离线、不花钱
"""

from lab_core import (
    PERSONAS,
    USAGE,
    arg_int,
    banner,
    build_client,
    clip,
    cost_report,
    extract_verdict,
)

GOAL = "给订单系统加一个按关键词搜索订单的接口，支持分页，且不得返回手机号"


def main() -> None:
    client = build_client("critic")
    banner("V3｜民主讨论跑飞：没有 Boss / 没有证据门槛 / 没有终止条件", client)

    rounds_cap = arg_int("--max-rounds", 12)
    print(f"\n[警告] 这一版故意做崩。演示用硬上限 = {rounds_cap} 轮。")
    if not client.mock:
        print("[警告] 真实 API 模式下这是真的在花钱。看完立刻去看 V4 怎么治它。")

    artifacts = {}
    artifacts["author"] = client.chat(
        PERSONAS["coder_fast"], f"总目标：{GOAL}\n请给出初版实现。", tag="author"
    )

    converged = False
    for rnd in range(1, rounds_cap + 1):
        critique = client.chat(
            PERSONAS["critic"],
            f"总目标：{GOAL}\n待审产物：\n{clip(artifacts['author'], 600)}\n\n请复审。",
            tag="critic",
            verbose=(rnd <= 2),
        )
        verdict = extract_verdict(critique)
        if verdict == "PASS":
            print(f"\n  第 {rnd} 轮居然收敛了（critic 这次没听人设）")
            converged = True
            break

        artifacts["author"] = client.chat(
            PERSONAS["coder_fast"],
            f"总目标：{GOAL}\n审查意见：{clip(critique, 300)}\n\n请修改后重新提交。",
            tag="author",
            verbose=(rnd <= 2),
        )
        print(f"  第 {rnd:>3} 轮 | 累计 {USAGE.calls} 次调用 | 约 {USAGE.tokens} tokens | 结论 {verdict}")

    if not converged:
        print(f"\n  到达演示上限 {rounds_cap} 轮仍未收敛。")
        print("  真实系统没有这个上限 —— 它会一直转下去。这就是『死循环』。")

    print(f"\n账本：{cost_report()}")
    print("\n【结论】")
    print("  author 一直在『声称』改好了，critic 一直在说『还是有新问题』。")
    print("  两个 Agent 都很礼貌、都在努力工作 —— 但它们永远收敛不了。")
    print("  这不是模型笨，是结构缺了三样东西：决策点、证据门槛、终止条件。")
    print("  -> 去跑 v4_guarded.py，看这三样怎么补上。")


if __name__ == "__main__":
    main()
