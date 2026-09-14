# -*- coding: utf-8 -*-
"""
V2 | 中心化编排（Boss-Worker）

这一版要体会的事：
  1. Boss 是唯一的决策点：任务图由它生成，打回重试也由它决定。
  2. 让流水线收敛的关键，不是 reviewer 更聪明，而是 Boss 在「重派任务」时
     明确要求了『附证据』—— 门禁只认证据，不认「已修复」这句声明。
  3. 有重试预算（retry_budget），用完就升级人工 —— 这就是「人类在环」。

对标文档《06-多智能体》：第 2 节（Boss-Worker）、第 4 节（任务分配）、
                        第 5 节（冲突解决：优先级 + 证据门槛）

运行：
    python v2_boss_worker.py
    python v2_boss_worker.py --mock
"""

import json

from lab_core import (
    PERSONAS,
    Tracer,
    banner,
    build_client,
    clip,
    cost_report,
    extract_verdict,
)

GOAL = "给订单系统加一个按关键词搜索订单的接口，支持分页，且不得返回手机号"
RETRY_BUDGET = 3


def parse_plan(raw: str) -> list:
    """从模型输出里抠 JSON。生产上应该直接用 JSON mode / function calling。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Boss 没有按格式输出：{clip(raw, 150)}")
    return json.loads(raw[start : end + 1]).get("subtasks", [])


def context(goal: str, artifacts: dict, budget: int = 700) -> str:
    """把已有产物拼成上下文。注意这里是「全量拼接」——生产上要换成摘要或引用 ID。"""
    parts = [f"总目标：{goal}"]
    for name, text in artifacts.items():
        parts.append(f"[{name} 的产物]\n{clip(text, budget)}")
    return "\n\n".join(parts)


def review(client, tracer, goal, artifacts) -> str:
    out = client.chat(
        PERSONAS["reviewer"],
        context(goal, artifacts) + "\n\n请给出审查结论。",
        tag="reviewer",
    )
    verdict = extract_verdict(out)
    tracer.log("review", "reviewer", f"verdict={verdict}")
    return verdict


def main() -> None:
    client = build_client("boss")
    banner("V2｜中心化编排 Boss-Worker：Boss 拆任务 -> 派活 -> 质检 -> 打回重做", client)
    tracer = Tracer()

    print("\n[1] Boss 规划任务图")
    plan_raw = client.chat(PERSONAS["boss"], f"目标：{GOAL}", tag="boss")
    tracer.log("plan", "boss", plan_raw)
    for st in parse_plan(plan_raw):
        print(f"     - {st.get('agent', '?'):<10} {st.get('task', '')}")

    artifacts: dict = {}

    print("\n[2] 流水线产出（analyst -> architect -> coder）")
    artifacts["analyst"] = client.chat(PERSONAS["analyst"], context(GOAL, artifacts), tag="analyst")
    artifacts["architect"] = client.chat(PERSONAS["architect"], context(GOAL, artifacts), tag="architect")
    print("ARCHITECT RAW:", repr(artifacts["architect"]))
    # 关键设计：初稿故意用「快速版」人设，不强调边界处理 —— 否则质检门就没意义了
    artifacts["coder"] = client.chat(PERSONAS["coder_fast"], context(GOAL, artifacts), tag="coder")
    print("\n[3] 质检门：reviewer 只认证据，不认声明")
    verdict = review(client, tracer, GOAL, artifacts)
    print(f"     -> 结构化结论：{verdict}")

    retry_left = RETRY_BUDGET
    while verdict == "FAIL" and retry_left > 0:
        retry_left -= 1
        print(f"\n[4] Boss 决策：打回重做（剩余预算 {retry_left}），并在派活时明确要求『附证据』")
        artifacts["coder"] = client.chat(
            PERSONAS["coder"],
            context(GOAL, artifacts) + "\n\n审查未通过，请修改。必须附上边界处理的证据。",
            tag="coder-rework",
        )
        tracer.log("rework", "coder", "按审查意见重做，并附上证据")
        verdict = review(client, tracer, GOAL, artifacts)
        print(f"     -> 结构化结论：{verdict}")

    print("\n[5] 结果")
    if verdict == "PASS":
        print("     交付成功：Boss 收敛，任务可以进入 DONE")
    else:
        print("     重试预算用尽 -> 升级人工处理（人类在环，别让 Agent 无限互相折磨）")

    tracer.dump()
    print(f"\n账本：{cost_report()}")
    print("\n【观测点】")
    print("  1. Boss 是唯一决策点，所以它的规划错误会放大到全局 —— 这就是")
    print("     文档第 2 节说的『Boss 是瓶颈与单点』。")
    print("  2. 让流程收敛的不是 reviewer 更严格，而是 Boss 重派任务时把")
    print("     『必须附证据』写进了指令 —— 门禁只认证据，不认声明。")
    print("  3. 如果用 .env 里的 FAST / SMART 两个模型，你就实现了文档第 9 节说的")
    print("     『小模型干子任务 + 大模型做决策』。")


if __name__ == "__main__":
    main()
