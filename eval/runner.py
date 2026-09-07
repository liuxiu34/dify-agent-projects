"""批量跑测

依据文档 D2 §2.4 目录树：src/eval/runner.py —— 批量跑测
依据文档 D2 §3-M6 任务 5：eval/runner + eval/cost_report：双通道 vs 全量 LLM 成本对照
依据文档 D2 §2.5 模块职责（eval/runner）：批量执行评测用例并收集指标。

跑测口径（M6-3，文档空白 A12 一并登记）：
    - 每个用例：落库 → 规则引擎（通道 A，0 Token）→ 异常单走 agent 管线（通道 B，真实 LLM）；
    - use_cache=False：评测关闭相似异常缓存，测得的是双通道架构本身的成本
      （缓存收益已在 M6-2 单独实测：命中单 0 Token）；
    - 每个用例跑完即清理数据库行，评测不污染业务表；
    - 单个用例失败（LLM 超时/解析失败）不中断批次，记录 error 供报告统计。

指标定义（写入报告）：
    - 单据级 LLM 调用率 = 产生 ≥1 次 LLM 调用的单据数 / 总单据数（验收①口径）
    - 正常单调用率 = normal 类用例中产生 LLM 调用的比例（验收①要求 = 0%）
    - 建议准确率 = 带条款标注用例中「匹配条款 = 标注条款」的比例（验收③口径）
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.agent.pipeline import run_analysis
from src.eval.generator import CaseSpec
from src.rules.engine import RuleEngine, build_context, persist_hits


@dataclass
class CaseResult:
    """单个用例的跑测结果"""
    case_id: str
    kind: str
    expected_level: str
    actual_conclusion: str = ""
    actual_level: str = ""
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    matched_clause_code: str | None = None
    expected_clause_code: str | None = None
    suggestion: str = ""
    error: str | None = None

    @property
    def had_llm_call(self) -> bool:
        return self.llm_calls > 0

    @property
    def suggestion_correct(self) -> bool | None:
        """验收③口径：仅带标注的用例参与统计（None = 不参与）"""
        if self.expected_clause_code is None or self.error:
            return None
        return self.matched_clause_code == self.expected_clause_code


@dataclass
class BatchResult:
    """一批跑测的汇总结果（cost_report 的输入）"""
    results: list[CaseResult] = field(default_factory=list)
    wall_seconds: float = 0.0


# ---------------------------------------------------------------------------
# 落库与清理
# ---------------------------------------------------------------------------

def _insert_case(session: Session, case: CaseSpec) -> int:
    """把 CaseSpec 落库（reimbursement + items + invoices），返回 reimb_id。

    行程区间按明细推导：起点 = 最早发生日期；终点 = 各明细「发生日期 + 覆盖天数 -
    1」的最大值（住宿按 nights、餐补按 days 覆盖），否则 TRV-ACC-002（夜数 ≤ 出差
    自然天数）与餐补天数规则会把多晚住宿误判为异常；提交时间取行程结束后第 5 天。
    """
    code = f"E-{uuid.uuid4().hex[:10]}"
    trip_start = min(i.happen_date for i in case.items)
    trip_end = trip_start
    for it in case.items:
        d = date.fromisoformat(it.happen_date)
        span = max(int(it.ext.get("nights", 1)), int(it.ext.get("days", 1)), 1)
        trip_end = max(trip_end, (d + timedelta(days=span - 1)).isoformat())
    submit_at = (date.fromisoformat(trip_end) + timedelta(days=5)).isoformat() + " 12:00:00"
    rid = session.execute(text(
        "INSERT INTO reimbursement (code,user_id,status,trip_start,trip_end,city_level,total_amount,submit_at) "
        "VALUES (:c,:u,'submitted',:ts,:te,:cl,:amt,:sa)"
    ), {"c": code, "u": 950000 + (uuid.uuid4().int % 40000),
        "ts": trip_start, "te": trip_end,
        "cl": case.city_level, "amt": f"{case.total_amount:.2f}", "sa": submit_at}).lastrowid  # type: ignore[attr-defined]
    for it in case.items:
        iid = session.execute(text(
            "INSERT INTO reimbursement_item (reimb_id,category,amount,happen_date,reason_text,ext_json) "
            "VALUES (:r,:c,:a,:d,:t,:e)"
        ), {"r": rid, "c": it.category, "a": f"{it.amount:.2f}", "d": it.happen_date,
            "t": it.reason_text,
            "e": json.dumps(it.ext, ensure_ascii=False)}).lastrowid  # type: ignore[attr-defined]
        if it.invoice_amount is not None:
            session.execute(text(
                "INSERT INTO invoice (item_id,invoice_no,amount,attached,title_valid) "
                "VALUES (:i,:no,:a,:at,:tv)"
            ), {"i": iid, "no": f"EVAL-{uuid.uuid4().hex[:10]}",
                "a": f"{it.invoice_amount:.2f}",
                "at": 1 if it.invoice_attached else 0,
                "tv": 1 if it.invoice_title_valid else 0})
    session.commit()
    return int(rid)


def _cleanup_case(session: Session, reimb_id: int) -> None:
    """删除单据全部关联行（评测不污染业务表；避免坑 #8 式的表状态残留）"""
    session.execute(text("DELETE FROM llm_request_log WHERE reimb_id = :r"), {"r": reimb_id})
    session.execute(text("DELETE FROM agent_analysis WHERE reimb_id = :r"), {"r": reimb_id})
    session.execute(text("DELETE FROM rule_hit_log WHERE reimb_id = :r"), {"r": reimb_id})
    session.execute(text(
        "DELETE FROM invoice WHERE item_id IN "
        "(SELECT id FROM reimbursement_item WHERE reimb_id = :r)"), {"r": reimb_id})
    session.execute(text("DELETE FROM reimbursement_item WHERE reimb_id = :r"), {"r": reimb_id})
    session.execute(text("DELETE FROM reimbursement WHERE id = :r"), {"r": reimb_id})
    session.commit()


# ---------------------------------------------------------------------------
# 单用例执行
# ---------------------------------------------------------------------------

def run_case(session: Session, case: CaseSpec) -> CaseResult:
    """执行单个用例：规则引擎 → 异常单走 agent 管线 → 收集指标 → 清理"""
    res = CaseResult(case_id=case.case_id, kind=case.kind,
                     expected_level=case.expected_level,
                     expected_clause_code=case.expected_clause_code)
    rid: int | None = None
    try:
        rid = _insert_case(session, case)

        # 通道 A：规则引擎（0 Token，依据 D2 §2.3）
        reimb = session.execute(text(
            "SELECT id,code,user_id,trip_start,trip_end,city_level,total_amount,submit_at "
            "FROM reimbursement WHERE id = :r"), {"r": rid}).mappings().one()
        items = session.execute(text(
            "SELECT id,category,amount,happen_date,reason_text,ext_json "
            "FROM reimbursement_item WHERE reimb_id = :r"), {"r": rid}).mappings().all()
        invoices = session.execute(text(
            "SELECT i.id,i.item_id,i.invoice_no,i.amount,i.attached,i.title_valid "
            "FROM invoice i JOIN reimbursement_item it ON it.id = i.item_id "
            "WHERE it.reimb_id = :r"), {"r": rid}).mappings().all()
        ctx = build_context(session, dict(reimb), [dict(i) for i in items],
                            [dict(i) for i in invoices])
        rule_result = RuleEngine().run(ctx)
        persist_hits(session, rid, rule_result)
        session.commit()
        res.actual_conclusion = rule_result.conclusion
        res.actual_level = rule_result.level

        # 通道 B：异常单走 agent 管线（use_cache=False，评测口径见模块注释）
        if rule_result.conclusion in ("anomaly", "reject"):
            outcome = run_analysis(
                session, rid, case.anomaly_text or "（无异常说明）",
                level=rule_result.level,
                request_id=f"eval-{case.case_id}",
                use_cache=False,
            )
            res.llm_calls = outcome.llm_calls
            res.matched_clause_code = outcome.matched_clause_code
            res.suggestion = outcome.suggestion
            # Token 从 llm_request_log 聚合读取（统计口径单一，token_stats.summarize）
            from src.agent.middleware.token_stats import summarize_by_reimb
            usage = summarize_by_reimb(session, rid)
            res.input_tokens = usage["input_tokens"]
            res.output_tokens = usage["output_tokens"]
            res.total_tokens = usage["total_tokens"]
            res.latency_ms = usage["latency_ms"]
    except Exception as e:  # 单用例失败不中断批次（降级为记录 error，见模块注释）
        res.error = f"{type(e).__name__}: {e}"
    finally:
        if rid is not None:
            _cleanup_case(session, rid)
    return res


def run_batch(cases: list[CaseSpec], session: Session,
              log_every: int = 20) -> BatchResult:
    """顺序执行一批用例（严格串行，与 H1 一致；返回 BatchResult）"""
    batch = BatchResult()
    started = time.perf_counter()
    for idx, case in enumerate(cases, 1):
        res = run_case(session, case)
        batch.results.append(res)
        if log_every and idx % log_every == 0:
            done_llm = sum(1 for r in batch.results if r.had_llm_call)
            print(f"  [runner] {idx}/{len(cases)} 完成，其中 {done_llm} 单触发 LLM", flush=True)
    batch.wall_seconds = time.perf_counter() - started
    return batch


def to_dict_list(batch: BatchResult) -> list[dict[str, Any]]:
    """结果序列化（供 JSON 报告落盘）"""
    return [asdict(r) for r in batch.results]


# ---------------------------------------------------------------------------
# 命令行入口（python -m src.eval.runner [N] [seed]）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from pathlib import Path

    from src.db.session import SessionLocal
    from src.eval.cost_report import build_report, save_report
    from src.eval.generator import generate_cases

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20260829

    print(f"生成 {n} 张评测单（正常 80% / L1 15% / L2 5%，seed={seed}），"
          f"开始批量跑测（关闭缓存）...", flush=True)
    _session = SessionLocal()
    try:
        _batch = run_batch(generate_cases(n, seed=seed), _session, log_every=20)
    finally:
        _session.close()
    _rep = build_report(_batch)
    _md, _js = save_report(_rep, to_dict_list(_batch), Path("eval_reports"))
    _errors = [r for r in _batch.results if r.error]
    print(f"\n跑测完成：{len(_batch.results)} 单，耗时 {_batch.wall_seconds:.0f}s，出错 {len(_errors)} 单")
    for _r in _errors[:5]:
        print(f"  ERROR {_r.case_id}: {(_r.error or '')[:120]}")
    print(f"\n单据级调用率 {_rep.receipt_level_call_rate:.1%}  正常单调用率 {_rep.normal_call_rate:.1%}")
    print(f"Token: 实测 {_rep.actual_total_tokens:,} vs 基线 {_rep.baseline_total_tokens:,}"
          f" → 节省 {_rep.token_saving_ratio:.1%}")
    print(f"建议准确率 {_rep.suggestion_accuracy}")
    print(f"报告：{_md}\n      {_js}")
