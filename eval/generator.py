"""报销单评测数据生成器

依据文档 D2 §2.4 目录树：src/eval/generator.py —— 【核心】报销单生成器（300~500 张，分布可控）
依据文档 D2 §3-M6 任务 4：eval/generator：生成 300~500 张（正常 80% / L1 15% / L2 5%）
依据文档 D2 §2.5 模块职责（eval/generator）：输入 数量+分布，输出 报销单列表
依据文档 D2 §4 学习索引 #20：测试数据分布设计；边界值分析。

设计（M6-3）：
    - 每个用例 = CaseSpec（明细/发票/异常说明/人工标注），由 runner 落库后走真实链路；
    - 正常单（80%）：全部硬规则合规 → 期望 L0 / 0 Token；
    - L1 简单异常（15%）：单类别问题、无自然语言原因说明 → 期望 L1
      （住宿超标无说明 / 缺一张发票 / 票金额与明细不符）；
    - L2 复杂异常（5%）：含自然语言原因说明 → 期望 L2，其中例外豁免类带条款标注
      （展会满房 → TRV-EX-07；台风航班取消 → TRV-EX-01），供验收③建议准确率比对；
    - 边界值（学习索引 #20）：住宿单价恰等于标准（450 = pass）与超标 0.01 的用例
      分布在正常/L1 模板中。

标注口径（文档空白 A12，已登记待确认）：验收③「建议准确率」的对照标注 =
    例外豁免类用例的期望条款编码（expected_clause_code）；无标注的用例不参与准确率统计。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

# 城市住宿标准（依据 conf/policy_clauses.yaml TRV-ACC-01；与规则引擎查表一致）
HOTEL_STANDARD = {"一线城市": 700, "二线城市": 450}
MEAL_DAILY = {"一线城市": 100, "二线城市": 80, "三线及以下城市": 60}

# L2 例外豁免模板：异常说明变体 → 期望条款（依据 conf/policy_clauses.yaml / policy_doc.md 第六章）
EXCEPTION_TEMPLATES = [
    # 展会满房 → TRV-EX-07（含同义改写，考察抽取鲁棒性；文本逐例不同）
    ("{month}月参加{city}国际{trade}展，展会期间同城协议酒店全部满房，只能入住价格更高的酒店",
     "TRV-EX-07"),
    ("{month}月在{city}参展期间，周边协议酒店因展会全部订满，被迫选择高价酒店入住",
     "TRV-EX-07"),
    # 台风航班取消 → TRV-EX-01（不可抗力）
    ("{month}月因台风{typhoon}导致航班取消，被迫在{city}滞留一晚并改签",
     "TRV-EX-01"),
    ("{month}月{city}遭遇台风{typhoon}，返程航班被取消，额外产生一晚住宿",
     "TRV-EX-01"),
]

_TRADES = ["家具", "电子", "汽配", "医疗", "纺织", "建材", "食品", "物流"]
_CITIES = {"二线城市": ["成都", "武汉", "西安", "杭州", "南京"],
           "一线城市": ["北京", "上海", "广州", "深圳"]}
_TYPHOONS = ["梅花", "烟花", "杜苏芮", "海葵", "苏拉"]

HOTEL_ITEM, MEAL_ITEM, TRANSPORT_ITEM = "hotel", "meal", "transport"


@dataclass
class ItemSpec:
    """一条费用明细（形态与 reimbursement_item / invoice 对齐）"""
    category: str
    amount: float
    happen_date: str
    reason_text: str | None = None
    ext: dict = field(default_factory=dict)
    invoice_amount: float | None = None   # None = 不开发票（attached=0 或免票品类）
    invoice_attached: bool = True
    invoice_title_valid: bool = True


@dataclass
class CaseSpec:
    """一个评测用例 = 一张报销单 + 人工标注"""
    case_id: str
    kind: str                      # normal / l1 / l2
    expected_level: str            # L0 / L1 / L2（规则引擎应输出的等级）
    city_level: str
    items: list[ItemSpec]
    anomaly_text: str | None = None          # 异常说明（L1/L2 用例）
    expected_clause_code: str | None = None  # 例外豁免类标注（验收③）
    note: str = ""

    @property
    def total_amount(self) -> float:
        return round(sum(i.amount for i in self.items), 2)


def _hotel_item(day: str, unit_price: float, nights: int = 1, with_invoice: bool = True,
                title_valid: bool = True) -> ItemSpec:
    amount = round(unit_price * nights, 2)
    return ItemSpec(
        category=HOTEL_ITEM, amount=amount, happen_date=day,
        ext={"unit_price": unit_price, "nights": nights, "has_folio": True},
        invoice_amount=amount if with_invoice else None,
        invoice_attached=with_invoice, invoice_title_valid=title_valid,
    )


def _meal_item(day: str, daily: float, days: int) -> ItemSpec:
    return ItemSpec(
        category=MEAL_ITEM, amount=round(daily * days, 2), happen_date=day,
        ext={"daily_amount": daily, "days": days},
        invoice_amount=None,  # 依据 TRV-MEA-01：餐补按制度无需发票（坑 #7）
    )


def _transport_item(day: str, amount: float, with_invoice: bool = True,
                    invoice_amount: float | None = None) -> ItemSpec:
    return ItemSpec(
        category=TRANSPORT_ITEM, amount=amount, happen_date=day,
        ext={"sub_category": "高铁", "seat_class": "二等座"},
        invoice_amount=(invoice_amount if invoice_amount is not None else amount) if with_invoice else None,
        invoice_attached=with_invoice,
    )


def _normal_case(cid: str, rng: random.Random) -> CaseSpec:
    """正常单：全部合规 → L0。含边界值（单价恰等于标准）。"""
    city_level = rng.choice(["二线城市", "一线城市"])
    city = rng.choice(_CITIES[city_level])
    month = rng.randint(1, 12)
    day = f"2026-{month:02d}-{rng.randint(10, 25):02d}"
    std = HOTEL_STANDARD[city_level]
    # 边界值：约 1/3 用例单价恰等于标准（450/700，应判 pass）
    unit = std if rng.random() < 0.34 else rng.randint(int(std * 0.6), std - 1)
    nights = rng.randint(1, 3)
    items = [
        _hotel_item(day, unit, nights),
        _meal_item(day, MEAL_DAILY[city_level], days=nights + 1),
        _transport_item(day, rng.choice([210.0, 306.0, 553.0, 144.5])),
    ]
    return CaseSpec(cid, "normal", "L0", city_level, items,
                    note=f"合规单（{city}，单价 {unit} = 边界或标准内）")


def _l1_case(cid: str, rng: random.Random) -> CaseSpec:
    """L1 简单异常：单类别、无原因说明。三种模板轮换。"""
    city_level = rng.choice(list(HOTEL_STANDARD))
    month = rng.randint(1, 12)
    day = f"2026-{month:02d}-{rng.randint(10, 25):02d}"
    std = HOTEL_STANDARD[city_level]
    flavor = rng.randint(0, 2)
    if flavor == 0:
        # 边界值 +0.01：住宿单价略超标准，无说明 → TRV-ACC-001，L1
        items = [_hotel_item(day, std + 0.01)]
        note = f"住宿超标 {std + 0.01:.2f}（边界 +0.01），无说明 → L1"
        text = None
    elif flavor == 1:
        # 缺一张发票（交通），无说明 → TRV-INV-003，L1
        items = [_hotel_item(day, std), _transport_item(day, 300.0, with_invoice=False)]
        note = "交通缺一张发票，无说明 → L1"
        text = None
    else:
        # 发票金额与明细不符 → TRV-INV-004，L1
        items = [_transport_item(day, 300.0, invoice_amount=250.0)]
        note = "发票金额与明细不符 → L1"
        text = None
    return CaseSpec(cid, "l1", "L1", city_level, items, anomaly_text=text, note=note)


def _l2_case(cid: str, rng: random.Random) -> CaseSpec:
    """L2 复杂异常：含自然语言原因说明。例外豁免类带条款标注（验收③）。"""
    city_level = rng.choice(list(_CITIES))
    month = rng.randint(1, 12)
    day = f"2026-{month:02d}-{rng.randint(10, 25):02d}"
    std = HOTEL_STANDARD.get(city_level, 450)
    tmpl, expected = EXCEPTION_TEMPLATES[rng.randrange(len(EXCEPTION_TEMPLATES))]
    anomaly_text = tmpl.format(
        month=month, city=rng.choice(_CITIES[city_level]), trade=rng.choice(_TRADES),
        typhoon=rng.choice(_TYPHOONS),
    )
    over = rng.choice([1.15, 1.25, 1.33])          # 超标 15%~33%（不同审批档）
    items = [
        # 异常说明写入明细 reason_text（规则引擎据 has_reason_text 判 L2，
        # 与真实链路一致：员工的说明随明细提交）
        _hotel_item(day, round(std * over, 2)),
        _meal_item(day, MEAL_DAILY[city_level], days=2),
    ]
    items[0].reason_text = anomaly_text
    return CaseSpec(
        cid, "l2", "L2", city_level, items, anomaly_text=anomaly_text,
        expected_clause_code=expected,
        note=f"例外豁免类 L2，期望条款 {expected}，住宿超标 {over}",
    )


def generate_cases(n: int = 500, seed: int = 20260829) -> list[CaseSpec]:
    """按分布生成 n 个用例：正常 80% / L1 15% / L2 5%（依据 D2 §3-M6 任务 4）。

    Args:
        n: 用例总数（文档要求 300~500，验收①按 500 跑测）
        seed: 随机种子（同种子可复现同一批数据，评测可复现）
    """
    rng = random.Random(seed)
    n_l1 = round(n * 0.15)
    n_l2 = round(n * 0.05)
    n_normal = n - n_l1 - n_l2
    cases: list[CaseSpec] = []
    for i in range(n_normal):
        cases.append(_normal_case(f"eval-{i:04d}", rng))
    for i in range(n_l1):
        cases.append(_l1_case(f"eval-{n_normal + i:04d}", rng))
    for i in range(n_l2):
        cases.append(_l2_case(f"eval-{n_normal + n_l1 + i:04d}", rng))
    rng.shuffle(cases)
    return cases
