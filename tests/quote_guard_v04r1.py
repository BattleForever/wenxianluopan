# -*- coding: utf-8 -*-
"""quote_guard_v04r1 —— V4R1-07 严格引用校验唯一入口。

背景（本轮必须修正的误判）：
    parallel_v04 交付的 closure_v04.py 第 243 行附近写的是

        okq = all(((b.get("elements", {}) or {}).get(k, {}) or {}).get("quote", "") in src
                  for k in ("question", "method", "results", "contribution"))

    其中 quote 默认空串 ""，而 "" in src 对任何非空 src 恒为 True：
    四要素引用全空（如真实库里 8 条"摘要未提供"占位笔记对应的 brief）
    也会得到 okq=True，闭环因此假绿。Codex 验收（codex_acceptance_02
    验收结论.md 第 3 节 V4R1-07）已确认该误判。

本模块是本轮所有演示 / 闭环 / 抽查脚本复用的**唯一**严格校验入口；
不允许任何脚本另写宽松版本。

规则（缺一即失败）：
  G1 必要引用非空：四要素（question/method/results/contribution）凡声称
     "通过"的必须带非空 quote。摘要中确实没有该要素时 zh/en 填
     "摘要未提供"、quote 为空串是**合法降级**（not_reported，诚实负例），
     但该要素不得计入通过项，更不得算"实际展示了有依据的 AI 结论"。
  G2 quote_verified 与实际检查一致：claimed quote_verified=True 必须
     实际复核通过；quote 非空却 quote_verified=False 同样拒绝（前后矛盾）。
  G3 生成时原快照存在：brief 必须携带 source_snapshot_raw（或旧字段
     source_text 同值）非空；调用方给 expected_snapshot 时两者必须一致
     （错误快照必须拒绝）。
  G4 Unicode 码点切片逐字匹配：quote 必须在快照中逐字（区分大小写、
     含原始空白）出现，且给出的 quote_start/quote_end 必须是 Python
     码点索引且满足 source[start:end] == quote。UTF-16（JS 风格）索引、
     归一化后"看起来一样"的片段一律拒绝。

结果分三类，调用方必须区分：
  - verified      实际复核通过（计入通过项）
  - not_reported  合法降级（quote 空且 zh/en 为"摘要未提供"类占位）
  - violation     校验失败（任何 G1–G4 分支），reason 给出具体分支

空串 / 缺 elements / 坏片段 / 错位置 / 错误快照必须拒绝，逐一由
tests/test_v04r1_quote_guard.py 的反例证明。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

REQUIRED_ELEMENTS = ("question", "method", "results", "contribution")

# 合法降级（诚实负例）占位文案——与 app/ai_brief.py 提示词约定一致
NOT_REPORTED_MARKERS = ("摘要未提供", "abstract not provided")

VERIFIED = "verified"
NOT_REPORTED = "not_reported"
VIOLATION = "violation"


@dataclass
class GuardResult:
    ok: bool = True                      # 无任何 violation（允许含 not_reported）
    strict_pass: bool = False            # ok 且全部必要要素 verified（可称"AI 正例"）
    elements: dict = field(default_factory=dict)   # key -> {status, reason?, ...}
    violations: list = field(default_factory=list) # [{key?, branch, reason}]
    not_reported: list = field(default_factory=list)
    snapshot_basis: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "strict_pass": self.strict_pass,
            "snapshot_basis": self.snapshot_basis,
            "elements": self.elements,
            "violations": self.violations,
            "not_reported": self.not_reported,
        }

    def __str__(self) -> str:  # 便于日志
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _snapshot_of(brief: Mapping[str, Any]) -> Optional[str]:
    """G3：取生成时原快照。source_snapshot_raw 为准；source_text 为兼容同值。"""
    raw = brief.get("source_snapshot_raw")
    if isinstance(raw, str) and raw:
        return raw
    legacy = brief.get("source_text")
    if isinstance(legacy, str) and legacy:
        return legacy
    return None


def _is_not_reported_text(text: Any) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.strip().lower()
    return any(m in low or m in text for m in NOT_REPORTED_MARKERS)


def _snapshot_matches_expected(snapshot: str, expected: Any) -> bool:
    """快照一致性：期望值可以是完整文本或 sha256（utf-8）十六进制。"""
    if expected is None:
        return True
    if isinstance(expected, str):
        if len(expected) == 64 and _is_hex(expected):
            return hashlib.sha256(snapshot.encode("utf-8")).hexdigest() == expected.lower()
        return snapshot == expected
    return False


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def check_brief(brief: Any,
                *,
                required: Iterable[str] = REQUIRED_ELEMENTS,
                expected_snapshot: Any = None,
                require_all_verified_for_strict: bool = True) -> GuardResult:
    """唯一入口：对一份 brief（POST /api/ai/briefs 响应中的 brief 对象，
    或 GET /briefs/{id} 的 brief 字段）做严格引用校验。

    expected_snapshot：调用方掌握的生成时快照（完整文本或其 sha256 hex）。
      给出且不一致 ⇒ G3 违规（错误快照必须拒绝）。
    """
    res = GuardResult()
    required = tuple(required)

    def _violate(key: Optional[str], branch: str, reason: str):
        res.ok = False
        res.violations.append({"key": key, "branch": branch, "reason": reason})
        if key:
            res.elements[key] = {"status": VIOLATION, "reason": reason}

    # ---- 顶层形状 ----
    if not isinstance(brief, Mapping):
        _violate(None, "not_a_mapping", "brief 不是对象（Mapping）")
        res.strict_pass = False
        return res

    elements = brief.get("elements")
    if not isinstance(elements, Mapping) or not elements:
        _violate(None, "missing_elements",
                 "brief.elements 缺失或为空（R4：四要素结构必须存在）")
        res.strict_pass = False
        return res

    # ---- G3 快照存在与一致 ----
    snapshot = _snapshot_of(brief)
    if snapshot is None:
        _violate(None, "missing_snapshot",
                 "生成时原快照缺失（source_snapshot_raw / source_text 均为空）")
        res.strict_pass = False
        return res
    if expected_snapshot is not None and not _snapshot_matches_expected(snapshot, expected_snapshot):
        _violate(None, "wrong_snapshot",
                 "brief 携带快照与调用方掌握的生成时快照不一致（或哈希不符）")
        res.strict_pass = False
        return res
    res.snapshot_basis = brief.get("quote_basis") if isinstance(
        brief.get("quote_basis"), str) else "unspecified"
    if res.snapshot_basis != "source_snapshot_raw":
        _violate(None, "unknown_quote_basis",
                 f"quote_basis={res.snapshot_basis!r} 不是 source_snapshot_raw，"
                 "引用依据不明，拒绝")

    # ---- 逐要素 ----
    all_verified = True
    for key in required:
        entry = elements.get(key)
        if not isinstance(entry, Mapping):
            _violate(key, "missing_element", f"必要要素 {key} 缺失或不是对象")
            all_verified = False
            continue

        quote = entry.get("quote")
        quote = quote if isinstance(quote, str) else ""
        claimed_verified = bool(entry.get("quote_verified"))
        zh = entry.get("zh")
        en = entry.get("en")

        if quote == "":
            # 空引用：只有"合法降级"占位才允许，且绝不计入通过项。
            if _is_not_reported_text(zh) or _is_not_reported_text(en):
                if claimed_verified:
                    _violate(key, "flag_mismatch",
                             f"{key}: quote 为空却声称 quote_verified=True")
                else:
                    res.elements[key] = {"status": NOT_REPORTED,
                                         "reason": "摘要未提供该要素（合法降级，不计入通过项）"}
                    res.not_reported.append(key)
                    all_verified = False
            else:
                _violate(key, "empty_quote",
                         f"{key}: quote 为空且 zh/en 不是『摘要未提供』占位——"
                         "无证据支撑的要素（closure_v04.py:243 空引用误判分支）")
                all_verified = False
            continue

        if not isinstance(quote, str) or not quote.strip():
            _violate(key, "bad_quote_type", f"{key}: quote 不是非空字符串")
            all_verified = False
            continue

        # ---- G4 逐字匹配（码点级） ----
        pos = snapshot.find(quote)  # 区分大小写、含原始空白的精确匹配
        if pos < 0:
            _violate(key, "fragment_not_found",
                     f"{key}: quote 不在生成时快照中逐字出现（坏片段）")
            all_verified = False
            continue

        start = entry.get("quote_start")
        end = entry.get("quote_end")
        if start is None or end is None:
            _violate(key, "missing_position",
                     f"{key}: 已验证引用必须携带 quote_start/quote_end（码点索引）")
            all_verified = False
            continue
        if not (isinstance(start, int) and isinstance(end, int)
                and not isinstance(start, bool) and not isinstance(end, bool)):
            _violate(key, "bad_position_type",
                     f"{key}: quote_start/quote_end 必须是整数码点索引")
            all_verified = False
            continue
        if not (0 <= start < end <= len(snapshot)):
            _violate(key, "bad_position_range",
                     f"{key}: quote_start/quote_end 超出快照范围（start={start}, end={end}, "
                     f"len={len(snapshot)}）")
            all_verified = False
            continue
        if start != pos:
            _violate(key, "wrong_position",
                     f"{key}: quote_start={start} 与实际首次出现位置 {pos} 不符（错位置）")
            all_verified = False
            continue
        if snapshot[start:end] != quote:
            _violate(key, "slice_mismatch",
                     f"{key}: 快照码点切片 snapshot[start:end] 与 quote 不逐字相等")
            all_verified = False
            continue

        # ---- G2 标志一致 ----
        if not claimed_verified:
            _violate(key, "flag_mismatch",
                     f"{key}: 引用实际复核通过但 quote_verified=False（前后矛盾）")
            all_verified = False
            continue

        res.elements[key] = {"status": VERIFIED, "quote_start": start,
                             "quote_end": end, "quote_len": len(quote)}

    if res.ok:
        res.strict_pass = (not require_all_verified_for_strict) or all_verified
    else:
        res.strict_pass = False
    return res


def require_strict_pass(brief: Any, **kw) -> GuardResult:
    """便捷封装：strict_pass=False 时抛 AssertionError（演示脚本用）。"""
    res = check_brief(brief, **kw)
    if not res.strict_pass:
        raise AssertionError("quote_guard_v04r1 严格校验未通过：" + str(res))
    return res


def summarize(result: GuardResult) -> dict:
    """给演示/闭环报告用的简表：每篇 通过/未报告/有误 三计数。"""
    passed = [k for k, v in result.elements.items() if v.get("status") == VERIFIED]
    return {
        "passed": passed,
        "not_reported": list(result.not_reported),
        "violations": result.violations,
        "strict_pass": result.strict_pass,
        "ok": result.ok,
    }
