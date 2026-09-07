# 评测集设计说明（500 条）

> 本目录是「企业报销预审助手」评测体系的可公开部分：**500 条逐条明细 + 生成器 + 执行器**。
> 对应简历口径：500 条（正常 400 / 轻度异常 75 / 复杂异常 25）。

## 文件
| 文件 | 内容 |
|---|---|
| `eval-detail-500.json` | 500 条逐条明细：`case_id`、`kind`、`expected_level`（人工标注期望）、`actual_conclusion`、`actual_level`、`llm_calls`、`input/output/total_tokens`、`latency` 等 |
| `generator.py` | 评测集生成器（200 行）：按分布与边界值规则批量构造报销单 + 人工标注 |
| `runner.py` | 执行器：把每条用例送入真实链路（规则引擎→L0/L1/L2），落明细 |

## 分布与设计依据（为什么是 400/75/25）

| 层 | 数量 | 设计依据 |
|---|---|---|
| 正常单（L0）| 400（80%）| 模拟真实企业报销流量结构——大多数报销合规。用于验证核心承诺「**合规单 0 Token、0 LLM 调用**」：样本必须够大，任何一条被误送进 LLM 都会被统计捕获（误调用率） |
| 轻度异常（L1）| 75（15%）| **每条规则至少有触发样本**（不是抽查是全覆盖）：住宿超标无说明 / 缺发票 / 票面金额与明细不符；验证 L1 命中准确率 |
| 复杂异常（L2）| 25（5%）| 多规则叠加 + 自然语言异常说明；验证槽位抽取 → 条款确定性匹配 → 建议生成 |

**边界值设计**（学习索引 #20）：住宿单价**恰好等于标准**（如二线城市 450 = 通过）与**超标 0.01 元**的用例都刻意构造，验证规则引擎的边界处理。

**例外豁免同义改写**：L2 用例的异常说明（如"展会满房"→TRV-EX-07、"台风航班取消"→TRV-EX-01）每个条款配多个自然语言变体，专门考察槽位抽取的鲁棒性——变体文本逐例不同。

**标注口径**：每条用例带人工标注 `expected_level`；例外豁免类另标 `expected_clause_code`（期望命中条款）。「建议准确率」= 实际建议结论与标注逐条比对。

## 三个指标与统计口径（SQL 可复算）

| 指标 | 口径 | 结果 |
|---|---|---|
| 正常单 LLM 调用率 | `normal_cases 中 llm_calls>0 的条数 / 400` | **0%** |
| 单据级 LLM 调用率 | `全部 500 条中 llm_calls>0 的条数 / 500` | 20% |
| 建议准确率 | L1/L2 用例中 `actual_conclusion` 与标注一致的比例 | 100%（对照人工标注）|
| Token 成本 | 全量 LLM 方案 Token vs 本方案 Token（93.2 万 → 18.7 万）| 省 80% |

统计 SQL（示意，`llm_calls`/`total_tokens` 取自逐条明细）：
```sql
SELECT
  COUNT(*)                                                              AS total,
  SUM(CASE WHEN kind='normal' THEN 1 ELSE 0 END)                        AS normal_cases,
  SUM(CASE WHEN kind='normal' AND llm_calls>0 THEN 1 ELSE 0 END)        AS normal_llm_called,  -- 期望 0
  SUM(CASE WHEN llm_calls>0 THEN 1 ELSE 0 END)                          AS llm_called,
  SUM(total_tokens)                                                     AS total_tokens
FROM eval_detail
GROUP BY kind;
```

## 与 Dify 云端版本的关系
- 本评测集跑在**本地 Python 版**链路（FastAPI + 规则引擎 + Chroma 知识库），产出上述指标；
- **Dify 云端版**（本仓 `dsl/`）复刻同一套分层路由，另做云端回归 22 条（见 `evidence/`）；
- 两版共用同一套规则口径与提示词设计。

---
*明细数据为程序生成的合成报销单（人名/公司均为虚构样例），无真实隐私。*
