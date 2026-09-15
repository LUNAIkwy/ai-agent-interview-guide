# -*- coding: utf-8 -*-
"""
V4 | 给 V3 装上刹车：四道护栏 + 状态机 + trace

四个场景，按顺序跑：
  A   把 V3 的死循环交给 Guard        -> 看哪一道刹车最先响
  A2  critic 每轮换措辞               -> 去重失效，「无进展检测」兜底
  B   真正的解法：门禁只认证据         -> 一次打回就收敛，状态机走到 DONE
  C   状态机拒绝非法迁移               -> PLAN 不能直接跳到 DONE

对标文档《06-多智能体》：第 6 节（状态机）、第 9 节 Q14（死循环检测）、
                        Q15（错误隔离：校验 Agent 当门禁）

运行：
    python v4_guarded.py
    python v4_guarded.py --mock
    python v4_guarded.py --max-steps 4
"""

from lab_core import (
    PERSONAS,
    Guard,
    Halt,
    TaskState,
    Tracer,
    USAGE,
    arg_int,
    banner,
    build_client,
    clip,
    cost_report,
    delta,
    extract_verdict,
    snapshot,
)

GOAL = "给订单系统加一个按关键词搜索订单的接口，支持分页，且不得返回手机号"

# 门禁（reviewer）必须看到完整产物。
# 拿一段被截断的代码去送审，等于「没有证据」—— 真实模型会直接回一句
# 「代码在 xxx 处被截断，无法确认」，于是场景 B 永远收敛不了。
VIEW_LIMIT = 6000


def _loop_under_guard(client, guard, critic_persona, critic_tag, ask: str = "请复审。"):
    """V3 的那个死循环，一字不改，只是外面套了 Guard。"""
    code = client.chat(PERSONAS["coder_fast"], f"总目标：{GOAL}", tag="author", verbose=False)
    step = 0
    try:
        while True:
            step += 1
            critique = client.chat(
                critic_persona,
                f"总目标：{GOAL}\n待审产物：\n{clip(code, 600)}\n\n{ask}",
                tag=critic_tag,
                verbose=False,
            )
            guard.check(step, critique, extract_verdict(critique))
            code = client.chat(
                PERSONAS["coder_fast"],
                f"总目标：{GOAL}\n审查意见：{clip(critique, 300)}\n\n请修改后重新提交。",
                tag="author",
                verbose=False,
            )
    except Halt as exc:
        print(f"     [刹车] {exc}")
        print(f"     -> 第 {step} 步就停住了")
    return step


def scenario_a(client, max_steps: int) -> None:
    print("\n" + "-" * 70)
    print("场景 A：V3 的死循环交给 Guard（critic 每轮说的话一模一样）")
    before = snapshot()
    _loop_under_guard(client, Guard(max_steps=max_steps, patience=2), PERSONAS["critic"], "critic")
    print(f"     本次代价：{delta(before)}")


def scenario_a2(client, max_steps: int) -> None:
    print("\n" + "-" * 70)
    print("场景 A2：critic 每轮换措辞 -> ② 去重失效，③ 无进展检测兜底")
    before = snapshot()
    _loop_under_guard(
        client,
        Guard(max_steps=max_steps + 3, patience=2),
        PERSONAS["critic_chatty"],
        "critic_chatty",
        ask="请换一个角度复审。",
    )
    print(f"     本次代价：{delta(before)}")


def scenario_b(client) -> None:
    print("\n" + "-" * 70)
    print("场景 B：真正的解法 —— 不是靠刹车硬停，而是让门禁只认证据")
    tracer = Tracer()
    before = snapshot()
    state = TaskState()

    state.move("EXEC")
    code = client.chat(PERSONAS["coder_fast"], f"总目标：{GOAL}", tag="author")
    tracer.log("exec", "author", "初版实现")

    state.move("VERIFY")
    verdict = "UNKNOWN"
    for attempt in (1, 2):
        out = client.chat(
            PERSONAS["reviewer"],
            f"总目标：{GOAL}\n待审产物：\n{clip(code, VIEW_LIMIT)}\n\n请给出审查结论。",
            tag="reviewer",
        )
        verdict = extract_verdict(out)
        tracer.log("verify", "reviewer", f"attempt={attempt} verdict={verdict}")
        if verdict == "PASS":
            break
        print("     门禁未通过 -> 打回 EXEC，并在派活时把『必须附证据』写进指令")
        state.move("EXEC")
        code = client.chat(
            PERSONAS["coder"],
            f"总目标：{GOAL}\n审查意见：{clip(out, VIEW_LIMIT)}\n\n请修改。必须附上边界处理的证据。",
            tag="coder-rework",
        )
        tracer.log("exec", "author", f"rework#{attempt}")
        state.move("VERIFY")

    if verdict == "PASS":
        state.move("DONE")
        print(f"     收敛：一次打回 + 一次重做就通了，状态机走到 DONE（{delta(before)}）")
    else:
        print(f"     仍未通过 -> 升级人工（人类在环）（{delta(before)}）")

    tracer.dump()


def scenario_c() -> None:
    print("\n" + "-" * 70)
    print("场景 C：状态机拒绝非法迁移")
    try:
        TaskState(verbose=False).move("DONE")  # 当前是 PLAN，不允许直接跳 DONE
    except ValueError as exc:
        print(f"     [拒绝] {exc}")


def main() -> None:
    client = build_client("critic")
    banner("V4｜给 V3 装上刹车：四道护栏 + 状态机 + trace", client)
    max_steps = arg_int("--max-steps", 4)

    scenario_a(client, max_steps)
    scenario_a2(client, max_steps)
    scenario_b(client)
    scenario_c()

    print("\n" + "=" * 70)
    print("Guard 的四道刹车（对应文档第 9 节 Q14「如何检测死循环」）：")
    for rule in Guard.RULES:
        print(f"   {rule}")

    print("\n哪一道先响，取决于你的场景：")
    print(f"   mock（措辞固定）    -> {Guard.RULES[1]} 最先响")
    print(f"   真实模型（措辞多变）-> {Guard.RULES[1]} 常常失效，{Guard.RULES[2]} / {Guard.RULES[0]} 才是主力")
    print("   这就是为什么生产上四道都要装：单靠任何一道都会漏。")

    print("\n【本篇最重要的一句话】")
    print("   刹车只能止血，不能治病。真正让 V3 收敛的，是场景 B 里那个动作：")
    print("   把『必须附证据』写进派活指令，让门禁只认证据、不认声明。")
    print(f"\n总账本：{cost_report()}")


if __name__ == "__main__":
    main()
