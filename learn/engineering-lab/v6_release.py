# -*- coding: utf-8 -*-
"""
V6 | 部署与运维：怎么把这些东西安全地发上线

对标文档《08-工程化实践》第 5 节：
    5.2.1 Docker / 5.2.2 Kubernetes / 5.2.3 CI-CD / 5.2.4 蓝绿与金丝雀
    5.2.5 模型版本管理 / 5.2.6 A/B 测试；面试题 Q9、Q20、Q21 与追问「长任务怎么不中断」

五个场景：
  A  健康检查：/health 到底该检查什么（探活 vs 就绪）
  B  金丝雀发布：5% -> 25% -> 50% -> 100%，指标超阈值就自动回滚
  C  质量回归导致回滚：这次不是报错，而是「悄悄变差」（幻觉率上升）
  D  优雅停机 + checkpoint：长任务被打断后从断点继续，而不是从头再来
  E  蓝绿 vs 金丝雀：怎么选

运行：
    python v6_release.py
"""

import json

from lab_core import (
    LEDGER,
    FakeVendor,
    Gateway,
    MODELS,
    Task,
    banner,
    build_vendors,
    live_hint,
    money,
    new_trace,
    print_table,
    route,
    section,
    set_log,
    table,
    title_line,
)
from eval_kit import CASES, GROUNDED_PROMPT, NAIVE_PROMPT, rule_score
from guard_kit import mask_pii

TASK = Task(kind="final", needs=frozenset({"json", "tools"}), min_quality=60, region="cn")

# 服务质量目标（SLO）：金丝雀发布时就是拿这几个数去比
SLO = {
    "错误率": 0.05,
    "P95 延迟": 2000.0,
    "单次成本(元)": 0.0015,
    "幻觉/引用失败率": 0.05,
}

# 三个版本：现网、一个好的金丝雀、一个「悄悄变差」的金丝雀
VARIANTS = {
    "stable-v1": {
        "label": "现网 stable-v1",
        "prompt": GROUNDED_PROMPT,
        "model": "backup-cn",
        "prompt_version": "prompt-20260310",
        "image": "agent-api:v20260310",
        "events": ["ok"] * 19 + ["429"],   # 现网的偶发抖动
    },
    "canary-good": {
        "label": "金丝雀 good（新 Prompt + 更强的模型）",
        "prompt": GROUNDED_PROMPT,
        "model": "balanced-pro",
        "prompt_version": "prompt-20260401",
        "image": "agent-api:v20260401",
        "events": ["ok"] * 20,
    },
    "canary-bad": {
        "label": "金丝雀 bad（新 Prompt 丢了引用约束）",
        "prompt": NAIVE_PROMPT,
        "model": "balanced-pro",
        "prompt_version": "prompt-20260402",
        "image": "agent-api:v20260402",
        "events": ["ok"] * 9 + ["500"],    # 顺便多了点 5xx
    },
}

QUESTION = "订单接口的分页上限是多少？"


# ===========================================================================
# 场景 A：健康检查
# ===========================================================================
def healthz() -> dict:
    """探活 + 就绪检查。

    /live  只回答「进程还活着吗」——它绝不能依赖下游，
           否则下游抖一下，K8s 会把所有 Pod 一起杀掉重启（雪崩的经典来源）。
    /ready 回答「我现在能接流量吗」——依赖不通就摘掉流量，等恢复。
    """
    checks = []
    spec, _ = route(TASK, "cost_first")
    checks.append(("进程存活", True, "live：进程没卡死（真实项目用事件循环心跳）"))

    ok_vendor = bool(spec and spec.provider in build_vendors())
    checks.append(("模型可路由", ok_vendor,
                   f"ready：路由能选出 {spec.model_id if spec else '无'}，供应商已接入"))

    l2_ok = True  # 这里模拟 Redis ping
    checks.append(("L2 缓存可达", l2_ok, "ready：Redis PING 正常（不通就摘流量，别让请求全打回源）"))

    queue = 3
    checks.append(("队列深度", queue < 50, f"ready：待处理 {queue} 个（积压过高应拒绝新流量/扩容）"))

    budget_ok = LEDGER.total_cost < 1.0
    checks.append(("成本未超预算", budget_ok, f"ready：已花费 {money(LEDGER.total_cost)}"))
    return {"checks": checks, "live": True, "ready": all(ok for _, ok, _ in checks)}


def scenario_a() -> None:
    section("场景 A：健康检查（K8s 探针不该只是一个 200）")
    report = healthz()
    print(table(["检查项", "结果", "说明"],
                [[name, "OK" if ok else "FAIL", hint] for name, ok, hint in report["checks"]]))
    print()
    print(f"  /live  -> {'200' if report['live'] else '503'}"
          f"（只表示进程活着，不检查下游）")
    print(f"  /ready -> {'200' if report['ready'] else '503'}"
          f"（能接流量才返回 200）")
    print()
    print("  对应的 K8s 片段（完整版在 deploy/deployment.yaml）：")
    print("      readinessProbe: httpGet: {path: /ready, port: 8000}, initialDelaySeconds: 5")
    print("      livenessProbe:  httpGet: {path: /live,  port: 8000}, periodSeconds: 10")
    print()
    print("  另外两个 Agent 服务特有的坑：")
    print("      - 冷启动慢（要预载模型/连向量库）-> initialDelaySeconds 给足，别让 K8s 反复重启你；")
    print("      - 长任务在跑（一次 Agent 任务 30s+）-> 停机时别硬杀，见场景 D。")


# ===========================================================================
# 场景 B/C：金丝雀发布
# ===========================================================================
def measure_variant(key: str, spec_cfg: dict, requests: int) -> dict:
    """跑一批请求，量出四个指标：错误率 / P95 / 单次成本 / 引用（幻觉）失败率。"""
    vendors = build_vendors()
    vendor_name = MODELS[spec_cfg["model"]].provider
    vendors[vendor_name] = FakeVendor(vendor_name, events=list(spec_cfg["events"]), seed=11,
                                      latency_scale=1.0)
    gw = Gateway(vendors, policy="cost_first", max_retries=2, base_delay=0.1,
                 breaker_kwargs={"failure_threshold": 5, "open_seconds": 10.0})

    name = spec_cfg["label"]
    case = next(c for c in CASES if c.require_citation)  # 带引用检查的那条用例
    latencies, costs, errors, cite_fail, retries = [], [], 0, 0, 0
    for i in range(requests):
        new_trace(f"{name}-{i:02d}")
        result = gw.chat([{"role": "system", "content": spec_cfg["prompt"]},
                          {"role": "user", "content": QUESTION}],
                         task=TASK, tenant=f"T-10{i % 3 + 1}", step="answer",
                         model=spec_cfg["model"])
        latencies.append(result.latency_ms)
        costs.append(result.cost)
        retries += result.attempts - 1
        if result.provider == "static":
            errors += 1
        if not rule_score(case, result.text).passed:
            cite_fail += 1

    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0.0
    return {
        "版本": name,
        "镜像": spec_cfg["image"],
        "Prompt 版本": spec_cfg["prompt_version"],
        "模型": spec_cfg["model"],
        "样本": requests,
        "错误率": errors / requests,
        "P95 延迟": p95,
        "单次成本(元)": sum(costs) / requests,
        "幻觉/引用失败率": cite_fail / requests,
        "重试": retries,
    }


def judge(metrics: dict) -> tuple:
    """拿指标和 SLO 比。返回 (是否通过, 违规说明)。"""
    bad = []
    for key, limit in SLO.items():
        value = metrics[key]
        if value > limit:
            bad.append(f"{key} {value:.3f} > SLO {limit}")
    return (not bad), bad


def rollout(variant_key: str, steps) -> None:
    variant = VARIANTS[variant_key]
    rows = []
    rolled_back = False
    for weight in steps:
        n_canary = max(3, int(round(30 * weight)))
        n_stable = 30 - n_canary
        # 100% 流量时没有对照组的必要（也就没有对照组可测）
        stable = measure_variant("stable-v1", VARIANTS["stable-v1"], n_stable) if n_stable else None
        canary = measure_variant(variant_key, variant, n_canary)
        ok, bad = judge(canary)
        rows.append([
            f"{weight:.0%}", f"{n_stable}/{n_canary}",
            f"{stable['错误率']:.0%}" if stable else "-", f"{canary['错误率']:.0%}",
            f"{canary['P95 延迟']:.0f}ms",
            f"{canary['单次成本(元)']:.5f}",
            f"{canary['幻觉/引用失败率']:.0%}",
            "放量" if ok else "回滚",
        ])
        if not ok:
            rolled_back = True
            print(f"  [告警] 金丝雀在 {weight:.0%} 流量时违规：{'；'.join(bad)}")
            print("  [动作] 自动回滚：权重置 0，流量 100% 回到 stable-v1，保留现场（trace + 这批评测结果）")
            break
        print(f"  {weight:>4.0%} 流量观察窗口："
              f"错误率 {canary['错误率']:.0%}、P95 {canary['P95 延迟']:.0f}ms、"
              f"单次成本 {canary['单次成本(元)']:.5f} 元、引用失败率 "
              f"{canary['幻觉/引用失败率']:.0%}，指标正常，继续放量")

    print()
    print_table(["流量", "stable/canary", "stable 错误率", "canary 错误率", "canary P95",
                 "canary 单次成本", "引用失败率", "决策"], rows,
                title="金丝雀发布观察表（每个窗口 30 个请求，虚拟时钟）")
    print()
    if rolled_back:
        print("  回滚之后要做的三件事：")
        print("      1) 从 trace 里捞失败样本（带 prompt_version 与 model，见场景 C）；")
        print("      2) 回到评估集上跑一遍回归测试（V7 的门禁就是这么用的）；")
        print("      3) 修好之后再从 5% 开始，而不是「这次直接上 50%」。")
    else:
        print("  全部窗口通过 -> 提升为正式版本（镜像 tag 带日期，别用 latest）。")


def scenario_b() -> None:
    section("场景 B：金丝雀发布 —— 先放 5% 的流量去替你踩雷")
    print("  stable-v1：现网（引用约束 Prompt + backup-cn，P95 约 1.1s）")
    print("  canary-good：新版本（引用约束 Prompt + balanced-pro，更快但更贵）")
    print("  观察指标：错误率 / P95 / 单次成本 / 引用（幻觉）失败率，任一超 SLO 就回滚")
    print()
    rollout("canary-good", [0.05, 0.25, 0.50, 1.0])
    print()
    print("  顺带一提：这个版本质量没退、只是单价从 6 元/M 变成 12 元/M。")
    print("  如果成本 SLO 收紧到 0.0008 元/次，它就会被「成本门禁」拦下 ——")
    print("  质量没退但成本翻倍，同样不该悄悄发出去（V7 的 cost_gate 就是干这个的）。")


def scenario_c() -> None:
    section("场景 C：最危险的不是报错，是「悄悄变差」")
    print("  这次的金丝雀把引用约束 Prompt 弄丢了（现实中最常见的回归来源）")
    print()
    rollout("canary-bad", [0.10])
    print()
    stable = measure_variant("stable-v1", VARIANTS["stable-v1"], 20)
    canary = measure_variant("canary-bad", VARIANTS["canary-bad"], 20)
    print_table(["版本", "Prompt 版本", "模型", "错误率", "P95", "单次成本", "引用失败率"],
                [[m["版本"], m["Prompt 版本"], m["模型"], f"{m['错误率']:.0%}",
                  f"{m['P95 延迟']:.0f}ms", f"{m['单次成本(元)']:.5f}",
                  f"{m['幻觉/引用失败率']:.0%}"] for m in (stable, canary)])
    print()
    print(f"  注意 canary 的错误率是 {canary['错误率']:.0%}（接口全都 200），"
          f"但引用失败率到了 {canary['幻觉/引用失败率']:.0%} ——")
    print("  接口很健康，答案是编的。这就是为什么文档 Q9 要求金丝雀还要看")
    print("  「工具失败率 / 重试率 / 熔断率 / 幻觉率 / 任务完成率」这些业务与质量指标。")
    print()
    print("  版本化的价值（文档 5.2.5）：这条 trace 里同时记了")
    print("      image tag = agent-api:v20260402、prompt_version = prompt-20260402、model = balanced-pro。")
    print("      于是「从哪个版本开始变差」是一句 SQL，而不是一场会议。")


# ===========================================================================
# 场景 D：优雅停机 + checkpoint
# ===========================================================================
STEPS = ["解析需求", "检索资料", "生成草稿", "事实核查", "写入 CRM"]


class GracefulStop(Exception):
    pass


def run_task(checkpoint: dict, stop_after: int) -> dict:
    """一次长任务。stop_after=2 表示「跑到第 2 步时收到 SIGTERM」。"""
    start = checkpoint.get("done", 0)
    trace_id = checkpoint.get("trace_id") or new_trace()
    for i in range(start, len(STEPS)):
        if i == stop_after:
            raise GracefulStop(f"收到 SIGTERM：在第 {i} 步之后保存 checkpoint 并退出")
        checkpoint[f"step_{i}"] = f"{STEPS[i]} 的产物"
        checkpoint["done"] = i + 1
        checkpoint["trace_id"] = trace_id
    checkpoint["done"] = len(STEPS)
    return checkpoint


def scenario_d() -> None:
    section("场景 D：优雅停机 + checkpoint（长任务不能从头再来）")
    print("  任务：5 个步骤的 Agent 流水线；第 2 步之后 Pod 被回收（滚动更新/缩容）")
    print()

    rows = []
    for label, use_ckpt in (("不保存状态（简单粗暴）", False), ("保存 checkpoint", True)):
        checkpoint: dict = {}
        saved: dict = {}
        interrupted_at = len(STEPS)
        try:
            run_task(checkpoint, stop_after=2)
        except GracefulStop:
            interrupted_at = checkpoint.get("done", 0)
            if use_ckpt:
                saved = dict(checkpoint)   # 落到 Redis/DB，而不是留在 Pod 内存里

        start_step = interrupted_at if use_ckpt else 0
        final = run_task(dict(saved) if use_ckpt else {}, stop_after=99)
        total_steps = interrupted_at + (len(STEPS) - start_step)
        wasted = 0 if use_ckpt else interrupted_at
        detail = (f"新 Pod 从第 {start_step + 1} 步继续（{STEPS[start_step]}）" if use_ckpt
                  else f"新 Pod 从第 1 步重来，白跑 {wasted} 步")
        rows.append([label, f"{interrupted_at}/5 步后中断", detail,
                     f"{total_steps} 步（白跑 {wasted} 步）"])

    print_table(["停机处理", "中断点", "恢复方式", "总工作量"], rows)
    print()
    print("  工程要点：")
    print("      - K8s 的 terminationGracePeriodSeconds 要大于「一个消息的处理时间」，")
    print("        并让应用先停止接收新消息、再把手上的活干完；")
    print("      - checkpoint 要写在**任务状态**里（Redis/DB），不是写进 Pod 内存；")
    print("      - 幂等：重放的那一步必须可以安全地再执行一次（否则退款会被退两次）。")


def scenario_e() -> None:
    section("场景 E：蓝绿 vs 金丝雀（面试必答的取舍）")
    rows = [
        ["蓝绿", "两套完整环境，切流量", "秒级回滚、环境干净", "双倍资源、切换瞬间全量", "合规要求高、变更幅度大"],
        ["金丝雀", "小流量先跑新版本", "风险可控、能看真实指标", "需要流量切分与指标对比", "绝大多数日常迭代"],
    ]
    print_table(["方式", "做法", "优点", "代价", "适用"], rows)
    print()
    print("  Agent 场景的额外考量：新版本可能换了 Prompt/模型 —— 这不是「二进制切换」，")
    print("  而是行为切换，所以金丝雀更应该看**质量指标**（幻觉率、任务完成率），而不只是 5xx。")


def main() -> None:
    banner("V6｜部署与运维：健康检查 -> 金丝雀 -> 版本化 -> 优雅停机",
           [live_hint(False), "结构：探针 + 灰度权重 + SLO 判断 + 自动回滚 + checkpoint"])
    set_log(False)
    LEDGER.reset()
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    scenario_e()
    title_line("V6 结论")
    print("  上线不是「部署成功」，而是「你敢让 5% 的真实流量先替你试」。")
    print("  最后一站 V7：把「悄悄变差」这件事变成 CI 里一道会红的门禁。")


if __name__ == "__main__":
    main()