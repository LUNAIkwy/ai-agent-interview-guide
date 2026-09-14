# -*- coding: utf-8 -*-
"""
V1 | 流水线 + 共享黑板（Pipeline + Blackboard）

这一版要体会的事：
  1. 每个 Agent 的上下文都很短 —— 它只看到「上一轮产物」，看不到全部历史。
  2. 黑板是唯一的真相源：所有中间产物都写在里面，而不是靠 Agent 互相转述。
  3. 产物会经历 需求 -> 设计 -> 代码 三次变形，每一步都看得见。

对标文档《06-多智能体》：第 2 节（流水线模式）、第 3 节（共享黑板）、第 6 节（全局状态共享）

运行：
    python v1_pipeline.py            # 用 .env 里配的真实模型
    python v1_pipeline.py --mock     # 离线跑，不花钱
"""

from lab_core import PERSONAS, banner, build_client, clip, cost_report


class Blackboard:
    """一块公共白板。Agent 只通过它读写，禁止私下传话。"""

    def __init__(self) -> None:
        self._data = {}

    def write(self, key: str, value: str) -> None:
        self._data[key] = value

    def read(self, key: str = "") -> str:
        return self._data.get(key, "")


GOAL = "给订单系统加一个按关键词搜索订单的接口，支持分页，且不得返回手机号"

# 阶段顺序写死：这就是「流水线」—— 谁先谁后是固定的
STAGES = [
    ("analyst", "analyst", "requirements"),
    ("architect", "architect", "design"),
    ("coder", "coder", "code"),
]


def main() -> None:
    client = build_client("default")
    banner("V1｜流水线 + 共享黑板：需求 -> 设计 -> 代码", client)

    board = Blackboard()
    board.write("goal", GOAL)

    prev_key = ""
    for tag, persona, key in STAGES:
        user = (
            f"总目标：{board.read('goal')}\n"
            f"上一轮产物：{board.read(prev_key) if prev_key else '（无，你是第一棒）'}\n"
            f"请只完成你这一段。"
        )
        board.write(key, client.chat(PERSONAS[persona], user, tag=tag))
        prev_key = key

    print("\n" + "-" * 70)
    print("黑板最终内容（这就是面试里说的『单一真相源』）：")
    for k in ("requirements", "design", "code"):
        print(f"  [{k}] {clip(board.read(k), 90)}")

    print("\n【观测点】")
    print("  1. 每个 Agent 只收到『上一轮产物』—— 谁都没看到全部历史，上下文被切短了。")
    print("     这就是文档第 1 节说的『拆上下文』：子上下文更短、更聚焦。")
    print("  2. 反例：把这三步塞进同一个 prompt 交给一个 Agent，就是 V0 的单 Agent 长链，")
    print("     也就是文档第 1 节讲的『注意力漂移』。")
    print("  3. 流水线的隐患：如果 coder 发现设计错了，它没法自己退回第一步重想。")
    print("     想解决就得加一条『反馈边』，那是 V2 里 Boss 干的活。")
    print(f"\n账本：{cost_report()}")


if __name__ == "__main__":
    main()
