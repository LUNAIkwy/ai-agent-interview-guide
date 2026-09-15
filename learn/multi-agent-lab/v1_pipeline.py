# -*- coding: utf-8 -*-
"""
V1 | 流水线 + 共享黑板（Pipeline + Blackboard）

这一版要体会的事：
  1. 每个 Agent 的上下文都很短 —— 它只看到「上一轮产物」，看不到全部历史。
  2. 黑板是唯一的真相源：所有中间产物都写在里面，而不是靠 Agent 互相转述。
  3. 产物会经历 需求 -> 设计 -> 代码 三次变形，每一步都看得见。
  4. 这条流水线全程串行，所以黑板**故意没加锁**。文件末尾有个并发实验，
     把「不加锁 / 方法级加锁 / 版本号乐观锁」三种黑板跑同一个 worker，
     你会看到前两种都会丢更新，以及为什么「加锁」并没有解决它。

对标文档《06-多智能体》：第 2 节（流水线模式）、第 3 节（共享黑板）、
                        第 6 节（状态管理与同步）

运行：
    python v1_pipeline.py            # 用 .env 里配的真实模型
    python v1_pipeline.py --mock     # 离线跑，不花钱
"""

import threading
import time
import unicodedata
from typing import Any, Dict, List, Tuple

from lab_core import PERSONAS, banner, build_client, clip, cost_report


class Blackboard:
    """一块公共白板。Agent 只通过它读写，禁止私下传话。

    注意：这是「单线程串行流水线」专用版 —— 没有锁，也没有版本号。
    在本文件这个全程串行的场景里，这是**正确**的简化，不是偷懒：
    没有并发，就没有需要互斥的东西，加个锁只是摆设。

    什么时候它就不够用了？见文件末尾的「并发实验」。
    """

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

# ===========================================================================
# 并发实验：黑板到底要不要加锁？
#
# 三种黑板，跑同一个 worker、同一个目标值，看谁丢更新。
# 对应文档《06-多智能体》第 3 节表格里的「共享黑板：并发写需锁/版本」，
# 以及第 6 节「状态管理与同步」（§6.3 追问：生产上要用乐观锁）。
#
# 黑板之间唯一的差别就是实现，worker 一个字都不差 —— 这样才看得出
# 「丢更新」到底是谁的锅。
# ===========================================================================
WRITERS = 8    # 8 个 Agent 同时在干活
ROUNDS = 500   # 每个 Agent 把黑板上的计数器 +1 五百次


class UnsafeBlackboard:
    """① 裸 dict —— 就是上面那个 Blackboard 的并发版对照。

    单线程用它是绝对正确的；并发下它会丢更新。
    """

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}

    def read(self, key: str) -> Tuple[Any, int]:
        return self._data.get(key), 0          # 没有版本概念，永远回 0

    def write(self, key: str, value: Any, expect_version: int = 0) -> bool:
        self._data[key] = value
        return True                            # 也没有版本检查，永远「写成功」


class LockedBlackboard:
    """② 给每个方法各自加锁 —— 文档第 3 节示例里的写法。

    它保证「单次写」不会撕裂，但保证不了「读-改-写」是一件事：
    两个 Agent 可以先后读到同一个旧值，再先后盖回去，后一次把前一次覆盖掉。
    """

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def read(self, key: str) -> Tuple[Any, int]:
        with self._lock:
            return self._data.get(key), 0

    def write(self, key: str, value: Any, expect_version: int = 0) -> bool:
        with self._lock:
            self._data[key] = value
            return True


class VersionedBlackboard:
    """③ 版本号 + CAS（乐观锁）：唯一能真正防住丢更新的写法。

    写入时必须带上「你读到的那一刻的版本号」，对不上就写失败，让你重读重算。
    生产上这对应 Redis 的 WATCH/MULTI，或 SQL 的 UPDATE ... WHERE version = ?。
    """

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._version: Dict[str, int] = {}
        self._lock = threading.Lock()          # 只保护「比较 + 写入」这一瞬间

    def read(self, key: str) -> Tuple[Any, int]:
        with self._lock:
            return self._data.get(key), self._version.get(key, 0)

    def write(self, key: str, value: Any, expect_version: int = 0) -> bool:
        with self._lock:
            if self._version.get(key, 0) != expect_version:
                return False                   # 有人抢先改过，这次作废
            self._data[key] = value
            self._version[key] = expect_version + 1
            return True


def _race_worker(board, key: str, rounds: int, retries: List[int]) -> None:
    """三种黑板共用这一个 worker：读 -> （停顿）-> 写。

    中间那个停顿就是真实系统里的「一次 LLM 调用 / 一次网络 IO」，
    也正是竞态窗口所在：读到的值可能在你写回去之前就已经过期了。
    """
    for _ in range(rounds):
        while True:
            value, version = board.read(key)
            time.sleep(0)                      # 让出 GIL，模拟耗时的中间计算
            if board.write(key, (value or 0) + 1, version):
                break
            retries[0] += 1                    # 乐观锁的代价：这次白算了，重来


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格：中文占两格，不然表格对不齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def race_demo() -> None:
    expected = WRITERS * ROUNDS
    print("\n" + "-" * 70)
    print(f"并发实验：{WRITERS} 个 Agent 同时把黑板上的计数器 +1，各加 {ROUNDS} 次")
    print(f"（期望 {expected}；三种黑板跑的是同一个 worker，只有黑板实现不同）")
    print("")

    cases = [
        ("① 无锁（裸 dict）", UnsafeBlackboard()),
        ("② 方法级加锁（文档写法）", LockedBlackboard()),
        ("③ 版本号 CAS（乐观锁）", VersionedBlackboard()),
    ]
    for label, board in cases:
        board.write("counter", 0)
        retries = [0]
        threads = [
            threading.Thread(target=_race_worker, args=(board, "counter", ROUNDS, retries))
            for _ in range(WRITERS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = board.read("counter")[0]
        print(f"  {_pad(label, 26)} 实际 {got:<6} 丢了 {expected - got:<6} 重试 {retries[0]}")

    print(
        """
【结论】锁不是「加上就对了」：
  ①② 都丢更新（两者谁多谁少纯属调度噪声，反正都远不到 4000）——
     因为 read 和 write 是两个独立动作，中间那段窗口谁都能插进来。
     加锁只保证「单次写」原子，不保证「读-改-写」这个复合动作原子。
  ③ 靠版本号才真正做到「只有拿着最新版本的人能改」；代价是重试次数 ——
     乐观锁不是免费的，冲突越激烈，白算的次数越多。

【顺带记住两条生产要点】
  · 别把锁套在 LLM 调用外面。一次模型调用是秒级的，持锁调用 = 所有 Agent
    排队等模型，并发收益直接归零。正确姿势：先调模型拿结果，再短暂持锁写黑板。
  · 多进程 / 多副本部署时 threading.Lock 完全无效，这段「比较 + 写入」必须
    交给 DB 或 Redis 去做。

所以 V1 这条串行流水线不加锁不是偷懒：它压根没有并发。
等真出现并发写（多副本、并行取数、人类在环同时改），再按 ③ 上版本号。"""
    )


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

    # 黑板的并发问题跟模型 API 无关，纯本地跑，不花一分钱，也不影响上面的账本
    race_demo()


if __name__ == "__main__":
    main()
