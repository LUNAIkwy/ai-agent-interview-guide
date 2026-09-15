# -*- coding: utf-8 -*-
"""
V5 | 性能优化：并行、限流、缓存分层、流式输出

对标文档《08-工程化实践》第 6 节：
    6.2.1 异步处理 / 6.2.2 流式输出 / 6.2.3 并发控制（Semaphore）
    6.2.4 连接池 / 6.2.5 缓存分层（L1 进程内 + L2 共享）

四个场景（这个文件用**真实时间**，因为要量耗时，总共约 3 秒）：
  A  串行 vs 并行 vs 限流并行：同一批请求的墙钟时间与并发峰值
  B  并发不是越高越好：并发 8 会把自己打成 429，Semaphore 是关键阀门
  C  L1/L2 双层缓存的读路径：L1 -> L2 -> 回源模型，以及多实例共享
  D  流式输出：首字时间 TTFB 从「等全部生成完」变成「边生成边给」

运行：
    python v5_perf.py
    python v5_perf.py --concurrency 5    # 自己调并发上限试试
"""

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

from lab_core import (
    CLOCK,
    ExactCache,
    FakeVendor,
    Gateway,
    ProviderError,
    Task,
    TokenBucket,
    arg_int,
    banner,
    build_vendors,
    live_hint,
    new_trace,
    section,
    set_log,
    table,
    title_line,
)

TASK = Task(kind="final", needs=frozenset({"json", "tools"}), min_quality=60, region="cn")

# 把假供应商调成「一次调用约 110ms」：真实时间、看得见的耗时
LATENCY_SCALE = 0.3

QUESTIONS = [
    "订单 A1001 什么状态", "帮我搜键盘", "退款多久到账",
    "发票怎么开", "能分页吗", "数据库挂了怎么办",
    "订单 A1002 什么状态", "有限流吗",
]


def make_gateway(policy: str = "cost_first", vendor_events: Optional[List[str]] = None,
                 seed: int = 7, **kwargs: Any) -> Gateway:
    CLOCK.set_real(True)  # 本文件要量真实耗时，所以用真实时钟
    vendors: Dict[str, Any] = build_vendors()
    vendors["vendorA"] = FakeVendor("vendorA", events=vendor_events, seed=seed,
                                    latency_scale=LATENCY_SCALE)
    for name, vendor in vendors.items():
        vendor.latency_scale = LATENCY_SCALE
    return Gateway(vendors, policy=policy, **kwargs)


# ---------------------------------------------------------------------------
# 一个带「并发峰值统计」的异步调用器
# ---------------------------------------------------------------------------
class Meter:
    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0
        self.lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)

    async def exit(self) -> None:
        async with self.lock:
            self.inflight -= 1


async def acall(gw: Gateway, meter: Meter, sem: Optional[asyncio.Semaphore],
                tenant: str, question: str, step: str = "answer"):
    async def run():
        await meter.enter()
        try:
            return await asyncio.to_thread(
                gw.chat, [{"role": "user", "content": question}], TASK, tenant, step)
        finally:
            await meter.exit()

    if sem is None:
        return await run()
    async with sem:
        return await run()


async def run_batch(gw: Gateway, concurrency: Optional[int], label: str) -> Dict[str, Any]:
    meter = Meter()
    sem = asyncio.Semaphore(concurrency) if concurrency else None
    new_trace()
    started = time.perf_counter()
    results = await asyncio.gather(
        *[acall(gw, meter, sem, f"T-10{i % 3 + 1}", q) for i, q in enumerate(QUESTIONS)]
    )
    wall_ms = (time.perf_counter() - started) * 1000
    return {
        "label": label,
        "wall_ms": wall_ms,
        "peak": meter.peak,
        "ok": sum(1 for r in results if r.provider != "static"),
        "degraded": sum(1 for r in results if r.degraded),
        "retry": gw.metrics["retry"],
        "breaker_reject": gw.metrics["breaker_reject"],
        "cost": sum(r.cost for r in results),
        "results": results,
    }


class CongestionVendor(FakeVendor):
    """并发超过上限就 429。

    真实供应商的限流就是这个行为：它不管你「是不是同一个请求」，
    只看同一时刻压过来多少。所以「把并发开到最大」的结果必然是 429 + 重试 + 更慢。
    """

    def __init__(self, name: str, limit: int = 4, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.limit = limit
        self.inflight = 0
        self._lock = threading.Lock()
        self.rejected = 0

    def chat(self, model: str, messages: List[Dict[str, str]], **kwargs: Any):
        with self._lock:
            self.inflight += 1
            over = self.inflight > self.limit
            now_inflight = self.inflight
        try:
            if over:
                with self._lock:
                    self.rejected += 1
                raise ProviderError("429", f"并发 {now_inflight} > 供应商上限 {self.limit}",
                                    retry_after=0.4)
            return super().chat(model, messages, **kwargs)
        finally:
            with self._lock:
                self.inflight -= 1


def scenario_a() -> None:
    section("场景 A：串行 vs 全并行 vs 限流并行（8 个请求，单次约 110ms）")
    rows = []
    for label, conc in (("串行（1）", 1), ("全并行（8）", None), ("限流并行（Semaphore=4）", 4)):
        gw = make_gateway(record_ledger=False)
        out = asyncio.run(run_batch(gw, conc, label))
        rows.append([label, f"{out['wall_ms']:.0f}ms", str(out["peak"]), str(out["ok"]),
                     str(out["retry"]), f"{out['wall_ms'] / 8:.0f}ms"])
    print(table(["方式", "总耗时", "并发峰值", "成功", "重试", "平均每请求"], rows))
    print()
    print("  记住两件事：")
    print("      - I/O 密集（HTTP、DB）并行确实能提速：8 个请求从 ~0.9s 降到 ~0.11s。")
    print("      - 但「全并行」把 8 个请求同时砸给供应商，真实世界这就是 429 的来源 —— 见场景 B。")
    print("      - 如果瓶颈是 GPU 推理（单机单卡），并行只会让每个请求都变慢，得靠批处理/多副本。")


def scenario_b(concurrency: int) -> None:
    section("场景 B：并发的正确姿势是「限流 + 退避」，不是「放手冲」")
    print(f"  供应商：vendorA 的并发上限 = 4，超过就直接 429（真实限流就是这么发生的）")
    print()
    rows = []
    for label, conc in (("不限流（8 并发）", None), (f"Semaphore={concurrency}", concurrency)):
        gw = make_gateway(record_ledger=False, max_retries=2, base_delay=0.4,
                          breaker_kwargs={"failure_threshold": 10, "open_seconds": 5.0})
        gw.vendors["vendorA"] = CongestionVendor("vendorA", limit=4, seed=3,
                                                 latency_scale=LATENCY_SCALE)
        out = asyncio.run(run_batch(gw, conc, label))
        rejected = getattr(gw.vendors["vendorA"], "rejected", 0)
        rows.append([label, f"{out['wall_ms']:.0f}ms", str(rejected), str(out["retry"]),
                     f"{out['ok']}/8"])
    print(table(["方式", "总耗时", "被 429 的次数", "重试次数", "最终成功"], rows,
                title="同一批请求、同一个有并发上限的供应商"))
    print()
    print("  结论很清楚：把并发开到最大，不会更快 —— 只会多出一堆 429、重试和更长的尾延迟。")
    print("  正确做法是「限流到供应商能承受的水位」，再配合退避。")
    bucket = TokenBucket(rate_per_s=2.0, burst=2.0)
    bucket.try_acquire()
    bucket.try_acquire()
    print(f"  令牌桶（按供应商 rpm 限流）：rate=2/s、burst=2，用完 2 个令牌后，"
          f"第 3 个请求要等 {bucket.wait_time(1.0):.2f}s 才有额度。")
    print("      文档 1.2.5 的原话：对 429 要「限流 + 退避」——"
          "限流让请求别挤在同一秒，退避让重试别踩在对方刚缓过来的时候。")


def scenario_c() -> None:
    section("场景 C：缓存分层 L1（进程内）-> L2（共享）-> 回源模型")
    CLOCK.set_real(True)

    class SharedL2:
        """模拟 Redis：多实例共享。真实项目用 redis.asyncio + TTL + 序列化。"""

        def __init__(self) -> None:
            self.data: Dict[str, str] = {}
            self.gets = 0
            self.hits = 0

        def get(self, key: str) -> Optional[str]:
            self.gets += 1
            value = self.data.get(key)
            if value is not None:
                self.hits += 1
            return value

        def set(self, key: str, value: str) -> None:
            self.data[key] = value

    class TwoLevelCache:
        """读路径：L1 -> L2 -> 回源；任意一层命中都要回填上层。"""

        def __init__(self, l2: SharedL2, ttl_s: float = 300.0) -> None:
            self.l1 = ExactCache(ttl_s=ttl_s)
            self.l2 = l2
            self.l1_hits = 0
            self.l2_hits = 0
            self.misses = 0

        def get(self, key: str):
            value = self.l1.get(key)
            if value is not None:
                self.l1_hits += 1
                return value, "L1"
            value = self.l2.get(key)
            if value is not None:
                self.l2_hits += 1
                self.l1.set(key, value)  # 回填 L1：下次同实例直接命中
                return value, "L2"
            self.misses += 1
            return None, "miss"

    l2 = SharedL2()
    gw = make_gateway(record_ledger=False)
    question = "订单 A1001 什么状态"
    cache_a, cache_b = TwoLevelCache(l2), TwoLevelCache(l2)

    def key_of(cache: TwoLevelCache) -> str:
        return cache.l1.key("T-1001", "cheap-mini", "v1-prompt", "tools-7",
                            [{"role": "user", "content": question}])

    def read(cache: TwoLevelCache, who: str) -> List[str]:
        key = key_of(cache)
        value, layer = cache.get(key)
        if value is None:
            started = time.perf_counter()
            result = gw.chat([{"role": "user", "content": question}], TASK, "T-1001")
            took = (time.perf_counter() - started) * 1000
            cache.l1.set(key, result.text)
            cache.l2.set(key, result.text)
            return [who, "回源模型", f"{took:.0f}ms", "1 次模型调用"]
        return [who, f"{layer} 命中", "~0.2ms", "省 1 次调用"]

    rows = [read(cache_a, "实例A 第1次"), read(cache_b, "实例B 第1次"),
            read(cache_a, "实例A 第2次"), read(cache_b, "实例B 第2次")]
    print(table(["请求方", "命中层级", "延迟", "成本"], rows))
    print()
    print(f"  L2 统计：get {l2.gets} 次，命中 {l2.hits} 次。"
          f"L1 命中 {cache_a.l1_hits + cache_b.l1_hits} 次，L2 命中 {cache_a.l2_hits + cache_b.l2_hits} 次，"
          f"回源 {cache_a.misses + cache_b.misses} 次。")
    print("  要点：L1 极快但只在单实例内有效；L2 让多实例共享，代价是一次网络往返 + 序列化。")
    print("        上线前想清楚 TTL 与失效：模型换了、Prompt 改了、知识库更新了，旧答案必须脏掉。")


def scenario_d() -> None:
    section("场景 D：流式输出的价值在首字时间（TTFB），不在总耗时")

    async def fake_stream(chunks: int = 6, per_chunk_ms: int = 60):
        for i in range(chunks):
            await asyncio.sleep(per_chunk_ms / 1000)
            yield f"第{i + 1}段"

    async def measure(streamed: bool):
        started = time.perf_counter()
        first = None
        text = ""
        async for chunk in fake_stream():
            if first is None:
                first = (time.perf_counter() - started) * 1000
            text += chunk
        total = (time.perf_counter() - started) * 1000
        if not streamed:
            first = total  # 非流式下，「首字」= 全部生成完
        return first, total, text

    rows = []
    for streamed, label in ((False, "非流式"), (True, "流式（SSE）")):
        first, total, _text = asyncio.run(measure(streamed))
        rows.append([label, f"{first:.0f}ms", f"{total:.0f}ms"])
    print(table(["方式", "首字时间 TTFB", "总耗时"], rows,
                title="同样的生成内容，用户的感受完全不同"))
    print()
    print("  流式的三个工程注意点（文档 Q10）：")
    print("      - 计费仍以 token 为准，流式不省一分钱；")
    print("      - 日志要「聚合完整响应再记一条」，或记增量 chunk 但都带上同一个 trace_id；")
    print("      - 用户中途关页面（连接断开）时，要决定部分结果如何处理（丢弃 / 存草稿 / 计费）。")


def scenario_e() -> None:
    section("场景 E：连接池 / keep-alive（示意，不测真实网络）")
    rtt = 30
    rows = [
        ["每次新建连接（8 次调用）", f"8 次 TLS 握手 ≈ {rtt * 8}ms", "8"],
        ["复用连接（keep-alive）", f"1 次 TLS 握手 ≈ {rtt}ms", "1"],
    ]
    print(table(["方式", "握手开销", "握手次数"], rows))
    print()
    print("  这就是文档 6.2.4 那句话的全部内容：HTTP 客户端复用 keep-alive，数据库用连接池，")
    print("  省下的握手开销在「每个请求都要调 3~5 次模型」的 Agent 场景里是成倍放大的。")


def warm_cache_note() -> None:
    print()
    print("  补充：模型客户端（openai SDK）本身有连接池；如果你自己拼 HTTP，记得给 httpx 配 limits。")


def main() -> None:
    concurrency = arg_int("--concurrency", 3)
    banner("V5｜性能：并行的收益、限流的必要性、缓存分层、流式首字",
           [live_hint(False), f"本文件用真实时间（约 2~3 秒跑完），并发上限 = {concurrency}"])
    set_log(False)
    try:
        scenario_a()
        scenario_b(concurrency)
        scenario_c()
        scenario_d()
        scenario_e()
        warm_cache_note()
    finally:
        CLOCK.set_real(False)  # 别把真实时钟留给后面的脚本

    title_line("V5 结论")
    print("  性能优化不是「让模型变快」，而是：别串行、别裸并发、别重复问、别让用户干等第一个字。")
    print("  下一站 V6：这些东西怎么安全地发布上线（灰度、健康检查、优雅停机、回滚）。")


if __name__ == "__main__":
    main()