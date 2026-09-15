# -*- coding: utf-8 -*-
"""
V3 | 全链路可观测性 + 成本账本 + 缓存

对标文档《08-工程化实践》：
    第 3 节 3.2.1 结构化日志 / 3.2.2 Trace 与 Span / 3.2.3 每个 Agent 步骤的记录 / 3.2.5 自定义 Trace
    第 2 节 2.2.2 Prompt 精简、2.2.3 缓存策略（精确 + 语义）、2.2.5 成本监控与告警

四个场景：
  A  一次请求的完整 trace：span 树 + 瓶颈定位 + 结构化日志（可 grep）
  B  成本账本：按 模型 / 租户 / 步骤 聚合，以及预算告警
  C  路由策略对账单的影响：同一个工作流，成本优先 vs 质量优先差多少钱
  D  缓存：精确缓存 -> 语义缓存 -> 模型；顺便看语义缓存怎么「串味」

运行：
    python v3_observe.py
    python v3_observe.py --live     # 用真实模型跑，看真实的 token 与费用
"""

from lab_core import (
    LEDGER,
    Completion,
    ExactCache,
    SemanticCache,
    Task,
    Tracer,
    banner,
    build_gateway,
    budget_check,
    clip,
    embed_stub,
    live_hint,
    log_event,
    money,
    new_trace,
    print_table,
    section,
    set_log,
    table,
    title_line,
    cosine,
)
from guard_kit import mask_pii

PROMPT_VERSION = "v3-prompt-20260401"   # Prompt 版本要进缓存键、trace 和账单
TOOLS_VERSION = "tools-7"              # 工具清单版本同理

TASK = Task(kind="final", needs=frozenset({"json", "tools"}), min_quality=60, region="cn")

WORKLOAD = [
    ("T-1001", "订单接口支持搜索吗", "answer"),
    ("T-1001", "订单接口支持搜索吗？", "answer"),
    ("T-1002", "退款要多久到账", "answer"),
    ("T-1002", "退货要多久到账", "answer"),
    ("T-1003", "数据库连不上时接口怎么办", "answer"),
    ("T-1003", "帮我查订单 A1001", "tool"),
    ("T-1001", "订单列表会返回手机号吗", "answer"),
    ("T-1002", "发票怎么开", "answer"),
    ("T-1003", "搜索支持分页吗", "answer"),
    ("T-1001", "订单接口支持搜索吗", "answer"),
]


# ===========================================================================
# 一条「检索 + 模型 + 工具」的最小链路，每一步都留痕
# ===========================================================================
def handle(gateway, tracer, exact, semantic, tenant: str, question: str, step: str):
    new_trace()
    cache_key = exact.key(tenant, "cheap-mini", PROMPT_VERSION, TOOLS_VERSION,
                          [{"role": "user", "content": question}])
    # 注意：连 trace 的属性都要脱敏 —— 日志、trace、审计是同一条泄漏路径
    with tracer.span("agent.handle", tenant=tenant,
                     question=clip(mask_pii(question), 34)) as sp:
        text = None

        with tracer.span("retrieve.search", top_k=3):
            snippets = ["订单接口支持按关键词搜索订单，并支持分页返回。",
                        "订单列表的返回体不包含手机号等个人信息。"]
            tracer.log("retrieve.hit", ms=12, docs=len(snippets))
            log_event("INFO", "retrieve_done", tenant=tenant, docs=len(snippets), latency_ms=12)

        with tracer.span("cache.lookup") as csp:
            text = exact.get(cache_key)
            if text:
                csp.note("exact", "hit")
                tracer.log("cache.hit", ms=0.1, layer="L1/exact")
                log_event("INFO", "cache_hit", tenant=tenant, layer="exact")
            else:
                csp.note("exact", "miss")
                sem = semantic.get(question, tenant=tenant)
                if sem:
                    text = sem
                    csp.note("semantic", f"hit(sim={semantic.last_similarity:.3f})")
                    tracer.log("cache.hit", ms=0.3, layer="semantic",
                               sim=round(semantic.last_similarity, 3),
                               matched=clip(semantic.last_match, 16))
                    log_event("INFO", "cache_hit", tenant=tenant, layer="semantic",
                              sim=round(semantic.last_similarity, 3))
                else:
                    csp.note("semantic", f"miss(sim={semantic.last_similarity:.3f})")

        if text is None:
            result = gateway.chat([{"role": "user", "content": question}], task=TASK,
                                  tenant=tenant, step=step)
            text = result.text
            # 回填两层缓存：精确键 + 语义向量
            exact.set(cache_key, text)
            semantic.set(question, text, tenant=tenant)
            log_event("INFO", "llm_done", tenant=tenant, model=result.model,
                      tokens=result.prompt_tokens + result.completion_tokens,
                      cost=round(result.cost, 6), degraded=result.degraded,
                      preview=clip(mask_pii(text), 24))
        else:
            result = Completion(text=text, prompt_tokens=0, completion_tokens=0,
                                provider="cache", model="cache", latency_ms=0.0, cached=True)
            LEDGER.add(tenant=tenant, model="cache", step=step, provider="cache",
                       prompt_tokens=0, completion_tokens=0, cost=0.0, cached=True)
            exact.set(cache_key, text)

        with tracer.span("tools.call", tool="query_order"):
            tracer.log("tool.ok", ms=9, tool="query_order")

        with tracer.span("ledger.write"):
            tracer.log("ledger.entry", ms=0.2, cost=round(result.cost, 6),
                       model=result.model, cached=result.cached)
        sp.note("model", result.model)
        sp.note("cached", result.cached)
    return result


def scenario_a() -> None:
    section("场景 A：一次请求的 trace —— 出错时你到底要知道什么")
    tracer = Tracer("req-01")
    gw = build_gateway(tracer=tracer)
    exact, semantic = ExactCache(), SemanticCache(threshold=0.95)

    question = "订单接口支持搜索吗？用户手机号 13812345678，邮箱 a.b@example.com"
    print("  用户原始输入（注意里面有 PII，日志里绝对不能原样落盘）：")
    print(f"      {question}")
    print(f"  日志里应该长这样：{mask_pii(question)}")
    print()

    print("  下面是这次请求真实产生的结构化日志（set_log(True)，生产上就照这个格式进日志系统）：")
    set_log(True)
    result = handle(gw, tracer, exact, semantic, "T-1001", question, "answer")
    set_log(False)

    print()
    print("  结构化日志（一行一条 JSON，可直接 grep / jq / 进 ELK）：")
    log_event("INFO", "request_done", tenant="T-1001", model=result.model,
              latency_ms=round(result.latency_ms, 1), tokens=result.prompt_tokens + result.completion_tokens,
              cost=round(result.cost, 6), degraded=result.degraded)
    print()
    print("  Trace 树（父子关系 = 因果链；最耗时的那段会被标出来）：")
    tracer.dump()

    print()
    print(table(["没有 trace 时你会说的话", "有了 trace 之后你能说的话"],
                [["「这个接口有点慢」", "慢在 llm.call：1140ms，占整条链路的 96%"],
                 ["「刚才报错了」", "error_code=429、model=cheap-mini、attempt=2/3、已降级"],
                 ["「这个请求怎么这么贵」", "prompt 812 tokens、模型 backup-cn、0.0012 元"],
                 ["「用户说答案是错的」", "retrieve 命中的是 [2] 号片段，引用可回查"]],
                title="同一件事，两种回答方式"))


def scenario_b() -> None:
    section("场景 B：成本账本 —— 钱到底花在哪个模型、哪个租户、哪一步")
    tracer = Tracer("budget", quiet=True)
    gw = build_gateway(tracer=tracer)
    # 让 vendorA 在第 3~5 次连着挂：账本里才会同时出现「主模型」和「降级模型」
    from lab_core import FakeVendor
    gw.vendors["vendorA"] = FakeVendor("vendorA", events=["ok", "ok", "429", "429", "429", "ok"],
                                       seed=7, latency_scale=0.5)
    exact, semantic = ExactCache(ttl_s=600), SemanticCache(threshold=0.92)
    LEDGER.reset()

    for tenant, question, step in WORKLOAD[:6]:
        handle(gw, tracer, exact, semantic, tenant, question, step)

    print(LEDGER.table("model", title="按模型看（小模型干子任务、大模型做决策，账本会替你说话）"))
    print()
    print(LEDGER.table("tenant", title="按租户看（谁在烧钱 / 该不该给这个租户单独限流）"))
    print()
    print(LEDGER.table("step", title="按步骤看（哪一类功能最贵）"))
    print()
    print(f"  总计：{LEDGER.report()}")
    print(f"  单次请求平均：{money(LEDGER.total_cost / max(1, LEDGER.calls))}"
          f" | P95 之类要靠 trace 里的 latency_ms 分位统计")

    import lab_core
    lab_core.BUDGET = round(LEDGER.total_cost * 0.8, 6)   # 演示：把预算调成当前的 80%
    warn = budget_check()
    print()
    print(f"  预算告警演示（.env 里 LLM_BUDGET_YUAN 就是它的生产版）：{warn}")


def scenario_c(policies: tuple = ("cost_first", "quality_first")) -> None:
    section("场景 C：路由策略直接决定账单（同一批请求，换策略再跑一遍）")
    rows = []
    for policy in policies:
        LEDGER.reset()
        gw = build_gateway(policy=policy)
        exact, semantic = ExactCache(), SemanticCache(threshold=0.92)
        for tenant, question, step in WORKLOAD:
            handle(gw, Tracer("x"), exact, semantic, tenant, question, step)
        rows.append([policy, gw.plan(TASK)[0].model_id, str(LEDGER.calls),
                     money(LEDGER.total_cost), str(LEDGER.cached_calls)])
    LEDGER.reset()
    print_table(["策略", "主模型", "调用次数", "总花费", "缓存命中"], rows,
                title="同一批请求、两种策略")


def scenario_d() -> None:
    section("场景 D：缓存分层（精确 -> 语义 -> 模型）与它的风险")
    gw = build_gateway()
    exact, semantic = ExactCache(ttl_s=600), SemanticCache(threshold=0.92)
    tracer = Tracer("cache")
    LEDGER.reset()

    print("  ① 精确缓存：同一个租户、同一个 Prompt 版本、同一句话")
    for i in range(2):
        r = handle(gw, tracer, exact, semantic, "T-1001", "订单接口支持搜索吗", "answer")
        print(f"     第 {i + 1} 次：{'缓存命中' if r.cached else '走了模型'} | "
              f"花费 {money(r.cost)}")
    print(f"     账本：{LEDGER.report()}")

    print()
    print("  ② 语义缓存：措辞不同但足够相似（阈值 0.92）")
    q = "订单接口支持搜索吗？"
    r = handle(gw, tracer, exact, semantic, "T-1001", q, "answer")
    print(f"     「{q}」-> {'语义缓存命中' if r.cached else '未命中'}，"
          f"相似度 {semantic.last_similarity:.3f}")
    print(f"     注：这里用的是「假 embedding」（字符 bigram 哈希），只抓字面；"
          f"真实 embedding 下「这个接口能搜订单吗」也会命中。")

    print()
    print("  ③ 租户隔离：换个租户问同一句话，绝不允许复用别人的答案")
    r = handle(gw, tracer, exact, semantic, "T-9999", "订单接口支持搜索吗", "answer")
    print(f"     T-9999 -> {'命中（不应该发生！）' if r.cached else '未命中（正确，租户隔离生效）'}")

    print()
    print("  ④ 风险演示：把阈值放松到 0.6，语义缓存立刻「串味」")
    loose = SemanticCache(threshold=0.6)
    loose.set("退款要多久到账", "退款一般 1-3 个工作日到账。", tenant="T-1002")
    sim = cosine(embed_stub("退货要多久到账"), embed_stub("退款要多久到账"))
    got = loose.get("退货要多久到账", tenant="T-1002")
    print(f"     库里存的是「退款要多久到账」，用户问的是「退货要多久到账」，"
          f"相似度 {sim:.3f} >= 阈值 0.6")
    print(f"     用户拿到的答案：{got}")
    print("     -> 退款和退货是两个流程，这就是文档说的「相似但意图不同」的坑。")
    print()
    print("  加固手段（文档 2.4 / Q17）：")
    print("     - 缓存键包含 租户 + 模型 + Prompt 版本 + 工具集版本（本文件就是这么做的）")
    print("     - 阈值保守 + 先做意图分类再匹配")
    print("     - 医疗/法律/资金这类敏感场景直接禁用语义缓存，或命中后二次确认")
    print("     - TTL + 主动失效：模型或知识库更新后必须能脏掉旧答案")


def main() -> None:
    banner("V3｜全链路可观测性 + 成本账本 + 缓存",
           [live_hint(False),
            "结构：trace(span 树) + 结构化日志 + 账本(租户/模型/步骤) + 精确/语义缓存"])
    set_log(False)
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    set_log(True)
    title_line("V3 结论")
    print("  到这里，你能回答「哪个租户在烧钱、这次为什么慢、这个请求答得对不对」。")
    print("  但系统仍然会**老实照做模型说的每一句**——V4 给工具调用加上门禁。")


if __name__ == "__main__":
    main()