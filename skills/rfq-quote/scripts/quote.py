#!/usr/bin/env python3
"""Deterministic RFQ costing from explicitly sourced inputs; no OCR or network IO."""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

CATEGORIES = ("直接材料", "加工费", "表面处理", "包装运输")
CRITICAL = {"material", "thickness", "welding", "heat_treatment", "tolerance", "history_loss"}
CONDITIONAL = {"surface", "hardware", "packaging"}
ROOT = Path(__file__).resolve().parents[1]


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("数值缺失或类型错误，不能以零代替")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("无效数值: %r" % value) from exc
    if not result.is_finite():
        raise ValueError("数值必须有限")
    return result


def money(value):
    return number(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def text_field(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少非空文本: " + key)
    return value.strip()


def positive(data, key, *, zero=False):
    value = number(data.get(key))
    if value < 0 or (not zero and value == 0):
        raise ValueError(key + " 必须为" + ("非负数" if zero else "正数"))
    return value


def calculate(data):
    """Return auditable amounts and reasons that prevent release of this draft."""
    for key in ("rfq_id", "customer", "part", "currency", "tax_basis", "price_version", "policy_source"):
        text_field(data, key)
    if not isinstance(data.get("demo", False), bool):
        raise ValueError("demo 必须为布尔值")
    validity = positive(data, "valid_days")
    if validity != int(validity):
        raise ValueError("valid_days 必须为整数")
    fee = positive(data, "management_rate", zero=True)
    if fee > 1:
        raise ValueError("management_rate 应使用小数比例")
    floor = positive(data, "markup_floor", zero=True)
    material = data["material"]
    for key in ("source", "utilization_reason"):
        text_field(material, key)
    area, thick, density, utilization, price, scrap = (
        positive(material, k, zero=k == "scrap_price") for k in
        ("net_area", "thickness_mm", "density", "utilization", "price", "scrap_price"))
    if utilization > 1:
        raise ValueError("utilization 必须在 (0, 1] 内")
    gross = area / utilization * thick * density
    waste = gross - area * thick * density
    lines = [
        dict(category="直接材料", item="板材毛重", quantity=gross, unit="kg", price=price,
             amount=money(gross * price), source=material["source"], basis="净面积÷利用率×厚度×密度"),
        dict(category="直接材料", item="废料回收抵减", quantity=waste, unit="kg", price=-scrap,
             amount=money(-waste * scrap), source=material["source"], basis="(毛重－净面积×厚度×密度)×回收价"),
    ]
    for row in data["cost_lines"]:
        for key in ("category", "item", "unit", "source", "basis"):
            text_field(row, key)
        if row["category"] not in CATEGORIES:
            raise ValueError("未知成本分类: " + row["category"])
        qty, rate = positive(row, "quantity", zero=True), positive(row, "price", zero=True)
        lines.append(dict(row, quantity=qty, price=rate, amount=money(qty * rate)))
    if set(x["category"] for x in lines) != set(CATEGORIES):
        raise ValueError("必须显式覆盖材料、加工、表处和包装运输，零成本亦须说明依据")
    totals = {c: sum(x["amount"] for x in lines if x["category"] == c) for c in CATEGORIES}
    manufacturing = sum(totals.values())
    if manufacturing <= 0:
        raise ValueError("制造成本必须为正")
    management = money(manufacturing * fee)
    cost = manufacturing + management

    history = data.get("history") or {}
    historical_cost, deviation = None, None
    reasons = []
    if history.get("actual_cost") is not None:
        actual = positive(history, "actual_cost")
        text_field(history, "source")
        text_field(history, "comparability")
        historical_cost = actual
        for adjustment in history.get("adjustments", []):
            text_field(adjustment, "source")
            text_field(adjustment, "reason")
            historical_cost += number(adjustment["amount"])
        if historical_cost <= 0:
            raise ValueError("历史同口径成本必须为正")
        historical_cost = money(historical_cost)
        deviation = (cost - historical_cost) / historical_cost
        if abs(deviation) > Decimal("0.08"):
            reasons.append("历史同口径成本偏差超过 8%，停止报价并排查")
        elif abs(deviation) > Decimal("0.03"):
            reasons.append("历史同口径偏差超过 3%，需解释差异并复核")
    else:
        reasons.append("缺少可靠的历史实际成本同口径校核，需技术复核")

    tiers = []
    raw_tiers = data["tiers"]
    if [t.get("kind") for t in raw_tiers] != ["standard", "urgent", "volume"]:
        raise ValueError("tiers 应依次包含 standard、urgent、volume")
    for tier in raw_tiers:
        text_field(tier, "name")
        text_field(tier, "source")
        quantity, days, markup = positive(tier, "quantity"), positive(tier, "days"), positive(tier, "markup", zero=True)
        if quantity != int(quantity) or days != int(days) or markup > 2:
            raise ValueError("数量/交期须为正整数，加成率须使用小数比例且不超过 2")
        changes = []
        for a in tier.get("adjustments", []):
            text_field(a, "item"); text_field(a, "source")
            if ("amount" in a) == ("rate" in a):
                raise ValueError("每项成本调整须二选一：amount 或 category + rate")
            if "rate" in a:
                if a.get("category") not in totals:
                    raise ValueError("比例调整必须指明成本分类")
                amount = money(totals[a["category"]] * number(a["rate"]))
            else:
                amount = money(a["amount"])
            changes.append(dict(a, amount=amount))
        if tier["kind"] == "standard" and changes:
            raise ValueError("常规档不能另加调整项，应修改基础成本")
        if tier["kind"] == "urgent" and (markup != number(raw_tiers[0]["markup"]) or any(a["amount"] < 0 for a in changes)):
            raise ValueError("加急档加成率必须与常规一致，成本增项不能为负")
        if tier["kind"] == "volume" and (quantity < number(raw_tiers[0]["quantity"]) or any(a["amount"] > 0 for a in changes)):
            raise ValueError("批量档数量不可小于常规档，降本项不能为正")
        mfg = manufacturing + sum(a["amount"] for a in changes)
        if mfg <= 0:
            raise ValueError("档位调整后制造成本必须为正")
        overhead = money(mfg * fee)
        total = mfg + overhead
        quoted = money(total * (1 + markup))
        if markup <= floor:
            reasons.append(tier["name"] + "加成率触及政策下限，需销售经理审批")
        tiers.append(dict(tier, quantity=int(quantity), markup=markup, days=int(days), adjustments=changes,
                          manufacturing=mfg, management=overhead, cost=total, price=quoted,
                          margin=(quoted-total)/quoted, profit=money((quoted-total)*quantity)))

    risks = []
    for row in data["risks"]:
        for key in ("item", "kind", "assumption", "alternative", "source", "owner", "deadline"):
            text_field(row, key)
        known = row.get("exposure") is not None
        exposure = abs(money(row["exposure"])) if known else None
        ratio = exposure / cost if known else None
        level = "A" if not known or row["kind"] in CRITICAL or ratio > Decimal("0.05") else (
            "B" if row["kind"] in CONDITIONAL or ratio >= Decimal("0.01") else "C")
        # Resolving a risk requires both an affirmative flag and an attributable record.
        if not isinstance(row.get("resolved", False), bool):
            raise ValueError("resolved 必须为布尔值")
        resolved = row.get("resolved", False)
        if resolved and not str(row.get("confirmation", "")).strip():
            raise ValueError("已解决风险必须附确认人/时间/依据")
        if level == "A" and not resolved:
            reasons.append("A 级待确认：" + row["item"])
        risks.append(dict(row, exposure=exposure, level=level, resolved=resolved,
                          order_exposure=money(exposure * tiers[0]["quantity"]) if known else None))
    if data.get("demo"):
        reasons.insert(0, "虚构演示数据，仅供流程体验")
    # Outputs always remain drafts; this tool cannot approve or send a quotation.
    return dict(lines=lines, totals=totals, manufacturing=manufacturing, management=management,
                cost=cost, tiers=tiers, risks=risks, history_cost=historical_cost, deviation=deviation,
                reasons=list(dict.fromkeys(reasons)), status="内部草稿：禁止发出" if reasons else "内部草稿：待最终审批")


class Report:
    def __init__(self, path):
        import xlsxwriter
        self.book = xlsxwriter.Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False})
        self.formats = {
            "text": self.book.add_format({"font_name": "Microsoft YaHei", "font_size": 11, "valign": "top", "text_wrap": True}),
            "number": self.book.add_format({"font_name": "Microsoft YaHei", "font_size": 11, "num_format": "#,##0.00;[Red](#,##0.00);–", "valign": "top"}),
            "percent": self.book.add_format({"font_name": "Microsoft YaHei", "font_size": 11, "num_format": "0.0%", "valign": "top"}),
            "quantity": self.book.add_format({"font_name": "Microsoft YaHei", "font_size": 11, "num_format": "0.000000", "valign": "top"}),
            "integer": self.book.add_format({"font_name": "Microsoft YaHei", "font_size": 11, "num_format": "#,##0", "valign": "top"}),
            "header": self.book.add_format({"font_name": "Microsoft YaHei", "bold": True, "font_color": "#FFFFFF", "bg_color": "#17365D", "text_wrap": True, "valign": "vcenter"}),
        }

    def sheet(self, name, headers, widths):
        s = self.book.add_worksheet(name)
        s.hide_gridlines(2); s.freeze_panes(1, 2)
        for i, width in enumerate(widths):
            s.set_column(i, i, width)
        s.write_row(0, 0, headers, self.formats["header"])
        s.set_row(0, 34)
        s.set_landscape(); s.set_paper(9); s.fit_to_pages(1, 0); s.repeat_rows(0)
        return s

    def row(self, sheet, row, values, *, height=36, formats=None):
        sheet.set_row(row, height)
        for col, value in enumerate(values):
            if value is None:
                value = "待评估"
            if value == "":
                sheet.write_blank(row, col, None, self.formats["text"])
            elif isinstance(value, tuple):
                formula, cached, *kind = value
                sheet.write_formula(row, col, formula, self.formats[kind[0] if kind else "number"], float(cached))
            elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                sheet.write_number(row, col, float(value), self.formats[(formats or {}).get(col, "number")])
            else:
                sheet.write_string(row, col, str(value), self.formats["text"])

    def close(self):
        self.book.close()


def add_cost_sheet(report, data, result):
    s = report.sheet("成本明细", ["分类", "成本项目", "用量", "单位", "单价", "金额", "取数来源", "计算依据"], [14, 24, 15, 10, 14, 16, 52, 45])
    for i, line in enumerate(result["lines"], 1):
        report.row(s, i, [line["category"], line["item"], line["quantity"], line["unit"], line["price"],
                         (f"=ROUND(C{i+1}*E{i+1},2)", line["amount"]), line["source"], line["basis"]], height=48, formats={2: "quantity"})
    last = len(result["lines"]) + 1
    s.autofilter(0, 0, last-1, 7)
    detail_end = last
    for i, category in enumerate(CATEGORIES):
        report.row(s, last+i, ["汇总", category, "", "元/台", "", (f'=SUMIF(A2:A{detail_end},B{last+i+1},F2:F{detail_end})', result["totals"][category])])
    last += len(CATEGORIES)
    report.row(s, last, ["汇总", "制造成本", "", "元/台", "", (f"=SUM(F{last-3}:F{last})", result["manufacturing"])])
    report.row(s, last+1, ["汇总", "管理费", ("="+str(data["management_rate"]), number(data["management_rate"]), "percent"), "比例", "",
                             (f"=ROUND(F{last+1}*C{last+2},2)", result["management"]), data["policy_source"]])
    report.row(s, last+2, ["汇总", "单台总成本", "", "元/台", "", (f"=SUM(F{last+1}:F{last+2})", result["cost"])])
    # Geometry is visible, and the material rows link to it rather than pasted rounded weights.
    geo = last+5
    m = data["material"]
    report.row(s, geo, ["材料参数", "净展开面积 / m²", m["net_area"], "厚度 / mm", m["thickness_mm"], "", m["source"]])
    report.row(s, geo+1, ["材料参数", "利用率", ("="+str(m["utilization"]), number(m["utilization"]), "percent"), "密度", m["density"], "", m["utilization_reason"]])
    s.write_formula("C2", f"=C{geo+1}/C{geo+2}*E{geo+1}*E{geo+2}", report.formats["quantity"], float(result["lines"][0]["quantity"]))
    s.write_formula("C3", f"=C2-C{geo+1}*E{geo+1}*E{geo+2}", report.formats["quantity"], float(result["lines"][1]["quantity"]))
    return last+1, last+3


def export(data, result, output):
    output = Path(output)
    names = ["成本核算表.xlsx", "报价单草稿.xlsx", "待确认清单.xlsx", "核算结果.json"]
    if any((output/n).exists() for n in names):
        raise ValueError("输出目录已有本次成果，请换一个询价目录以保留历史版本")
    output.mkdir(parents=True, exist_ok=True)
    for name in names[:3]:
        r = Report(output/name)
        try:
            summary = r.sheet("报价状态", ["项目", "值 / 说明"], [26, 110])
            rows = [("询价", data["rfq_id"]), ("客户 / 件号", data["customer"]+" / "+data["part"]),
                    ("当前状态", result["status"]), ("币种 / 税口径", data["currency"]+" / "+data["tax_basis"]),
                    ("材料价格版本", data["price_version"]), ("报价有效期（天）", data["valid_days"]),
                    ("政策来源", data["policy_source"]), ("使用说明", "本次输入的内部核算快照；修改假设后重新运行并重新审批，不能据此自动发送。")]
            rows += [("待处理", reason) for reason in result["reasons"]]
            for i, row in enumerate(rows, 1): r.row(summary, i, row, height=40)
            if name != names[2]:
                mfg_row, cost_row = add_cost_sheet(r, data, result)
            if name == names[0]:
                s = r.sheet("历史校核", ["项目", "金额 / 偏差", "依据"], [32, 22, 90])
                h = data.get("history") or {}
                r.row(s, 1, ["历史实际成本", h.get("actual_cost"), h.get("source", "缺少可比实际成本明细")])
                for i, a in enumerate(h.get("adjustments", []), 2): r.row(s, i, [a["reason"], a["amount"], a["source"]])
                pos = len(h.get("adjustments", []))+3
                r.row(s, pos, ["换算后历史成本", (f"=SUM(B2:B{pos-1})", result["history_cost"]) if result["history_cost"] is not None else None, h.get("comparability", "未完成同口径换算，不能判通过")])
                r.row(s, pos+1, ["本次独立正算成本", (f"='成本明细'!F{cost_row}", result["cost"]), "独立成本明细"])
                r.row(s, pos+2, ["相对偏差", (f"=(B{pos+2}-B{pos+1})/B{pos+1}", result["deviation"], "percent") if result["deviation"] is not None else None, "绝对值 >8% 停止报价；>3% 须解释并复核"])
            if name == names[1]:
                s = r.sheet("三档报价", ["档位", "数量", "交期(天)", "制造成本", "管理费", "总成本", "加成率", "单价", "毛利率", "订单毛利", "定价依据"], [16, 12, 12, 16, 16, 16, 12, 16, 12, 18, 55])
                a = r.sheet("档位成本调整", ["档位", "项目", "单台差额", "依据"], [18, 30, 18, 90])
                ar = 1
                for i, tier in enumerate(result["tiers"], 1):
                    start = ar+1
                    for change in tier["adjustments"]:
                        if "rate" in change:
                            formula = f'=ROUND(SUMIF(\'成本明细\'!A2:A{mfg_row-1},"{change["category"]}",\'成本明细\'!F2:F{mfg_row-1})*{change["rate"]},2)'
                            value = (formula, change["amount"])
                        else: value = change["amount"]
                        r.row(a, ar, [tier["name"], change["item"], value, change["source"]], height=48); ar += 1
                    adjustment = f"+SUM('档位成本调整'!C{start}:C{ar})" if ar >= start else ""
                    row = i+1
                    r.row(s, i, [tier["name"], tier["quantity"], tier["days"],
                        (f"='成本明细'!F{mfg_row}{adjustment}", tier["manufacturing"]),
                        (f"=ROUND(D{row}*'成本明细'!C{mfg_row+1},2)", tier["management"]),
                        (f"=D{row}+E{row}", tier["cost"]),
                        ("=G2" if tier["kind"] == "urgent" else "="+str(tier["markup"]), tier["markup"], "percent"),
                        (f"=ROUND(F{row}*(1+G{row}),2)", tier["price"]),
                        (f"=(H{row}-F{row})/H{row}", tier["margin"], "percent"),
                        (f"=ROUND((H{row}-F{row})*B{row},2)", tier["profit"]), tier["source"]], height=64, formats={1: "integer", 2: "integer"})
            if name == names[2]:
                s = r.sheet("风险明细", ["等级", "不确定项", "当前口径", "替代口径", "单台总成本敞口", "订单敞口", "负责人", "时限", "状态 / 确认依据", "来源"], [10, 30, 32, 32, 20, 20, 18, 20, 45, 60])
                for i, risk in enumerate(result["risks"], 1):
                    exposure = (f"=ROUND(E{i+1}*{result['tiers'][0]['quantity']},2)", risk["order_exposure"]) if risk["exposure"] is not None else None
                    r.row(s, i, [risk["level"], risk["item"], risk["assumption"], risk["alternative"], risk["exposure"], exposure,
                        risk["owner"], risk["deadline"], risk.get("confirmation") if risk["resolved"] else "待确认", risk["source"]], height=72)
        finally:
            r.close()
    (output/names[3]).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=lambda x: float(x) if isinstance(x, Decimal) else str(x))+"\n", encoding="utf-8")
    return [output/n for n in names]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true"); mode.add_argument("--input", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        data = json.loads((ROOT/"assets/demo/quote-input.json" if args.demo else args.input).read_text(encoding="utf-8"))
        if args.demo: data["demo"] = True
        result = calculate(data)
        files = export(data, result, args.output)
    except (ValueError, KeyError, TypeError) as exc:
        p.error(str(exc))
    print(json.dumps({"status": result["status"], "reasons": result["reasons"], "files": [str(x.resolve()) for x in files]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
