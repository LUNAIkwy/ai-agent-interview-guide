# -*- coding: utf-8 -*-
"""
V2 | 给 V1 装上「路由 + 重试退避 + 熔断 + 降级」

对标文档《08-工程化实践》第 1 节：1.2.1 统一抽象、1.2.2 优先级调度、
1.2.3 三态熔断、1.2.4 自动降级、1.2.5 重试与指数退避。

五个场景：
  A  同一任务、四种路由策略 -> 选中的模型不一样（约束内最优化）
  B  把 V1 那 10 个请求重跑一遍：重试 -> 熔断 -> 降级，用户可见失败率 40% -> 0%
  C  熔断状态机：CLOSED -> OPEN -> HALF_OPEN -> CLOSED，以及半开只放几个探测
  D  不可重试错误（401）不该浪费重试：一次失败就换策略
  E  jitter 到底防的是什么：20 个客户端同时重试的「重试风暴」

运行：
    python v2_router.py
    python v2_router.py --real-time     # 真的等退避，感受一下有多慢
"""

from lab_core import (
    CLOCK,
    MODELS,
    POLICIES,
    CircuitBreaker,
    FakeVendor,
    Gateway,
    Task,
    backoff_delay,
    banner,
    build_vendors,
    clip,
    live_hint,
    new_trace,
    now,
    print_table,
    route,
    section,
    set_log,
    table,
    title_line,
)
from v1_naive import REQUESTS

# 同一个业务任务：要 JSON、要能调工具、质量不低于 70、数据不能出境
TASK = Task(kind="final", needs=frozenset({"json", "tools"}), min_quality=70, region="cn")

# 场景 B 故意放宽质量门槛，让最便宜的 cheap-mini（也就是 V1 用的那个）当主候选，
# 这样你才能看到「主候选挂了 -> 重试 -> 熔断 -> 降级」的全过程。
# min_quality=60 的作用：把自托管的 local-oss（质量 55、价格为 0）筛掉，
# 否则「成本优先」会选价格 0 的自托管模型，你就看不到 vendorA 的故障了。
FAILURE_TASK = Task(kind="final", needs=frozenset({"json"}), min_quality=60, region="cn")


def scenario_a() -> None:
    section("场景 A：路由不是「永远用最强模型」，而是「约束内最优化」")
    print(f"  任务画像：needs={sorted(TASK.needs)} min_quality={TASK.min_quality} region={TASK.region}")
    print()
    rows = []
    for policy in POLICIES:
        spec, _ = route(TASK, policy)
        rows.append([policy, spec.model_id, f"{spec.price_out} 元/M",
                     f"{spec.p50_ms}ms", spec.quality, spec.caps_text, spec.region])
    print_table(["策略", "选中模型", "输出单价", "P50 延迟", "质量", "能力", "区域"], rows,
                title="同一任务的四种路由结果")

    _, rejected = route(TASK, "cost_first")
    print()
    print("  被淘汰的候选及原因（这才是路由设计的核心，面试要能讲清）：")
    for item in rejected:
        print(f"      - {item}")

    print()
    print("  换一个「数据可以出境、且要求高准确率」的任务，结果立刻不同")
    print("  （注意 cost_first 这次也选强模型：约束把便宜的都筛掉了 —— 约束内最优化，不是永远最便宜）：")
    offshore = Task(kind="code", needs=frozenset({"json", "tools", "long"}),
                    min_quality=90, region="any")
    for policy in ("quality_first", "cost_first"):
        spec, _ = route(offshore, policy)
        print(f"      {policy:<14} -> {spec.model_id}（质量 {spec.quality}，"
              f"{spec.price_out} 元/M，{spec.region}）")

    print()
    print("  负载感知：vendorA 排队已达 90%，同一个任务会绕开它：")
    spec, _ = route(TASK, "load_aware", load={"vendorA": 0.9, "vendorB": 0.1, "vendorC": 0.1})
    print(f"      load_aware -> {spec.model_id}（供应商 {spec.provider}）")

    print()
    print(table(["模型", "供应商", "输入单价", "输出单价", "rpm", "P50", "质量", "能力", "区域"],
                [[s.model_id, s.provider, s.price_in, s.price_out, s.rpm, f"{s.p50_ms}ms",
                  s.quality, s.caps_text, s.region] for s in MODELS.values()],
                title="配置中心（这张表就是文档 1.2.1 说的 model_id -> 端点/限额/价格表）"))


def scenario_b() -> None:
    section("场景 B：把 V1 那 10 个请求重跑一遍（V1 失败率 40%）")
    print("  供应商剧本：vendorA 前 3 次都 429（模拟限流），熔断阈值=3，冷却=15s")
    print("  网关配置：失败阈值 3，冷却 15s，最多重试 3 次，退避 0.3s 起")
    print()

    vendors = build_vendors()
    vendors["vendorA"] = FakeVendor("vendorA", events=["429", "429", "429", "ok"],
                                    seed=7, latency_scale=0.6)
    gw = Gateway(vendors, policy="cost_first", max_retries=3, base_delay=0.3,
                 breaker_kwargs={"failure_threshold": 3, "open_seconds": 15.0,
                                 "half_open_max_calls": 1, "success_threshold": 2})

    chain = gw.plan(FAILURE_TASK)
    print("  降级链（注意第二跳换到了另一家供应商 —— 同一家全挂时，"
          "「同厂商的弱模型」救不了你）：")
    print("      " + "  ->  ".join(f"{s.model_id}({s.provider},质量{s.quality})" for s in chain))
    print()

    ok, degraded, fallback = 0, 0, 0
    rows = []
    for i, (tenant, question) in enumerate(REQUESTS, 1):
        new_trace(f"req{i:02d}")
        set_log(i == 1)  # 只打开第 1 个请求的结构化日志，避免刷屏
        result = gw.chat([{"role": "user", "content": question}], task=FAILURE_TASK,
                         tenant=tenant, step="answer")
        set_log(True)
        if result.provider == "static":
            fallback += 1
        else:
            ok += 1
        degraded += 1 if result.degraded else 0
        rows.append([f"{i:02d}", tenant, result.model, result.provider,
                     "降级" if result.degraded else "主链路",
                     f"{result.attempts} 次", f"{result.waited_s:.2f}s",
                     clip(result.text, 24)])
    print()
    print(table(["#", "租户", "最终模型", "供应商", "链路", "尝试", "退避等待", "用户看到的答案"],
                rows, title="10 个请求的最终归宿"))

    print()
    print(f"  结果：成功 {ok} 个（其中降级 {degraded} 个），兜底文案 {fallback} 个，"
          f"用户可见失败率 {fallback / len(REQUESTS):.0%}")
    print(f"  对比 V1：失败率 40% -> {fallback / len(REQUESTS):.0%}（代价是部分请求变慢、变弱，但可用）")
    print()
    print("  熔断器与网关指标：")
    for line in gw.breaker_view():
        print(f"      {line}")
    print(f"      重试 {gw.metrics['retry']} 次 / 熔断拒绝 {gw.metrics['breaker_reject']} 次 / "
          f"降级 {gw.metrics['degrade']} 次 / 兜底 {gw.metrics['fallback']} 次")
    print("      注意：重试也是要花钱的，每次重试都是一次 GPU 时间。V3 会把这笔账记清楚。")

    print()
    print("  最后看一眼「恢复」：冷却时间过去后，半开探测成功 -> 关掉熔断 -> 回到最便宜的主模型")
    CLOCK.advance(16)
    new_trace("req-recover")
    set_log(False)
    recovered = gw.chat([{"role": "user", "content": "订单 A1001 的状态？"}], task=FAILURE_TASK)
    set_log(True)
    print(f"      恢复后走的模型：{recovered.model}（{recovered.provider}），"
          f"链路：{'降级' if recovered.degraded else '主链路'}")


def scenario_c() -> None:
    section("场景 C：熔断三态（文档 1.2.3）")
    transitions = []
    breaker = CircuitBreaker(
        "vendorA:cheap-mini", failure_threshold=3, open_seconds=15.0,
        half_open_max_calls=1, success_threshold=2,
        on_change=lambda name, old, reason: transitions.append(
            f"{name}: {old} -> {breaker.state}（{reason}）"),
    )

    print("  ① 连续失败打满阈值 -> 跳闸")
    for i in range(1, 4):
        if not breaker.allow():
            print(f"     第 {i} 次调用被拒（不该发生）")
            continue
        breaker.before_call()
        breaker.on_failure(RuntimeError("500"))
        if breaker.state == "OPEN":
            print(f"     第 {i} 次失败 -> 打满阈值：状态 OPEN，失败计数清零并进入 "
                  f"{breaker.open_seconds:.0f}s 冷却")
        else:
            print(f"     第 {i} 次失败，状态 {breaker.state}（累计失败 {breaker.failures}）")

    print("  ② OPEN 期间的请求：快速失败，一次都不碰下游（这就是防雪崩）")
    rejected = sum(0 if breaker.allow() else 1 for _ in range(5))
    print(f"     5 个请求里 {rejected} 个被直接拒绝，状态 {breaker.state}，"
          f"冷却剩余 {max(0, breaker.open_until - now()):.1f}s")

    print("  ③ 冷却结束 -> HALF_OPEN，只放 1 个探测（半开不放满流量）")
    CLOCK.advance(16)
    print(f"     allow() = {breaker.allow()}，状态 {breaker.state}")
    breaker.before_call()
    print(f"     探测期间再来的请求：allow() = {breaker.allow()}（并发上限 1，直接拒掉）")
    breaker.on_success()
    print(f"     第 1 次探测成功，累计 {breaker.half_success}/{breaker.success_threshold}，"
          f"状态 {breaker.state}")

    print("  ④ 探测继续成功到阈值 -> 回 CLOSED")
    breaker.before_call()
    breaker.on_success()
    print(f"     状态 {breaker.state}（半开阶段连续成功 {breaker.success_threshold} 次）")

    print()
    print("  状态迁移流水（on_change 回调打出来的，生产上要进日志/告警）：")
    for line in transitions:
        print(f"      {line}")


def scenario_d() -> None:
    section("场景 D：不可重试的错误，重试只是浪费钱（文档 1.2.5）")
    vendors = build_vendors()
    vendors["vendorA"] = FakeVendor("vendorA", events=["401"], seed=5, latency_scale=0.4)
    new_trace("req401")
    set_log(False)
    gw = Gateway(vendors, policy="cost_first", max_retries=3)
    set_log(True)
    result = gw.chat([{"role": "user", "content": "订单 A1001 的状态？"}], task=FAILURE_TASK)
    set_log(False)
    print(f"  最终由 {result.model}（{result.provider}）回答：{clip(result.text, 30)}")
    print(f"  重试次数 = {gw.metrics['retry']}，不可重试直接换策略 = {gw.metrics['non_retryable']}")
    print("  如果一个系统对 401 也退避重试 3 次：用户等 0.3+0.6+1.2 秒，钱照付，结果一样失败。")


def scenario_e() -> None:
    section("场景 E：jitter 防的是「重试风暴」")
    import random as _random

    base, cap = 0.5, 8.0
    no_jitter = [min(cap, base * (2 ** 2)) for _ in range(20)]
    rng = _random.Random(42)
    with_jitter = [backoff_delay(2, base, cap, jitter=True, rng=rng) for _ in range(20)]

    def histogram(values, buckets=6):
        lo, hi = min(values), max(values)
        step = (hi - lo) / buckets or 1.0
        counts = [0] * buckets
        for v in values:
            counts[min(buckets - 1, int((v - lo) / step))] += 1
        return [(f"{lo + i * step:.2f}~{lo + (i + 1) * step:.2f}s", counts[i]) for i in range(buckets)]

    for name, values in (("没有 jitter", no_jitter), ("有 jitter", with_jitter)):
        print(f"  {name}（20 个客户端同一时刻失败、同时重试）：")
        for bucket, count in histogram(values):
            print(f"      {bucket:<16} {'#' * count} {count}")
        print(f"      同一秒内重试的客户端数：{max(histogram(values), key=lambda kv: kv[1])[1]}")
    print()
    print("  没有 jitter：20 个请求整整齐齐砸向刚缓过来的下游 -> 再打挂一次（惊群）。")
    print("  有 jitter：重试被摊到时间轴上，下游的瞬时压力小一个数量级。")


def main() -> None:
    banner("V2｜路由 + 重试退避 + 熔断 + 降级：让用户不再看见 500",
           [live_hint(False, "本版故障要可复现（第 3 个请求必须 429），固定用假供应商；想看真实模型跑 v3"), "结构：Provider Adapter -> 路由 -> 熔断 -> 重试 -> 降级链 -> 记账"])
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    title_line("V2 结论")
    print("  第 1 节的闭环现在是可运行的了：route -> breaker -> retry(backoff+jitter) -> degrade。")
    print("  但它仍然回答不了「这笔钱花在哪了、这次为什么慢」——因为还没有 V3 的可观测性与账本。")


if __name__ == "__main__":
    main()