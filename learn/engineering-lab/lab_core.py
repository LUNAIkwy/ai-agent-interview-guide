# -*- coding: utf-8 -*-
"""
lab_core.py —— 工程化实验场的公共零件（对标文档《08-工程化实践》）

只放「所有实验都要用的零件」，每个零件都对应文档里的一节：

  ① 虚拟时钟 Clock               -> 让退避等待、熔断冷却不用真的等
  ② 记账 Ledger                  -> 文档 2.2.5「成本监控」：按租户/模型/步骤聚合
  ③ 结构化日志 + Tracer/Span      -> 文档 3.2「日志、Trace」
  ④ 模型注册表 + route()          -> 文档 1.2.1/1.2.2「多模型管理、优先级调度」
  ⑤ Provider 抽象 + FakeVendor    -> 文档 1.2.1「统一抽象」+ 可控故障注入
  ⑥ Gateway（重试/熔断/降级）      -> 文档 1.2.3 ~ 1.2.5
  ⑦ ExactCache / SemanticCache    -> 文档 2.2.3「缓存策略」
  ⑧ 输出工具（中文等宽表格等）      -> 让你一眼看清数据

三条约定：
  - 默认「离线假供应商」：不加 --live 绝不联网、绝不花钱。
  - 所有随机都用固定种子，保证每次跑出来的故障序列一模一样，方便和文档对照。
  - 虚拟时钟默认开启：30 秒的熔断冷却在这里是「瞬间」的，加 --real-time 才会真的等。

第三方依赖全部做成可选：
  openai / python-dotenv   只有 --live 调真实模型时才需要
  tiktoken / jsonschema    装了就用，没装自动降级（见 v4、v7）
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# 让 Windows 控制台也能正常打印中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:  # 可选依赖：没有也能跑，只是 --live 用不了
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ===========================================================================
# 0. .env 与命令行
# ===========================================================================
def _load_env_fallback(path: Path) -> None:
    """没装 python-dotenv 时的降级实现：能读 KEY=VALUE 就够用了。

    工程上「依赖缺席也要能起来」的兜底很常见，文档 2.2.1 的 tiktoken 降级是同一个思路。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_env() -> None:
    """读取同目录 .env。已存在的系统环境变量优先（CI 上注入密钥就是这个套路）。"""
    if load_dotenv is None:
        _load_env_fallback(ENV_PATH)
    else:
        load_dotenv(dotenv_path=ENV_PATH, override=False)


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(env(key) or default)
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(float(env(key) or default))
    except ValueError:
        return default


def has_flag(name: str) -> bool:
    return name in sys.argv


def arg_str(name: str, default: str = "") -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def arg_int(name: str, default: int) -> int:
    try:
        return int(arg_str(name, ""))
    except ValueError:
        return default


def arg_float(name: str, default: float) -> float:
    try:
        return float(arg_str(name, ""))
    except ValueError:
        return default


# ===========================================================================
# 1. 虚拟时钟：退避与冷却不真的等
# ===========================================================================
class Clock:
    """虚拟时钟。

    文档里动不动就是「冷却 30 秒」「退避 8 秒」，要真的等，这个 lab 得跑十分钟。
    所以这里把时间做成可推进的：
        sleep(30)    -> 直接把 offset 往前拨 30 秒（默认）
        --real-time  -> 走真正的 time.sleep，让你亲身体会退避有多慢
    """

    def __init__(self, real: bool = False) -> None:
        self.real = real
        self.offset = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return time.time() + self.offset

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.slept += seconds
        if self.real:
            time.sleep(seconds)
        else:
            self.offset += seconds

    def advance(self, seconds: float) -> None:
        self.offset += max(0.0, seconds)

    def set_real(self, real: bool) -> None:
        self.real = real

    def reset(self) -> None:
        self.offset = 0.0
        self.slept = 0.0


CLOCK = Clock()
CLOCK.set_real(has_flag("--real-time"))


def now() -> float:
    return CLOCK.now()


def sleep(seconds: float) -> None:
    CLOCK.sleep(seconds)


# ===========================================================================
# 2. 记账与成本账本（文档 2.2.5）
# ===========================================================================
def est_tokens(text: str) -> int:
    """粗略估算：中文约 1 字 1 token，其余约 4 字符 1 token。

    注意文档第 2 节的提醒：本地估算只用于预算与截断，最终要拿 API 的 usage 和账单对账。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = max(0, len(text) - cjk)
    return cjk + max(1, rest // 4)


def clip(text: Any, limit: int = 100) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def money(value: float) -> str:
    return f"{value:.4f} 元"


@dataclass
class Entry:
    """账本里的一行。多一个维度，就多一种「钱到底花在哪」的答案。"""

    tenant: str
    model: str
    step: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    cached: bool = False
    degraded: bool = False


class Ledger:
    """成本账本：按 租户 / 模型 / 步骤 / 供应商 聚合。

    文档 2.2.5 说「维度：租户 / 功能 / 模型 / Agent 步骤」，就是这里的几把刀。
    生产上这些数据异步入 Kafka 再进数仓，lab 里先用内存列表代替。
    """

    def __init__(self) -> None:
        self.entries: List[Entry] = []

    def add(self, **kwargs: Any) -> Entry:
        entry = Entry(**kwargs)
        self.entries.append(entry)
        return entry

    def reset(self) -> None:
        self.entries.clear()

    @property
    def calls(self) -> int:
        return len(self.entries)

    @property
    def total_tokens(self) -> int:
        return sum(e.prompt_tokens + e.completion_tokens for e in self.entries)

    @property
    def total_cost(self) -> float:
        return sum(e.cost for e in self.entries)

    @property
    def cached_calls(self) -> int:
        return sum(1 for e in self.entries if e.cached)

    @property
    def degraded_calls(self) -> int:
        return sum(1 for e in self.entries if e.degraded)

    def by(self, dim: str) -> Dict[str, Dict[str, float]]:
        buckets: Dict[str, Dict[str, float]] = {}
        for e in self.entries:
            key = str(getattr(e, dim))
            b = buckets.setdefault(key, {"calls": 0, "tokens": 0, "cost": 0.0})
            b["calls"] += 1
            b["tokens"] += e.prompt_tokens + e.completion_tokens
            b["cost"] += e.cost
        return buckets

    def table(self, dim: str = "model", title: str = "") -> str:
        rows = []
        for key, b in sorted(self.by(dim).items(), key=lambda kv: -kv[1]["cost"]):
            share = (b["cost"] / self.total_cost * 100) if self.total_cost else 0.0
            rows.append([key, f"{int(b['calls'])}", f"{int(b['tokens'])}",
                         money(b["cost"]), f"{share:.1f}%"])
        return table(["key", "调用", "tokens", "花费", "占比"], rows,
                     title=title or f"按 {dim} 聚合")

    def report(self) -> str:
        extra = f" | 命中缓存 {self.cached_calls} 次" if self.cached_calls else ""
        extra += f" | 降级 {self.degraded_calls} 次" if self.degraded_calls else ""
        return f"调用 {self.calls} 次 | {self.total_tokens} tokens | {money(self.total_cost)}{extra}"


LEDGER = Ledger()

# 预算封顶（文档 2.2.5）
BUDGET = env_float("LLM_BUDGET_YUAN", 0.0)


def budget_check() -> Optional[str]:
    """超预算就返回一句告警；生产上这里要真的拒绝请求（硬限制）。"""
    if BUDGET <= 0:
        return None
    if LEDGER.total_cost > BUDGET:
        return f"已花费 {money(LEDGER.total_cost)} > 预算 {money(BUDGET)}，应触发熔断/降级"
    return None
# ===========================================================================
# 3. 结构化日志 + Trace（文档 3.2）
# ===========================================================================
_trace_var: ContextVar[str] = ContextVar("trace_id", default="")
_span_var: ContextVar[str] = ContextVar("span_id", default="")
_LOG = {"on": True}


def set_log(on: bool) -> None:
    _LOG["on"] = on


def new_trace(trace_id: Optional[str] = None) -> str:
    """一次用户请求 = 一个 trace。没有它，多步链路的分支就只能靠猜。"""
    tid = trace_id or uuid.uuid4().hex[:12]
    _trace_var.set(tid)
    _span_var.set("")
    return tid


def trace_id() -> str:
    return _trace_var.get()


def span_id() -> str:
    return _span_var.get()


def log_event(level: str, msg: str, **fields: Any) -> Dict[str, Any]:
    """一行一条 JSON 的结构化日志。

    文档 3.2.1 的必备字段这里都齐了：ts / level / trace_id / span_id / msg + 业务字段。
    切忌只打一大段自然语言 —— 那样线上根本没法过滤、没法告警。
    """
    payload: Dict[str, Any] = {
        "ts": round(now(), 3),
        "level": level,
        "trace_id": trace_id() or "-",
        "span_id": span_id() or "-",
        "msg": msg,
    }
    payload.update(fields)
    if _LOG["on"]:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    return payload


@dataclass
class SpanRecord:
    id: str
    parent: str
    name: str
    start: float
    end: float = 0.0
    attrs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ms(self) -> float:
        return max(0.0, (self.end - self.start) * 1000)


class Span:
    """一段可计时的代码：`with tracer.span("llm.call", model=...) as sp: sp.note(...)`"""

    def __init__(self, tracer: "Tracer", name: str, attrs: Dict[str, Any]) -> None:
        self.tracer = tracer
        self.name = name
        self.attrs = attrs
        self.record: Optional[SpanRecord] = None

    def note(self, key: str, value: Any) -> None:
        if self.record is not None:
            self.record.attrs[key] = value

    def __enter__(self) -> "Span":
        self.record = self.tracer._open(self.name, self.attrs)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.tracer._close(self.record, exc)
        return False


class Tracer:
    """最小链路追踪器：记录 span 的父子关系，最后画成一棵树。

    文档 3.2.5 说「最小实现：contextvars 存 trace_id，with span(...) 记录开始结束」，
    这里就是那个最小实现（真实项目导出到 OpenTelemetry / Jaeger / LangFuse）。
    """

    def __init__(self, name: str = "trace", quiet: bool = True) -> None:
        self.name = name
        self.records: List[SpanRecord] = []
        self._stack: List[str] = []
        self.quiet = quiet

    def _open(self, name: str, attrs: Dict[str, Any]) -> SpanRecord:
        parent = self._stack[-1] if self._stack else ""
        rec = SpanRecord(id=uuid.uuid4().hex[:8], parent=parent, name=name, start=now())
        rec.attrs.update(attrs)
        self.records.append(rec)
        self._stack.append(rec.id)
        _span_var.set(rec.id)
        if not self.quiet:
            log_event("INFO", "span_start", span=name)
        return rec

    def _close(self, rec: Optional[SpanRecord], exc: Optional[BaseException]) -> None:
        if rec is None:
            return
        rec.end = now()
        if exc is not None:
            rec.error = f"{type(exc).__name__}: {exc}"
        if self._stack and self._stack[-1] == rec.id:
            self._stack.pop()
        _span_var.set(self._stack[-1] if self._stack else "")
        if not self.quiet:
            log_event("INFO", "span_end", span=rec.name, latency_ms=round(rec.ms, 1),
                      error=bool(exc))

    @contextmanager
    def span(self, name: str, **attrs: Any):
        sp = Span(self, name, attrs)
        sp.__enter__()
        try:
            yield sp
        except BaseException as exc:
            sp.__exit__(type(exc), exc, None)
            raise
        else:
            sp.__exit__(None, None, None)

    def log(self, name: str, ms: float = 0.0, **attrs: Any) -> None:
        """点事件（比如「写账本」「策略拒绝」）：带耗时，也参与瓶颈统计。"""
        start = now()
        rec = SpanRecord(id=uuid.uuid4().hex[:8],
                         parent=self._stack[-1] if self._stack else "",
                         name=name, start=start, end=start + ms / 1000)
        rec.attrs.update(attrs)
        self.records.append(rec)

    @property
    def total_ms(self) -> float:
        roots = [r for r in self.records if not r.parent]
        return max((r.ms for r in roots), default=0.0)

    def bottleneck(self) -> Optional[SpanRecord]:
        """自身耗时最大的 span —— 排障时第一个该看的东西（关键路径）。"""
        best: Optional[SpanRecord] = None
        best_self = -1.0
        for rec in self.records:
            child_ms = sum(c.ms for c in self.records if c.parent == rec.id)
            self_ms = rec.ms - child_ms
            if self_ms > best_self:
                best, best_self = rec, self_ms
        return best

    def dump(self, show_attrs: bool = True) -> None:
        print(f"  trace {self.name}  总耗时 {self.total_ms:.1f}ms")
        bottleneck = self.bottleneck()

        def render(rec: SpanRecord, last: bool, prefix: str) -> None:
            branch = "└─ " if last else "├─ "
            attrs = ""
            if show_attrs and rec.attrs:
                attrs = "  " + " ".join(f"{k}={clip(v, 32)}" for k, v in rec.attrs.items())
            mark = "   <== 瓶颈在这" if bottleneck is not None and rec.id == bottleneck.id else ""
            err = f"   [错误] {clip(rec.error, 60)}" if rec.error else ""
            print(f"  {prefix}{branch}{rec.name:<32} {rec.ms:>8.1f}ms{attrs}{err}{mark}")
            kids = [r for r in self.records if r.parent == rec.id]
            for i, kid in enumerate(kids):
                render(kid, i == len(kids) - 1, prefix + ("   " if last else "│  "))

        roots = [r for r in self.records if not r.parent]
        for i, root in enumerate(roots):
            render(root, i == len(roots) - 1, "  ")
# ===========================================================================
# 4. 模型注册表 + 路由（文档 1.2.1 / 1.2.2）
# ===========================================================================
@dataclass(frozen=True)
class ModelSpec:
    """配置中心里的一条记录：能力矩阵 + 价格表 + 限额 + 合规区域。"""

    model_id: str
    provider: str
    price_in: float  # 元 / 百万 token
    price_out: float
    rpm: int
    tpm: int
    quality: int  # 0~100，能力评分（演示用；生产上应由评估集跑出来）
    p50_ms: int
    context: int
    caps: frozenset
    region: str  # cn / us：合规路由要用

    @property
    def caps_text(self) -> str:
        return ",".join(sorted(self.caps)) or "-"


MODELS: Dict[str, ModelSpec] = {
    "cheap-mini": ModelSpec(
        "cheap-mini", "vendorA", 0.5, 1.5, 3000, 800_000, 62, 380, 32_000,
        frozenset({"json", "tools"}), "cn",
    ),
    "balanced-pro": ModelSpec(
        "balanced-pro", "vendorA", 4.0, 12.0, 1000, 200_000, 82, 900, 128_000,
        frozenset({"json", "tools", "vision"}), "cn",
    ),
    "strong-max": ModelSpec(
        "strong-max", "vendorB", 20.0, 60.0, 200, 40_000, 95, 2400, 400_000,
        frozenset({"json", "tools", "vision", "long"}), "us",
    ),
    "backup-cn": ModelSpec(
        "backup-cn", "vendorC", 2.0, 6.0, 600, 120_000, 74, 1100, 64_000,
        frozenset({"json", "tools"}), "cn",
    ),
    "local-oss": ModelSpec(
        "local-oss", "selfhosted", 0.0, 0.0, 100_000, 10_000_000, 55, 1500, 32_000,
        frozenset({"json"}), "cn",
    ),
}


def cost_of(spec: ModelSpec, prompt_tokens: int, completion_tokens: int) -> float:
    return prompt_tokens / 1e6 * spec.price_in + completion_tokens / 1e6 * spec.price_out


@dataclass
class Task:
    """一个待路由的任务画像。路由的本质：拿任务的需求去筛模型。"""

    kind: str = "final"  # classify / summarize / code / final
    needs: frozenset = field(default_factory=frozenset)
    min_quality: int = 0
    max_price_out: float = 1e9
    region: str = "any"  # "cn" = 数据不可出境
    tenant_tier: str = "normal"


POLICIES = ("cost_first", "latency_first", "quality_first", "load_aware")


def route(
    task: Task,
    policy: str = "cost_first",
    registry: Optional[Dict[str, ModelSpec]] = None,
    load: Optional[Dict[str, float]] = None,
) -> Tuple[Optional[ModelSpec], List[str]]:
    """按任务画像 + 策略挑模型，并把「谁被淘汰、为什么」一起返回。

    返回 (选中的 ModelSpec 或 None, 淘汰原因清单)。第二个返回值是给人看的：
    面试时能讲清「为什么这个请求没走大模型」，就说明你真的设计过路由。
    """
    registry = registry or MODELS
    load = load or {}
    eligible: List[ModelSpec] = []
    rejected: List[str] = []

    for spec in registry.values():
        missing = task.needs - spec.caps
        if missing:
            rejected.append(f"{spec.model_id}：能力不匹配，缺 {sorted(missing)}")
            continue
        if task.region != "any" and spec.region != task.region:
            rejected.append(f"{spec.model_id}：合规路由，数据不可出境（region={spec.region}）")
            continue
        if spec.quality < task.min_quality:
            rejected.append(f"{spec.model_id}：质量分 {spec.quality} < 要求 {task.min_quality}")
            continue
        if spec.price_out > task.max_price_out:
            rejected.append(f"{spec.model_id}：单价 {spec.price_out} 超预算 {task.max_price_out}")
            continue
        eligible.append(spec)

    if not eligible:
        return None, rejected

    if policy == "quality_first":
        key = lambda s: (-s.quality, s.price_out)  # noqa: E731
    elif policy == "latency_first":
        key = lambda s: (s.p50_ms, -s.quality)  # noqa: E731
    elif policy == "load_aware":
        # 负载感知：某家排队太深就先让别人上（文档 1.2.2）
        key = lambda s: (load.get(s.provider, 0.0) >= 0.8, load.get(s.provider, 0.0), s.p50_ms)  # noqa: E731
    else:  # cost_first：在满足约束的前提下选最便宜的
        key = lambda s: (s.price_out, -s.quality)  # noqa: E731

    eligible.sort(key=key)
    return eligible[0], rejected


# ===========================================================================
# 5. Provider 抽象 + 假供应商 + 真实供应商（文档 1.2.1）
# ===========================================================================
RETRYABLE_CODES = {"429", "500", "502", "503", "timeout", "connection"}


class ProviderError(RuntimeError):
    """上游模型的错误。带 code，是因为「能不能重试」完全取决于 code。"""

    def __init__(self, code: str, message: str, retry_after: float = 0.0,
                 retryable: Optional[bool] = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable


def is_retryable(exc: BaseException) -> bool:
    """文档 1.2.5：可重试（超时/429/部分 5xx）vs 不可重试（401/403/400）。"""
    if isinstance(exc, ProviderError):
        return exc.retryable
    return False


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str
    latency_ms: float
    cost: float = 0.0
    attempts: int = 1
    degraded: bool = False
    degraded_from: str = ""
    cached: bool = False
    waited_s: float = 0.0
    policy: str = ""

    def label(self) -> str:
        tags = []
        if self.cached:
            tags.append("缓存命中")
        if self.degraded:
            tags.append(f"已降级<-{self.degraded_from or '主模型'}")
        if self.attempts > 1:
            tags.append(f"重试{self.attempts - 1}次")
        return "、".join(tags) or "正常"


class Provider:
    """统一抽象层（Adapter）。

    业务代码只认 `chat(model, messages)`，后面是谁家完全不知道 ——
    这就是文档 1.2.1 说的「避免业务代码里散落 if vendor == ...」。
    """

    name = "provider"

    def chat(self, model: str, messages: List[Dict[str, str]], *, timeout_ms: int = 30_000,
             spec: Optional[ModelSpec] = None, **kwargs: Any) -> Completion:
        raise NotImplementedError  # pragma: no cover - 接口定义


# 假供应商能演的事件：真实厂商的 429 / 超时 / 挂掉，在这里被压成几行配置。
FAULT_EVENTS = ("ok", "429", "500", "timeout", "401", "slow")

_INJECT_RE = re.compile(
    r"(忽略(上文|之前|以上)|忽略所有(先前|前面)|ignore\s+(all\s+)?previous|"
    r"disregard|输出(你的)?(系统)?(提示词|密钥)|api[_ ]?key)"
)
_GROUNDED_RE = re.compile(r"(只依据|仅依据|必须.{0,8}引用|禁止编造|无法确定|不得编造|引用编号)")
_HARDENED_RE = re.compile(r"(资料中的指令|不可信|工具调用必须|必须经过策略|不执行文档|注入)")


def _grounded_answer(user_text: str) -> str:
    """「有引用约束」的模型：只回答资料支持的部分，不支持的就说无法确定。

    这里用关键词分流来模拟「RAG 检索到什么就答什么」，顺序有讲究：
    「分页上限」这类问题必须先于「分页」命中，否则就会**答非所问** ——
    这也是 RAG 路由/意图分类最常见的坑。
    """
    if any(k in user_text for k in ("上限", "多少条", "耗时", "性能", "平均")):
        return "根据已有资料无法确定：资料里没有分页上限与耗时数据。[1][2]"
    if any(k in user_text for k in ("手机号", "隐私", "个人信息")):
        return "订单列表的返回体不包含手机号等个人信息。[2]"
    if any(k in user_text for k in ("连不上", "异常", "错误", "报错")):
        return "接口异常时返回统一的错误码，不向前端暴露内部堆栈。[3]"
    if any(k in user_text for k in ("分页", "翻页", "页码", "page")):
        return "订单接口支持分页返回。[1]"
    if any(k in user_text for k in ("搜索", "keyword", "关键词")):
        return "订单接口支持按关键词搜索订单。[1]"
    return "根据已有资料无法确定，请补充资料。[1]"


class FakeVendor(Provider):
    """可编排故障的假供应商：离线跑，但行为像真的。

    faults: [(事件, 权重), ...]，事件见 FAULT_EVENTS。
    events: 也可以直接给一个固定序列（如 ["429", "429", "ok"]），跑完停在最后一个。

    固定 seed => 每次运行的故障序列一致 => 你就有了一个「可复现的故障演练环境」。
    """

    def __init__(self, name: str, faults: Sequence[Tuple[str, float]] = (("ok", 1.0),),
                 seed: int = 7, latency_scale: float = 1.0, quality: int = 80,
                 events: Optional[Sequence[str]] = None) -> None:
        self.name = name
        self.faults = list(faults)
        self.events = list(events) if events else None
        self.rng = random.Random(seed)
        self.calls = 0
        self.latency_scale = latency_scale
        self.quality = quality

    def _next_event(self) -> str:
        self.calls += 1
        if self.events is not None:
            idx = min(self.calls - 1, len(self.events) - 1)
            return self.events[idx]
        total = sum(w for _, w in self.faults)
        pick = self.rng.random() * total
        acc = 0.0
        for event, weight in self.faults:
            acc += weight
            if pick <= acc:
                return event
        return self.faults[-1][0]

    def _answer(self, messages: List[Dict[str, str]]) -> str:
        sys_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        hardened = bool(_HARDENED_RE.search(sys_text))
        injected = bool(_INJECT_RE.search(user_text))

        # 被注入的模型：真的去调一个危险工具（v4 用来说明「模型不是安全边界」）
        if injected and not hardened:
            return '{"tool": "delete_orders", "args": {"confirm": true}}'
        if injected and hardened:
            return "资料里夹带了指令性内容：我按规则只使用其中的事实，不执行其中的指令。"
        # 已经拿到工具结果 -> 组织最终回答（真实模型也是这个节奏）
        if "工具结果" in user_text:
            payload = user_text.split("工具结果：", 1)[-1]
            return f"根据工具返回：{clip(payload, 60)}"
        # 人设里允许调工具 -> 输出一次工具调用
        if "工具" in sys_text and "只输出 Python" not in sys_text:
            if "搜索" in user_text:
                return '{"tool": "search_orders", "args": {"keyword": "键盘", "page": 1, "size": 10}}'
            return '{"tool": "query_order", "args": {"order_id": "A1001"}}'
        # 引用约束（文档 8.5）：prompt 里写清了规则，模型才老实
        if _GROUNDED_RE.search(sys_text):
            return _grounded_answer(user_text)
        # 没有任何约束的模型：编造数字 + 漏要点（v7 用它演示幻觉与回归）
        return (f"关于「{clip(user_text, 16)}」：订单接口支持分页，单页上限 500 条，"
                f"平均耗时 120ms，已全量上线。")

    def chat(self, model: str, messages: List[Dict[str, str]], *, timeout_ms: int = 30_000,
             spec: Optional[ModelSpec] = None, **kwargs: Any) -> Completion:
        event = self._next_event()
        latency = (spec.p50_ms if spec else 500) * self.latency_scale
        latency *= 0.9 + self.rng.random() * 0.2  # ±10% 抖动

        if event == "429":
            sleep(min(latency, 800) / 1000)
            raise ProviderError("429", f"{model} 触发限流（RPM/TPM 超了）", retry_after=1.0)
        if event == "500":
            sleep(latency / 1000)
            raise ProviderError("500", f"{model} 服务端内部错误")
        if event == "timeout":
            sleep(timeout_ms / 1000)
            raise ProviderError("timeout", f"{model} 超过 {timeout_ms}ms 未返回")
        if event == "401":
            sleep(latency / 1000)
            raise ProviderError("401", "API Key 无效/过期（不可重试，重试只是浪费钱）",
                                retryable=False)

        scale = 1.6 if event == "slow" else 1.0
        sleep(latency * scale / 1000)
        text = self._answer(messages)
        return Completion(
            text=text,
            prompt_tokens=est_tokens("\n".join(m["content"] for m in messages)),
            completion_tokens=est_tokens(text),
            provider=self.name,
            model=model,
            latency_ms=latency * scale,
        )
class OpenAICompatProvider(Provider):
    """真实供应商：任何「OpenAI 兼容」接口都能用，只有 --live 才会被实例化。"""

    name = "live"

    def __init__(self, api_key: str, base_url: str, model: str, temperature: float = 0.3,
                 max_tokens: int = 1024, timeout: int = 60) -> None:
        if OpenAI is None:
            raise RuntimeError("没装 openai SDK：pip install openai，或者去掉 --live 走离线假供应商")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = base_url
        self._cli = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)

    def chat(self, model: str, messages: List[Dict[str, str]], *, timeout_ms: int = 30_000,
             spec: Optional[ModelSpec] = None, **kwargs: Any) -> Completion:
        start = now()
        target = model or self.model
        try:
            resp = self._cli.chat.completions.create(
                model=target, messages=messages,  # type: ignore[arg-type]
                temperature=self.temperature, max_tokens=self.max_tokens,
            )
        except Exception as exc:  # 把 SDK 异常翻译成统一错误码，上层才不用认识各家 SDK
            text = str(exc)
            low = text.lower()
            if "max_tokens" in text and "max_completion_tokens" in text:
                resp = self._cli.chat.completions.create(
                    model=target, messages=messages,  # type: ignore[arg-type]
                    temperature=self.temperature, max_completion_tokens=self.max_tokens,
                )
            elif "429" in text or "rate" in low:
                raise ProviderError("429", text[:200]) from exc
            elif "401" in text or "invalid_api_key" in low:
                raise ProviderError("401", text[:200], retryable=False) from exc
            elif "timeout" in low:
                raise ProviderError("timeout", text[:200]) from exc
            else:
                raise ProviderError("500", text[:200]) from exc

        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            # 推理模型把思考 token 写满预算时，content 会安静地变成空串
            raise ProviderError("500", f"模型返回空内容（finish_reason={choice.finish_reason}）")
        usage = getattr(resp, "usage", None)
        return Completion(
            text=content,
            prompt_tokens=getattr(usage, "prompt_tokens", 0)
            or est_tokens("\n".join(m["content"] for m in messages)),
            completion_tokens=getattr(usage, "completion_tokens", 0) or est_tokens(content),
            provider=self.name,
            model=target,
            latency_ms=(now() - start) * 1000,
        )


def live_available() -> bool:
    load_env()
    return OpenAI is not None and bool(env("LLM_API_KEY"))


def live_registry(registry: Optional[Dict[str, ModelSpec]] = None) -> Dict[str, ModelSpec]:
    """--live 时把真实模型也注册进配置中心。

    否则路由根本不会选到它（注册表里没有 = 不可达），你会以为 --live 没生效。
    价格从 .env 的 LLM_PRICE_IN / LLM_PRICE_OUT 读；这里故意给它「便宜又高分」，
    方便你直接看到真实的 token、真实的回答。
    """
    registry = dict(registry or MODELS)
    if not live_available():
        return registry
    model_id = env("LLM_MODEL", "gpt-4o-mini")
    registry[model_id] = ModelSpec(
        model_id, "live", env_float("LLM_PRICE_IN", 0.01), env_float("LLM_PRICE_OUT", 0.02),
        600, 200_000, 92, 2000, 128_000, frozenset({"json", "tools", "vision"}), "cn",
    )
    return registry


def build_vendors(live: bool = False) -> Dict[str, Provider]:
    """构造供应商池：默认四个「故障可控」的假供应商，--live 时再加一个真实的。"""
    vendors: Dict[str, Provider] = {
        # vendorA：主供应商，偶发 429 与超时（真实世界最常见的样子）
        "vendorA": FakeVendor("vendorA", faults=(("ok", 7.0), ("429", 1.5), ("timeout", 1.0),
                                                 ("slow", 0.5)), seed=7),
        # vendorB：强、贵、相对稳（也偶发 500）
        "vendorB": FakeVendor("vendorB", faults=(("ok", 9.0), ("500", 1.0)), seed=11),
        # vendorC：国产备用，降级链的后半段
        "vendorC": FakeVendor("vendorC", faults=(("ok", 9.5), ("429", 0.5)), seed=13),
        # 自托管：不爱挂，但慢、能力弱（便宜兜底）
        "selfhosted": FakeVendor("selfhosted", faults=(("ok", 10.0),), seed=17, latency_scale=1.4),
    }
    if live:
        if not live_available():
            print("  [提示] --live 需要 .env 里的 LLM_API_KEY，本次仍走离线假供应商。")
            return vendors
        vendors["live"] = OpenAICompatProvider(
            api_key=env("LLM_API_KEY"),
            base_url=env("LLM_BASE_URL", DEFAULT_BASE_URL),
            model=env("LLM_MODEL", "gpt-4o-mini"),
            max_tokens=env_int("LLM_MAX_TOKENS", 1024),
        )
    return vendors


# ===========================================================================
# 6. 重试退避 + 三态熔断 + 网关（文档 1.2.3 ~ 1.2.5）
# ===========================================================================
def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0,
                  jitter: bool = True, rng: Optional[random.Random] = None) -> float:
    """wait = min(cap, base * 2^attempt) + jitter（文档 1.2.5）。

    jitter 不是锦上添花：没有它，几百个客户端会同时重试，
    把刚缓过一口气的下游再打挂（重试风暴）。
    """
    rng = rng or random
    delay = min(cap, base * (2 ** attempt))
    if jitter:
        delay += rng.random() * base
    return delay


class BreakerState(str):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """三态熔断器（文档 1.2.3）。

      CLOSED    正常转发，统计连续失败
      OPEN      失败超阈值 -> 快速失败，进入冷却，不再打下游
      HALF_OPEN 冷却结束 -> 只放行少量探测请求，成功够了才回 CLOSED
    """

    def __init__(self, name: str, failure_threshold: int = 3, open_seconds: float = 15.0,
                 half_open_max_calls: int = 2, success_threshold: int = 2,
                 on_change: Optional[Callable[[str, str, str], None]] = None) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_max_calls = half_open_max_calls
        self.success_threshold = success_threshold
        self.on_change = on_change
        self.state = BreakerState.CLOSED
        self.failures = 0
        self.half_success = 0
        self.inflight = 0
        self.open_until = 0.0
        self.rejected = 0
        self.trips = 0

    def _switch(self, new: str, reason: str) -> None:
        if new == self.state:
            return
        old, self.state = self.state, new
        if self.on_change:
            self.on_change(self.name, old, reason)

    def _trip(self, reason: str) -> None:
        self.open_until = now() + self.open_seconds
        self.failures = 0
        self.half_success = 0
        self.inflight = 0
        self.trips += 1
        self._switch(BreakerState.OPEN, reason)

    def allow(self) -> bool:
        """能不能放行这次调用？OPEN 阶段直接快速失败（不碰下游）。"""
        if self.state == BreakerState.OPEN:
            if now() >= self.open_until:
                self.half_success = 0
                self.inflight = 0
                self._switch(BreakerState.HALF_OPEN, "冷却结束，放少量探测流量")
            else:
                self.rejected += 1
                return False
        if self.state == BreakerState.HALF_OPEN and self.inflight >= self.half_open_max_calls:
            self.rejected += 1
            return False
        return True

    def before_call(self) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self.inflight += 1

    def _release(self) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self.inflight = max(0, self.inflight - 1)

    def on_success(self) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self.half_success += 1
            self._release()
            if self.half_success >= self.success_threshold:
                self.half_success = 0
                self.failures = 0
                self._switch(BreakerState.CLOSED, f"半开阶段连续成功 {self.success_threshold} 次")
            return
        self.failures = 0

    def on_failure(self, exc: BaseException) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self._release()
            self._trip(f"半开探测失败：{exc}")
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._trip(f"连续失败 {self.failures} 次")

    def view(self) -> str:
        extra = ""
        if self.state == BreakerState.OPEN:
            extra = f" 冷却剩余 {max(0.0, self.open_until - now()):.1f}s"
        return f"{self.name}: {self.state}{extra}（拒绝 {self.rejected} 次 / 跳闸 {self.trips} 次）"


class TokenBucket:
    """令牌桶限流：文档 1.2.5 说 429 要「限流 + 退避」，这是限流那一半。"""

    def __init__(self, rate_per_s: float, burst: float = 1.0) -> None:
        self.rate_per_s = rate_per_s
        self.burst = burst
        self.tokens = burst
        self.updated = now()

    def _refill(self) -> None:
        self.tokens = min(self.burst, self.tokens + (now() - self.updated) * self.rate_per_s)
        self.updated = now()

    def try_acquire(self, cost: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def wait_time(self, cost: float = 1.0) -> float:
        self._refill()
        return 0.0 if self.tokens >= cost else (cost - self.tokens) / self.rate_per_s


STATIC_FALLBACK = "（兜底文案）模型暂时不可用，已记录您的请求。trace_id={trace}，请稍后重试。"


@contextmanager
def _nullspan():
    """没开 tracer 时的空实现，让 Gateway 里少一层 if。"""

    class _Empty:
        def note(self, *_: Any, **__: Any) -> None:
            pass

    yield _Empty()

class Gateway:
    """统一网关：路由 -> 熔断 -> 重试退避 -> 降级 -> 记账，这是文档第 1 节的完整闭环。

    业务代码永远只调 `gateway.chat(...)`：换模型、切供应商、降级对上层全透明。
    """

    def __init__(self, vendors: Dict[str, Provider], policy: str = "cost_first",
                 registry: Optional[Dict[str, ModelSpec]] = None, max_retries: int = 3,
                 base_delay: float = 0.3, cap_delay: float = 4.0,
                 breaker_kwargs: Optional[Dict[str, Any]] = None, record_ledger: bool = True,
                 tracer: Optional[Tracer] = None, pin_model: Optional[str] = None) -> None:
        self.vendors = vendors
        self.policy = policy
        self.registry = registry or MODELS
        # pin_model：把主模型锁死（--live 用它锁住真实模型）。
        # 不锁的话，「成本优先」会自动选到价格为 0 的自托管假供应商，
        # 你会以为在调真实模型，其实一次都没调出去。
        self.pin_model = pin_model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.cap_delay = cap_delay
        self.breaker_kwargs = breaker_kwargs or {}
        self.record_ledger = record_ledger
        self.tracer = tracer
        self.breakers: Dict[str, CircuitBreaker] = {}
        self.metrics: Counter = Counter()
        self.rng = random.Random(20260401)

    # --- 熔断粒度：按「供应商 + 模型」，粒度太粗会误伤健康模型 ----------------
    def breaker(self, key: str) -> CircuitBreaker:
        if key not in self.breakers:
            self.breakers[key] = CircuitBreaker(key, on_change=self._on_breaker_change,
                                                **self.breaker_kwargs)
        return self.breakers[key]

    def _on_breaker_change(self, name: str, old: str, reason: str) -> None:
        log_event("WARN", "breaker_state_change", breaker=name, frm=old,
                  to=self.breakers[name].state, reason=reason)

    def plan(self, task: Task, policy: Optional[str] = None) -> List[ModelSpec]:
        """降级链：主模型 -> 其他候选。

        关键设计：**第二跳优先换供应商**。
        如果降级只是「同一家的弱模型」，那这家整体挂掉时整条链一起挂 ——
        这正是 V2 场景 B 要暴露、也要在这一行代码里顺手修掉的东西。
        """
        policy = policy or self.policy
        primary, _ = route(task, policy, self.registry)
        if self.pin_model:
            pinned = [s for s in self.registry.values() if s.model_id == self.pin_model]
            if pinned:
                primary = pinned[0]
        rest = [
            s for s in self.registry.values()
            if s is not primary
            and not (task.needs - s.caps)
            and (task.region == "any" or s.region == task.region)
        ]
        # 先跨供应商（flag=False），同一家排后面；再按能力从强到弱
        rest.sort(key=lambda s: (s.provider == primary.provider if primary else False,
                                 -s.quality, s.price_out))
        return ([primary] if primary else []) + rest

    def chat(self, messages: List[Dict[str, str]], task: Optional[Task] = None,
             tenant: str = "demo", step: str = "llm", policy: Optional[str] = None,
             timeout_ms: int = 30_000, model: Optional[str] = None) -> Completion:
        task = task or Task()
        policy = policy or self.policy
        chain = self.plan(task, policy)
        if chain and model:  # 允许显式指定主模型（调试用）
            forced = [s for s in chain if s.model_id == model]
            chain = forced + [s for s in chain if s.model_id != model]
        primary = chain[0] if chain else None
        self.metrics["requests"] += 1

        ctx = (self.tracer.span("gateway.chat", model=primary.model_id if primary else "-",
                                policy=policy) if self.tracer else _nullspan())
        with ctx as sp:
            if primary is None:
                _, rejected = route(task, policy, self.registry)
                log_event("ERROR", "no_route", rejected=rejected)
                self.metrics["no_route"] += 1
                return self._static_fallback(None, policy)
            for idx, spec in enumerate(chain):
                result = self._try_model(sp, spec, primary, messages, tenant, step,
                                         policy, timeout_ms, degraded=idx > 0)
                if result is not None:
                    return result
            self.metrics["fallback"] += 1
            return self._static_fallback(primary, policy)

    def _try_model(self, sp: Span, spec: ModelSpec, primary: Optional[ModelSpec],
                   messages: List[Dict[str, str]], tenant: str, step: str, policy: str,
                   timeout_ms: int, degraded: bool) -> Optional[Completion]:
        """跑一个候选模型。返回 None = 这个候选彻底放弃，换降级链里的下一个。"""
        key = f"{spec.provider}:{spec.model_id}"
        breaker = self.breaker(key)
        waited = 0.0

        for attempt in range(1, self.max_retries + 1):
            if not breaker.allow():
                self.metrics["breaker_reject"] += 1
                log_event("WARN", "breaker_reject", breaker=key, state=breaker.state,
                          attempt=attempt, note="熔断期间不再盲试同一模型，直接换降级候选")
                return None
            breaker.before_call()
            vendor = self.vendors.get(spec.provider)
            try:
                if vendor is None:
                    raise ProviderError("500", f"供应商 {spec.provider} 未接入", retryable=False)
                started = now()
                result = vendor.chat(spec.model_id, messages, timeout_ms=timeout_ms, spec=spec)
                breaker.on_success()

                result.attempts = attempt
                result.waited_s = waited
                result.cost = cost_of(spec, result.prompt_tokens, result.completion_tokens)
                result.policy = policy
                result.degraded = degraded
                result.degraded_from = primary.model_id if (degraded and primary) else ""
                self.metrics["ok"] += 1
                self.metrics[f"ok:{spec.model_id}"] += 1
                if degraded:
                    self.metrics["degrade"] += 1
                if self.record_ledger:
                    LEDGER.add(tenant=tenant, model=spec.model_id, step=step,
                               provider=spec.provider, prompt_tokens=result.prompt_tokens,
                               completion_tokens=result.completion_tokens,
                               cost=result.cost, degraded=bool(degraded))
                if self.tracer:
                    self.tracer.log("llm.ok", ms=(now() - started) * 1000,
                                    model=spec.model_id, attempt=attempt,
                                    provider=spec.provider)
                return result
            except ProviderError as exc:
                breaker.on_failure(exc)
                self.metrics["fail"] += 1

                if not is_retryable(exc):
                    self.metrics["non_retryable"] += 1
                    log_event("ERROR", "call_failed", model=spec.model_id, code=exc.code,
                              retryable=False, note="401/400 这类错误重试只是浪费钱，立刻换策略")
                    return None
                if attempt >= self.max_retries:
                    log_event("ERROR", "call_failed", model=spec.model_id, code=exc.code,
                              attempts=attempt, note="重试次数用尽，进入降级链下一个候选")
                    return None

                delay = max(exc.retry_after,
                            backoff_delay(attempt - 1, self.base_delay, self.cap_delay,
                                          rng=self.rng))
                waited += delay
                self.metrics["retry"] += 1
                log_event("WARN", "retry_backoff", model=spec.model_id, code=exc.code,
                          attempt=attempt, wait_s=round(delay, 2), note="指数退避 + 抖动")
                sleep(delay)
        return None

    def _static_fallback(self, primary: Optional[ModelSpec], policy: str) -> Completion:
        """所有候选都挂了：给用户一句友好兜底，而不是把 500 甩到他脸上。"""
        log_event("ERROR", "static_fallback", note="全部候选不可用，返回静态兜底文案")
        return Completion(
            text=STATIC_FALLBACK.format(trace=trace_id() or "-"),
            prompt_tokens=0, completion_tokens=0, provider="static", model="static-fallback",
            latency_ms=0.0, degraded=True,
            degraded_from=primary.model_id if primary else "-", policy=policy,
        )

    def breaker_view(self) -> List[str]:
        return [b.view() for b in self.breakers.values()]


def build_gateway(policy: str = "cost_first", live: bool = False,
                  **kwargs: Any) -> Gateway:
    """一行拿到「配好路由表 + 供应商池」的网关。"""
    use_live = live or has_flag("--live")
    if use_live and live_available():
        # 真实模型当主候选；假供应商留在降级链尾部兜底（真挂了你会看到 degraded=True）
        kwargs.setdefault("pin_model", env("LLM_MODEL", "gpt-4o-mini"))
        return Gateway(build_vendors(True), policy=policy, registry=live_registry(), **kwargs)
    return Gateway(build_vendors(False), policy=policy, registry=MODELS, **kwargs)


# ===========================================================================
# 7. 缓存（文档 2.2.3 / 6.2.5）
# ===========================================================================
class ExactCache:
    """精确缓存：请求指纹完全一致才命中。

    键里必须带 租户 / 模型 / Prompt 版本 / 工具集版本 ——
    少任何一个，都会出现「A 租户看到 B 租户答案」的串味事故（文档 2.4 追问）。
    """

    def __init__(self, ttl_s: float = 300.0) -> None:
        self.ttl_s = ttl_s
        self.data: Dict[str, Tuple[str, float]] = {}
        self.hits = 0
        self.misses = 0

    def key(self, tenant: str, model: str, prompt_version: str, tools_version: str,
            messages: List[Dict[str, str]]) -> str:
        raw = json.dumps({"t": tenant, "m": model, "p": prompt_version, "x": tools_version,
                          "msg": messages}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def get(self, key: str) -> Optional[str]:
        item = self.data.get(key)
        if not item:
            self.misses += 1
            return None
        value, expire_at = item
        if now() >= expire_at:  # TTL 过期
            self.data.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: str) -> None:
        self.data[key] = (value, now() + self.ttl_s)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


def embed_stub(text: str, dims: int = 192) -> List[float]:
    """确定性「假 embedding」：字符 bigram 哈希到桶里再归一化。

    只为让语义缓存离线可跑、可复现；真实项目这里是 text-embedding-3-small / bge-m3。
    """
    vec = [0.0] * dims
    norm_text = re.sub(r"\s+", "", text.lower())
    grams = [norm_text[i:i + 2] for i in range(max(1, len(norm_text) - 1))] or [norm_text or "empty"]
    for gram in grams:
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class SemanticCache:
    """语义缓存：措辞不同但对齐了意图就复用（文档 2.2.3）。

    风险也在这里：阈值一松，「退款政策」和「退货政策」就会互相串味。
    所以它必须配合 租户隔离 / 意图分类 / 敏感场景禁用 / 更保守的阈值。
    """

    def __init__(self, threshold: float = 0.95, ttl_s: float = 600.0) -> None:
        self.threshold = threshold
        self.ttl_s = ttl_s
        self.items: List[Tuple[str, str, List[float], float, str]] = []
        self.hits = 0
        self.misses = 0
        self.last_similarity = 0.0
        self.last_match = ""

    def get(self, text: str, tenant: str = "demo") -> Optional[str]:
        vec = embed_stub(text)
        best: Optional[Tuple[str, str, List[float], float, str]] = None
        best_sim = -1.0
        for item in self.items:
            if item[4] != tenant:  # 租户隔离：绝不跨租户复用
                continue
            if now() >= item[3]:
                continue
            sim = cosine(vec, item[2])
            if sim > best_sim:
                best, best_sim = item, sim
        self.last_similarity = max(0.0, best_sim)
        self.last_match = best[0] if best else ""
        if best is not None and best_sim >= self.threshold:
            self.hits += 1
            return best[1]
        self.misses += 1
        return None

    def set(self, text: str, value: str, tenant: str = "demo") -> None:
        self.items.append((text, value, embed_stub(text), now() + self.ttl_s, tenant))

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


# ===========================================================================
# 8. 输出工具（中文按 2 个字符宽对齐）
# ===========================================================================
def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int, align: str = "left") -> str:
    space = max(0, size - width(text))
    return " " * space + text if align == "right" else text + " " * space


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], title: str = "") -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [width(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], width(cell))
    lines = []
    if title:
        lines.append(f"    {title}")
    lines.append("    " + "  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    lines.append("    " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in cells:
        lines.append("    " + "  ".join(pad(c, widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], title: str = "") -> None:
    print(table(headers, rows, title=title))


def title_line(text: str) -> None:
    print("=" * 78)
    print(text)
    print("=" * 78)


def banner(title: str, subtitle: Sequence[str] = ()) -> None:
    print("=" * 78)
    print(title)
    for line in subtitle:
        print(f"    {line}")
    print("=" * 78)
    clock = "真实时钟：退避与冷却都会真的等，跑得慢是正常的" if CLOCK.real \
        else "虚拟时钟：退避/冷却的秒数被压缩成瞬间"
    print(f"    {clock}")
    print()


def live_hint(live: bool, note: str = "") -> str:
    """横幅上那句话。note 用来纠正「这一版其实不接真实模型」的误期待。"""
    if (live or has_flag("--live")) and live_available():
        line = f"真实模型：{env('LLM_MODEL', 'gpt-4o-mini')} @ {env('LLM_BASE_URL', DEFAULT_BASE_URL)}"
    elif (live or has_flag("--live")):
        line = "要了 --live 但没读到 LLM_API_KEY，本次仍走离线假供应商"
    else:
        line = "离线假供应商（想调真实模型：加 --live，并在 .env 里配好 LLM_API_KEY）"
    return f"{line}；{note}" if note else line


def bullets(items: Iterable[str], indent: int = 4) -> None:
    for item in items:
        print(" " * indent + "- " + item)


def section(text: str) -> None:
    print()
    print("-" * 78)
    print(text)
    print("-" * 78)