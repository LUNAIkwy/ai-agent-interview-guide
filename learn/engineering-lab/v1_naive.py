# -*- coding: utf-8 -*-
"""
V1 | 裸奔版：一个没有容错、没有记账、没有 trace 的「网关」

对标文档《08-工程化实践》第 1 节。故意反着做一遍，你才知道那一节到底在治什么病：
    - 不做重试、不做熔断、不做降级
    - 不记账（花了多少钱不知道）
    - 不留 trace（哪一步慢、错在哪，靠猜）

运行：
    python v1_naive.py

提示：这里用的是「按剧本报错」的假供应商（固定事件序列），所以每次跑出来的
失败位置都一样，方便你和 V2 对照。真实世界当然不是剧本，而是随机发生的。
"""

from lab_core import (
    MODELS,
    FakeVendor,
    ProviderError,
    banner,
    bullets,
    live_hint,
    section,
    title_line,
)

# 10 个用户请求（租户不同，方便体会「按租户看成本」为什么重要）
REQUESTS = [
    ("T-1001", "订单 A1001 现在什么状态？"),
    ("T-1001", "帮我搜一下「键盘」相关的订单。"),
    ("T-1002", "发票怎么开？"),
    ("T-1002", "订单列表会返回手机号吗？"),
    ("T-1003", "退款要多久到账？"),
    ("T-1003", "帮我查订单 A1002。"),
    ("T-1001", "搜索支持分页吗？"),
    ("T-1002", "数据库连不上时接口怎么办？"),
    ("T-1003", "我上周买的键盘还没发货。"),
    ("T-1001", "这个接口有限流吗？"),
]

# 故意写死的故障序列：第 3 个请求遇 429、第 5 个超时、第 8 个 500、第 10 个 Key 失效
FAULTS = ["ok", "ok", "429", "ok", "timeout", "ok", "ok", "500", "ok", "401"]


def main() -> None:
    banner("V1｜裸奔版：没有重试 / 没有熔断 / 没有降级 / 没有记账 / 没有 trace",
           [live_hint(False, "本版是反例：故障按剧本发生，所以固定用假供应商"),
            "供应商：vendorA（按剧本报错：429 -> 超时 -> 500 -> 401）",
            "模型：cheap-mini（最便宜那个，因为「先跑起来再说」）"])

    vendor = FakeVendor("vendorA", events=FAULTS)
    spec = MODELS["cheap-mini"]

    section("1. 用户请求原样打给模型：出错就把异常甩出去")
    ok = fail = 0
    for tenant, question in REQUESTS:
        print(f"  [{tenant}] 用户问：{question}")
        try:
            result = vendor.chat(spec.model_id, [{"role": "user", "content": question}], spec=spec)
            ok += 1
            print(f"      -> 模型答：{result.text[:40]}…")
        except ProviderError as exc:
            fail += 1
            print(f"      -> 用户看到：500 Internal Server Error")
            print(f"         内部异常：{exc}")
            print(f"         用户动作：投诉 / 换一家试试")
    print()
    print(f"  结果：成功 {ok} 个，失败 {fail} 个，用户可见失败率 {fail / len(REQUESTS):.0%}")

    section("2. 这套代码现在回答不了的六个问题")
    bullets([
        "这次故障处理一共花了多少钱？—— 没有记账，答不出来。",
        "第 3 个请求到底是限流还是网络抖？—— 只有异常字符串，没有 error_code 结构。",
        "一次请求里，时间花在检索、模型还是工具上？—— 没有 trace，只能加 print 重跑。",
        "哪个租户最烧钱？—— 没有租户维度，答不出来。",
        "供应商挂了的时候，用户为什么必须跟着挂？—— 没有降级链，没有第二条路。",
        "把同一个请求重试一次会不会好？—— 没试过；就算想试，也没有「哪些错误能重试」的判断。",
    ])

    section("3. 所以第 1 节要装的东西（按顺序）")
    bullets([
        "Provider Adapter + 配置中心：模型能力/价格/限额/区域都进注册表（V2 场景 A）。",
        "路由策略：成本优先 / 延迟优先 / 质量优先，约束内最优化（V2 场景 A）。",
        "指数退避重试 + jitter：偶发抖动不该变成用户可见失败（V2 场景 B、E）。",
        "三态熔断：故障扩散时快速失败，别把下游打挂（V2 场景 C、D）。",
        "降级链：强模型 -> 弱模型 -> 兜底文案，用户最多「慢一点差一点」（V2 场景 B）。",
        "可观测性与记账：trace + 结构化日志 + 成本账本（V3）。",
    ])

    title_line("V1 结论：先跑起来很重要，但「能跑」和「能上线」之间隔着第 1 节的全部内容。")
    print("  V2 会把上面这些东西一件一件装进同一个网关，然后重新跑这 10 个请求。")


if __name__ == "__main__":
    main()