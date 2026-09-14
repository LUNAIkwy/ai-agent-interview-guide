# -*- coding: utf-8 -*-
"""
lab_core.py —— 实验场基础设施

依赖（见 requirements.txt）：
    openai          官方 SDK：负责 HTTP、重试、超时、流式
    python-dotenv   读取 .env 里的 Key

包含五样东西：
  1) .env 加载 + 真实 LLM 客户端（任何「OpenAI 兼容」接口都能用）
  2) mock 兜底：没配 Key 也能先把「结构」跑通
  3) Token / 费用记账
  4) 角色人设（system prompt）
  5) 生产三件套：Tracer（留痕）、Guard（四道刹车）、TaskState（状态机）

两点工程提醒：
  - API Key 只从 .env / 环境变量读，绝不写进代码、绝不提交到 Git。
  - 生产上还要给模型客户端加：超时、重试、限流、熔断（见文档第 8 章）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI

# 让 Windows 控制台也能正常打印中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ===========================================================================
# 1. .env 加载 + CLI 参数
# ===========================================================================
def load_env() -> None:
    """读取同目录下的 .env。已存在的系统环境变量优先，不被覆盖。"""
    load_dotenv(dotenv_path=ENV_PATH, override=False)


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def has_flag(name: str) -> bool:
    return name in sys.argv


def arg_int(name: str, default: int) -> int:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


# ===========================================================================
# 2. 记账
# ===========================================================================
@dataclass
class Usage:
    """Token 记账。多智能体最容易被忽视的成本就在这里。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def report(self) -> str:
        return (
            f"调用 {self.calls} 次 | 输入 {self.prompt_tokens} + 输出 "
            f"{self.completion_tokens} = {self.tokens} tokens"
        )


USAGE = Usage()


def snapshot() -> Tuple[int, int]:
    """给某个场景单独记账用。"""
    return USAGE.calls, USAGE.tokens


def delta(before: Tuple[int, int]) -> str:
    return f"{USAGE.calls - before[0]} 次调用 / {USAGE.tokens - before[1]} tokens"


def cost_report() -> str:
    """想算成钱，就在 .env 里填 LLM_PRICE_IN / LLM_PRICE_OUT（元 / 百万 token）。"""
    price_in = float(env("LLM_PRICE_IN", "0") or 0)
    price_out = float(env("LLM_PRICE_OUT", "0") or 0)
    if not price_in and not price_out:
        return USAGE.report()
    cost = USAGE.prompt_tokens / 1e6 * price_in + USAGE.completion_tokens / 1e6 * price_out
    return f"{USAGE.report()} | 估算花费 {cost:.4f} 元"


def est_tokens(text: str) -> int:
    """粗略估算：中文约 1 字 1 token，其余约 4 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = max(0, len(text) - cjk)
    return cjk + max(1, rest // 4)


def clip(text: str, limit: int = 100) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"

# ===========================================================================
# 3. 大模型客户端
# ===========================================================================
class LLMError(RuntimeError):
    """调用模型失败。"""


class LLMClient:
    """薄薄一层封装：有 Key 走官方 SDK，没 Key 退回 mock。"""

    def __init__(
        self,
        api_key: str = "",
        base_url: Optional[str] = None,
        model: str = "gpt-4o-mini",
        mock: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.mock = mock
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = base_url or DEFAULT_BASE_URL
        self._cli: Optional[OpenAI] = None
        if not mock:
            # max_retries / timeout 交给 SDK —— 这是生产上的基本盘
            self._cli = OpenAI(
                api_key=api_key,
                base_url=base_url or None,
                timeout=timeout,
                max_retries=2,
            )

    @property
    def label(self) -> str:
        return "mock(离线兜底)" if self.mock else self.model

    def chat(self, system: str, user: str, tag: str = "agent", verbose: bool = True) -> str:
        if self.mock:
            text, pt, ct = mock_reply(tag, system, user)
        else:
            text, pt, ct = self._live_chat(system, user)
        USAGE.add(pt, ct)
        if verbose:
            print(f"  -- [{tag}] {self.label}")
            print(f"     入参: {clip(user, 140)}")
            lines = text.strip().splitlines() or [""]
            for i, line in enumerate(lines):
                print(f"     出参: {line}" if i == 0 else f"           {line}")
            print(f"     用量: {pt} + {ct} tokens")
        return text

    def _live_chat(self, system: str, user: str) -> Tuple[str, int, int]:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            resp = self._cli.chat.completions.create(**kwargs)  # type: ignore[union-attr]
        except Exception as exc:
            msg = str(exc)
            # 有些新模型只认 max_completion_tokens，这里做一次兼容回退
            if "max_tokens" in msg and "max_completion_tokens" in msg:
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = self.max_tokens
                resp = self._cli.chat.completions.create(**kwargs)  # type: ignore[union-attr]
            else:
                raise LLMError(f"调用模型失败：{msg[:400]}") from exc

        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        pt = getattr(usage, "prompt_tokens", None) or est_tokens(system + user)
        ct = getattr(usage, "completion_tokens", None) or est_tokens(text)
        return text, int(pt), int(ct)


def build_client(role: str = "default", mock: bool = False) -> LLMClient:
    """
    按角色选模型 -- 对应文档第 9 节的「小模型干子任务 + 大模型仲裁」。
      决策类角色（boss / reviewer / critic）-> LLM_MODEL_SMART
      执行类角色（analyst / architect / coder）-> LLM_MODEL_FAST
    """
    load_env()
    api_key = env("LLM_API_KEY")
    base_url = env("LLM_BASE_URL") or None
    default_model = env("LLM_MODEL", "gpt-4o-mini")
    fast = env("LLM_MODEL_FAST")
    smart = env("LLM_MODEL_SMART")

    decision_roles = {"boss", "reviewer", "critic", "critic_chatty", "default"}
    model = (smart or default_model) if role in decision_roles else (fast or default_model)

    if mock or has_flag("--mock") or not api_key:
        return LLMClient(model=model, mock=True)
    return LLMClient(api_key=api_key, base_url=base_url, model=model)


def banner(title: str, client: LLMClient) -> None:
    print("=" * 70)
    print(title)
    if client.mock:
        print("模型：离线 mock（没读到 LLM_API_KEY，或显式加了 --mock）")
        print("      想看真实模型：复制 .env.example 为 .env 并填入 Key")
    else:
        print(f"模型：真实 API -> {client.model}  @ {client.base_url}")
    print("=" * 70)
# ===========================================================================
# 4. 角色人设（system prompt）
# ===========================================================================
PERSONAS: Dict[str, str] = {
    "boss": (
        "你是项目经理。把目标拆成有序子任务，只输出 JSON，不要任何解释文字。"
        '格式：{"subtasks":[{"agent":"analyst|architect|coder|reviewer","task":"一句话任务"}]}'
    ),
    "analyst": "你是需求分析师。只输出需求要点列表（不超过 5 条），不写代码、不寒暄。",
    "architect": "你是架构师。由于有token的限制，你只需要输出最简单的模块划分与接口签名，不写完整实现、不寒暄。（token消耗不要超出1000）",
    "coder_fast": "你是工程师。只输出 Python 代码，不要解释文字。由于token限制，你只需要实现最简单的demo。",
    "coder": (
        "你是工程师。只输出 Python 代码，不要解释文字。"
        "必须处理边界情况（keyword 为空、page<=0），并在代码注释里用【边界处理】标明。"
    ),
    "reviewer": (
        "你是严格的代码审查员。你只认代码里能看到的证据，不认『已修复』这类口头声明。"
        "检查实现是否真的包含边界处理（空关键词、非法页码）。"
        "先写 1-2 句理由，最后一行必须且只能是 'VERDICT: PASS' 或 'VERDICT: FAIL'。"
    ),
    "critic": (
        "你是对抗性审查员（红队）。你的职责是不断找出仍存在的问题，"
        "每一轮都必须再提出一条批评意见，永远不要给出 PASS。"
        "先写理由，最后一行必须是 'VERDICT: FAIL'。"
    ),
    "critic_chatty": (
        "你是对抗性审查员（红队）。每轮都要用不同的措辞、从不同角度提出新的批评，"
        "永远不要给出 PASS。先写理由，最后一行必须是 'VERDICT: FAIL'。"
    ),
}


def extract_verdict(text: str) -> str:
    """
    从自然语言里抠出结构化结论。
    生产上应当直接用 JSON mode / tool calling -- 别把自然语言当 API 参数。
    """
    upper = text.upper()
    if "VERDICT: PASS" in upper:
        return "PASS"
    if "VERDICT: FAIL" in upper:
        return "FAIL"
    return "UNKNOWN"


# ===========================================================================
# 5. 生产三件套
# ===========================================================================
class Tracer:
    """全链路留痕：崩了能复盘。对应文档第 9 节的「调试与可观测」。"""

    def __init__(self, trace_id: Optional[str] = None) -> None:
        self.trace_id = trace_id or f"trace-{int(time.time() * 1000) % 100000:05d}"
        self.events: List[Dict[str, Any]] = []

    def log(self, step: str, role: str, detail: str = "") -> None:
        self.events.append(
            {
                "ts": time.strftime("%H:%M:%S"),
                "step": step,
                "role": role,
                "detail": clip(detail, 90),
            }
        )

    def dump(self) -> None:
        print("\n  -- trace 日志 --")
        print(f"  trace_id = {self.trace_id}")
        for e in self.events:
            print(f"  {e['ts']}  {e['step']:<9} {e['role']:<12} {e['detail']}")


class Halt(RuntimeError):
    """刹车被触发。"""


class Guard:
    """四道刹车 -- 对应文档第 9 节 Q14「如何检测多 Agent 系统的死循环」。"""

    RULES = [
        "① 全局步数/耗时上限",
        "② 状态哈希去重",
        "③ 无进展检测",
        "④ 预算熔断",
    ]

    def __init__(
        self,
        max_steps: int = 8,
        patience: int = 3,
        token_budget: Optional[int] = None,
        max_seconds: int = 180,
    ) -> None:
        self.max_steps = max_steps
        self.patience = patience
        self.token_budget = (
            token_budget
            if token_budget is not None
            else int(os.environ.get("LLM_TOKEN_BUDGET", "60000") or 60000)
        )
        self.max_seconds = max_seconds
        self.started = time.time()
        self._seen = set()
        self._last_progress = None
        self._stale = 0

    @staticmethod
    def _hash(action: str) -> str:
        return re.sub(r"\s+", " ", action.strip().lower())

    def check(self, step: int, action: str, progress_key: str) -> None:
        elapsed = time.time() - self.started
        if step > self.max_steps:
            raise Halt(f"{self.RULES[0]} 触发：已执行 {step} 步 > 上限 {self.max_steps}")
        if elapsed > self.max_seconds:
            raise Halt(f"{self.RULES[0]} 触发：耗时 {elapsed:.1f}s > 上限 {self.max_seconds}s")

        h = self._hash(action)
        if h in self._seen:
            raise Halt(f"{self.RULES[1]} 触发：第 {step} 步与之前某一步动作完全一致 -> {clip(action, 60)}")
        self._seen.add(h)

        if progress_key == self._last_progress:
            self._stale += 1
            if self._stale >= self.patience:
                raise Halt(
                    f"{self.RULES[2]} 触发：关键指标连续 {self._stale} 轮无变化（progress={progress_key}）"
                )
        else:
            self._last_progress = progress_key
            self._stale = 0

        if USAGE.tokens > self.token_budget:
            raise Halt(f"{self.RULES[3]} 触发：已烧 {USAGE.tokens} tokens > 预算 {self.token_budget}")

PHASES = ("PLAN", "EXEC", "VERIFY", "DONE")
ALLOWED: Dict[str, set] = {
    "PLAN": {"EXEC"},
    "EXEC": {"VERIFY"},
    "VERIFY": {"DONE", "EXEC"},  # 质检不通过可以打回重做
    "DONE": set(),
}


class TaskState:
    """状态机：非法迁移直接拒绝。对应文档第 6 节「状态管理与同步」。"""

    def __init__(self, verbose: bool = True) -> None:
        self.phase = "PLAN"
        self.verbose = verbose

    def move(self, nxt: str) -> None:
        if nxt not in ALLOWED[self.phase]:
            raise ValueError(
                f"非法状态迁移 {self.phase} -> {nxt}"
                f"（当前允许：{sorted(ALLOWED[self.phase]) or '无，已是终态'}）"
            )
        if self.verbose:
            print(f"     [状态机] {self.phase} -> {nxt}")
        self.phase = nxt


# ===========================================================================
# 6. mock 兜底（规则驱动的假模型，行为写死，只为把结构跑通）
# ===========================================================================
def mock_reply(tag: str, system: str, user: str) -> Tuple[str, int, int]:
    if tag == "boss":
        text = json.dumps(
            {
                "subtasks": [
                    {"agent": "analyst", "task": "梳理需求要点"},
                    {"agent": "architect", "task": "给出接口草案"},
                    {"agent": "coder", "task": "实现 search_orders"},
                    {"agent": "reviewer", "task": "审查实现风险"},
                ]
            },
            ensure_ascii=False,
        )
    elif tag == "analyst":
        text = "1) 按关键词搜索订单；2) 支持分页；3) 返回结果不得包含手机号。"
    elif tag == "architect":
        text = (
            "service.search_orders(keyword, page, size) -> repo 分页查询 -> "
            "dto.OrderBrief(id, amount, status)，不含手机号。"
        )
    elif tag in ("coder", "author", "coder-rework"):
        # 只有人设/指令里明确要求了「边界处理」才真的做
        if "边界" in system or "边界" in user or "证据" in user:
            text = (
                "def search_orders(keyword, page, size):\n"
                "    # 【边界处理】keyword 为空返回空列表；page<=0 归一为 1\n"
                "    if not keyword:\n"
                "        return []\n"
                "    page = max(1, page)\n"
                "    return repo.query(keyword, page, size)"
            )
        else:
            text = "def search_orders(keyword, page, size):\n    return repo.query(keyword, page, size)"
    elif tag == "reviewer":
        if "边界" in user:
            text = "代码中能看到【边界处理】注释与对应实现。\nVERDICT: PASS"
        else:
            text = "keyword 为空未兜底，page<=0 未处理，没有任何边界证据。\nVERDICT: FAIL"
    elif tag == "critic":
        text = "仍有问题：错误处理不完整、缺少日志、未考虑并发与限流。\nVERDICT: FAIL"
    elif tag == "critic_chatty":
        # 每轮措辞都不同 -> 故意让「状态哈希去重」失效
        variants = [
            "错误分支仍未覆盖，建议补日志。",
            "换个角度看：异常路径没有回滚，并发下会脏读。",
            "再补充一点：缺少限流与超时设置。",
            "继续追问：分页上限没有约束，可能被拖库。",
        ]
        text = f"第 {USAGE.calls} 次复审：{variants[USAGE.calls % len(variants)]}\nVERDICT: FAIL"
    else:
        text = "（mock）已收到。"

    return text, est_tokens(system + user), est_tokens(text)
