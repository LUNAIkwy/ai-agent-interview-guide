# -*- coding: utf-8 -*-
"""
guard_kit.py —— 安全与权限零件（对标文档第 4 节「安全与权限」）

文档的原话是「把模型不可信作为默认假设」。这里就是那句话的代码版：

  ① scan_input()    输入侧：Prompt 注入 / 越狱的规则检测（文档 4.2.1、4.2.2）
  ② mask_pii()      脱敏：日志、Trace、审计里不许出现完整手机号/邮箱/身份证（4.2.5）
  ③ TOOLS + PolicyEngine  工具白名单 + 参数 schema 校验 + 角色权限（4.2.4、Q7）
  ④ ApprovalQueue   两步授权：模型提议 -> 策略批准 -> 执行（Q19，人工在环）
  ⑤ AuditLog        审计日志：谁 / 何时 / 对什么资源 / 哪个 Agent / 什么工具（4.2.6）
  ⑥ filter_output() 输出侧：违规内容与 PII 过滤（4.2.3）

一句话记住：**模型不是安全边界，策略引擎才是。**
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lab_core import HERE, clip, now, table, trace_id

# 可选依赖：装了 jsonschema 就用它，没装走下面的 mini 校验器
try:
    from jsonschema import Draft202012Validator, FormatChecker

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False


def backend_name() -> str:
    return "jsonschema（第三方库）" if _HAS_JSONSCHEMA else "内置 mini 校验器（没装 jsonschema，自动降级）"


# ===========================================================================
# 1. 输入侧：注入与越狱检测（文档 4.2.1 / 4.2.2）
# ===========================================================================
@dataclass
class Finding:
    rule: str
    severity: str  # high / medium
    snippet: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule}：…{self.snippet}…"


# 规则检测永远不是全部（模型侧还有安全分类器），但它便宜、可解释、可审计。
# 生产上这一层负责「拦掉显眼的」，剩下的交给权限与输出校验兜住。
INJECTION_RULES: List[Tuple[str, str, str]] = [
    ("覆盖系统指令", "high", r"(忽略(上文|之前|以上|前面|所有)|忽略一切|ignore\s+(all\s+)?(previous|above)|disregard)"),
    ("索取密钥或系统提示", "high", r"(输出|打印|告诉我|展示).{0,8}(系统提示词|system prompt|api[_ ]?key|密钥|凭证)"),
    ("诱导越权写操作", "medium", r"(删除|清空|批量|drop|delete|truncate).{0,10}(订单|数据|库|表|orders|database)"),
    ("角色扮演绕过", "medium", r"(你现在是|从现在起你是|pretend you are|act as an? (admin|root))"),
    ("隐藏指令标记", "medium", r"(<\s*system\s*>|\[\s*INST\s*\]|###\s*instruction|BEGIN\s+SYSTEM)"),
]


def scan_input(text: str) -> List[Finding]:
    """扫一遍用户输入 / RAG 检索结果，返回命中的规则。

    注意：**检索回来的文档也要扫**。文档 4.4 的追问「RAG 文档里恶意内容怎么防」，
    第一件事就是「不要把不可信文档的内容当指令用」。
    """
    findings: List[Finding] = []
    for rule, severity, pattern in INJECTION_RULES:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            start = max(0, match.start() - 12)
            findings.append(Finding(rule, severity, clip(text[start:match.end() + 12], 60)))
            break  # 同一条规则命中一次就够，避免刷屏
    return findings


# ===========================================================================
# 2. 脱敏（文档 4.2.5）
# ===========================================================================
PII_PATTERNS: List[Tuple[str, str, str]] = [
    ("手机号", r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2"),
    ("邮箱", r"([\w.+-]{1,3})[\w.+-]*(@[\w.-]+\.[A-Za-z]{2,})", r"\1***\2"),
    ("身份证", r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)", r"\1********\2"),
    ("银行卡", r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)", r"\1********\2"),
]


def mask_pii(text: str) -> str:
    """日志与 Trace 只记脱敏后的文本：PII 一旦进了日志，就等于到处都有了。"""
    for _name, pattern, repl in PII_PATTERNS:
        text = re.sub(pattern, repl, text)
    return text


def mask_args(args: Any) -> Any:
    """工具参数也可能带 PII，写审计前先递归打码。"""
    if isinstance(args, dict):
        return {k: mask_args(v) for k, v in args.items()}
    if isinstance(args, list):
        return [mask_args(v) for v in args]
    if isinstance(args, str):
        return mask_pii(args)
    return args


# ===========================================================================
# 3. 工具白名单 + 参数校验 + 两步授权（文档 4.2.4 / Q7 / Q19）
# ===========================================================================
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: Dict[str, Any]
    roles: frozenset  # 哪些角色能用（白名单）
    resource: str  # 审计里记的资源
    write: bool = False  # 是否有副作用
    needs_approval_over: Optional[float] = None  # 超过阈值要人工审批
    rpm: int = 120


TOOLS: Dict[str, ToolSpec] = {
    "query_order": ToolSpec(
        "query_order", "按订单号查询订单（只读）",
        {
            "type": "object",
            "properties": {"order_id": {"type": "string", "pattern": "^[A-Za-z0-9-]{4,32}$"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        frozenset({"agent", "support", "analyst"}), "orders/read",
    ),
    "search_orders": ToolSpec(
        "search_orders", "按关键词搜索订单（只读）",
        {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "maxLength": 64, "minLength": 1},
                "page": {"type": "integer", "minimum": 1, "maximum": 1000},
                "size": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["keyword", "page", "size"],
            "additionalProperties": False,
        },
        frozenset({"agent", "support", "analyst"}), "orders/read",
    ),
    "send_email": ToolSpec(
        "send_email", "给客户发邮件（有副作用）",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "format": "email"},
                "subject": {"type": "string", "maxLength": 120},
                "body": {"type": "string", "maxLength": 4000},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
        frozenset({"agent", "support"}), "mail/send", write=True, rpm=30,
    ),
    "refund_order": ToolSpec(
        "refund_order", "给订单退款（有副作用，金额超阈值要人工审批）",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "pattern": "^[A-Za-z0-9-]{4,32}$"},
                "amount": {"type": "integer", "minimum": 1, "maximum": 100000},
                "reason": {"type": "string", "maxLength": 200},
            },
            "required": ["order_id", "amount", "reason"],
            "additionalProperties": False,
        },
        frozenset({"agent"}), "orders/refund", write=True, needs_approval_over=200.0, rpm=20,
    ),
    # 危险工具：**谁都不在授权列表里**，只能走人工后台。
    # 这就叫最小权限：不是「拦住模型」，而是「根本没给这条路」。
    "delete_orders": ToolSpec(
        "delete_orders", "批量删除订单（危险操作，只允许人工后台执行）",
        {
            "type": "object",
            "properties": {"confirm": {"type": "boolean"}},
            "required": ["confirm"],
            "additionalProperties": False,
        },
        frozenset(), "orders/delete", write=True, needs_approval_over=0.0, rpm=0,
    ),
}


def _mini_validate(schema: Dict[str, Any], value: Any, path: str = "$") -> List[str]:
    """内置的最小校验器：够用即可，只为让你看清 schema 校验到底在做什么。

    生产上别自己写这个：边界情况太多（$ref、oneOf、format 组合…），
    用 jsonschema / pydantic 这类库（文档 4.5 给的也是 jsonschema）。
    """
    errors: List[str] = []
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path} 应为 object，实际 {type(value).__name__}"]
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} 缺少必填字段")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key} 是未知字段（additionalProperties=false）")
        for key, sub in props.items():
            if key in value:
                errors += _mini_validate(sub, value[key], f"{path}.{key}")
        return errors

    if expected == "string":
        if not isinstance(value, str):
            return [f"{path} 应为 string"]
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} 长度 < {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} 长度 > {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path} 不匹配 pattern {schema['pattern']}")
        if schema.get("format") == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", value):
            errors.append(f"{path} 不是合法邮箱")
        return errors

    if expected in ("integer", "number"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{path} 应为 {expected}"]
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} > {schema['maximum']}")
        return errors

    if expected == "boolean" and not isinstance(value, bool):
        return [f"{path} 应为 boolean"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 不在枚举 {schema['enum']} 内")
    return errors


def validate_args(schema: Dict[str, Any], args: Any) -> List[str]:
    """返回错误清单，空列表 = 通过。"""
    if _HAS_JSONSCHEMA:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(args)]
    return _mini_validate(schema, args)
@dataclass
class Decision:
    allowed: bool
    code: str  # ok / unknown_tool / role_denied / schema_invalid / needs_approval / rate_limited
    reason: str
    needs_human: bool = False

    @property
    def denied(self) -> bool:
        return not self.allowed


class PolicyEngine:
    """策略引擎：模型提议的工具调用，必须在这里过一遍才能执行。

    顺序很重要，别搞反（文档 4.2.4）：
        ① 白名单：工具存不存在、这个角色能不能用
        ② 参数校验：schema 合不合法（防止被诱导出危险参数）
        ③ 业务约束：金额阈值 -> 两步授权；速率限制
    """

    def __init__(self, tools: Optional[Dict[str, ToolSpec]] = None, role: str = "agent") -> None:
        self.tools = tools or TOOLS
        self.role = role
        self._hits: Dict[str, List[float]] = {}

    def _rate_ok(self, name: str, rpm: int) -> bool:
        if rpm <= 0:
            return False
        window = [t for t in self._hits.get(name, []) if now() - t < 60]
        self._hits[name] = window
        if len(window) >= rpm:
            return False
        window.append(now())
        return True

    def authorize(self, tool_name: str, args: Dict[str, Any]) -> Decision:
        spec = self.tools.get(tool_name)
        if spec is None:
            return Decision(False, "unknown_tool", f"工具 {tool_name} 不在白名单里（未知工具一律拒绝）")
        if self.role not in spec.roles:
            return Decision(False, "role_denied",
                            f"角色 {self.role} 无权调用 {tool_name}（允许：{sorted(spec.roles) or '无'}）")

        errors = validate_args(spec.schema, args)
        if errors:
            return Decision(False, "schema_invalid", "参数校验失败：" + "；".join(errors[:3]))

        if not self._rate_ok(tool_name, spec.rpm):
            return Decision(False, "rate_limited", f"{tool_name} 触发速率限制（{spec.rpm} rpm）")

        threshold = spec.needs_approval_over
        if threshold is not None:
            amount = float(args.get("amount", 0) or 0)
            if amount > threshold or threshold == 0.0:
                return Decision(False, "needs_approval",
                                f"金额/风险超过阈值（{threshold}），需要人工审批",
                                needs_human=True)
        return Decision(True, "ok", "通过策略校验")


class ApprovalQueue:
    """两步授权（Q19）：模型只生成「意图 + 参数」，执行前由人/策略服务批准。"""

    def __init__(self) -> None:
        self.tickets: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def submit(self, tool: str, args: Dict[str, Any], reason: str, actor: str = "agent") -> str:
        self._seq += 1
        ticket = f"AP-{self._seq:03d}"
        self.tickets[ticket] = {"id": ticket, "tool": tool, "args": args, "reason": reason,
                                "actor": actor, "status": "PENDING", "trace_id": trace_id()}
        return ticket

    def approve(self, ticket: str, approver: str = "human") -> str:
        item = self.tickets[ticket]
        item["status"] = "APPROVED"
        item["approver"] = approver
        return f"{ticket} 已批准（审批人 {approver}）"

    def reject(self, ticket: str, approver: str = "human", why: str = "") -> str:
        item = self.tickets[ticket]
        item["status"] = "REJECTED"
        item["approver"] = approver
        item["why"] = why
        return f"{ticket} 被驳回（审批人 {approver}{'：' + why if why else ''}）"

    def status(self, ticket: str) -> str:
        return self.tickets[ticket]["status"]


# ===========================================================================
# 4. 审计日志（文档 4.2.6）
# ===========================================================================
class AuditLog:
    """谁在何时、对什么资源、通过哪个 Agent、执行了什么工具、结果如何。

    生产要求：append-only、独立存储、普通用户不可删改、保留期按合规要求。
    这里落盘到 logs/audit.jsonl（`.gitignore` 已忽略 *.log / logs/）。
    """

    def __init__(self, path: Optional[Path] = None, echo: bool = True) -> None:
        self.path = path or (HERE / "logs" / "audit.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self.count = 0

    def write(self, actor: str, agent: str, tool: str, args: Dict[str, Any], decision: Decision,
              resource: str = "-") -> Dict[str, Any]:
        record = {
            "ts": round(now(), 3),
            "who": actor,              # 谁
            "agent": agent,            # 哪个 Agent
            "tool": tool,              # 执行了什么工具
            "resource": resource,      # 对什么资源
            "args": mask_args(args),   # 参数：脱敏后落盘
            "decision": decision.code,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "trace_id": trace_id() or "-",
            "approval": "human" if decision.needs_human else "-",
        }
        self.count += 1
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.echo:
            print("    " + json.dumps(record, ensure_ascii=False))
        return record

    def tail(self, n: int = 5) -> List[str]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return lines[-n:]


# ===========================================================================
# 5. 输出侧过滤（文档 4.2.3）
# ===========================================================================
OUTPUT_RULES: List[Tuple[str, str]] = [
    ("内部凭据泄漏", r"(sk-[A-Za-z0-9]{8,}|api[_ ]?key\s*[:=]\s*\S+|Bearer\s+[A-Za-z0-9._-]{10,})"),
    ("越权承诺", r"(我可以(帮你)?(删除|清空|绕过|破解)|无需审批即可)"),
]


def filter_output(text: str) -> Tuple[str, List[Finding]]:
    """输出过滤：规则先行（PII + 违规），再交给模型分类器做二次判定。

    返回 (过滤后的文本, 命中清单)。生产上命中高危规则要**直接拦截并告警**，
    而不是像这里一样温柔地替换成 [已过滤]。
    """
    findings: List[Finding] = []
    for rule, pattern in OUTPUT_RULES:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            findings.append(Finding(rule, "high", clip(match.group(0), 40)))
            text = re.sub(pattern, "[已过滤]", text, flags=re.IGNORECASE)
    masked = mask_pii(text)
    if masked != text:
        findings.append(Finding("PII 泄漏", "high", "输出里出现了未脱敏的个人信息"))
    return masked, findings


def policy_table() -> str:
    rows = []
    for spec in TOOLS.values():
        rows.append([
            spec.name,
            "写" if spec.write else "读",
            ",".join(sorted(spec.roles)) or "（无人）",
            str(spec.rpm),
            "需要" if spec.needs_approval_over is not None else "-",
        ])
    return table(["工具", "类型", "允许角色", "rpm", "两步授权"], rows,
                 title=f"工具白名单与权限矩阵（校验后端：{backend_name()}）")