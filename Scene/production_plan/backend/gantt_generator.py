"""
甘特图HTML生成器
支持设备/订单双视图切换的交互式甘特图
"""

from Scene.catalog import skill_path

import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any
from common.log import logger


# 尝试加载技能包模板
_TEMPLATE_CACHE = None


def _load_template() -> str:
    """加载技能包甘特图模板"""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE

    possible_paths = [
        str(skill_path('pmc-scheduler').joinpath('assets', 'gantt_template.html')),
        str(skill_path('pmc-scheduler').joinpath('gantt_template.html')),
    ]

    for path in possible_paths:
        path = os.path.normpath(path)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    _TEMPLATE_CACHE = f.read()
                logger.info(f"[GanttGenerator] Loaded template from {path}")
                return _TEMPLATE_CACHE
            except Exception as e:
                logger.warning(f"[GanttGenerator] Failed to load template from {path}: {e}")

    logger.warning("[GanttGenerator] Could not find skill package template, using inline fallback")
    return None


class GanttGenerator:
    """甘特图生成器（支持设备/订单双视图切换 + 物料需求计划）"""

    def __init__(self):
        self.colors = [
            "#3b82f6", "#10b981", "#f59e0b", "#ef4444",
            "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16",
            "#f97316", "#6366f1", "#14b8a6", "#d946ef"
        ]

    def generate(self, schedule: List[Dict], title: str = "生产排产甘特图", order_gantt: List[Dict] = None) -> str:
        """
        生成甘特图HTML（支持设备/订单双视图 + 物料需求计划）

        Args:
            schedule: 排程结果列表
            title: 图表标题
            order_gantt: 订单甘特图数据（按订单分组），为None时从schedule自动派生

        Returns:
            HTML字符串
        """
        if not schedule:
            return self._empty_gantt(title)

        # 优先使用技能包模板
        template = _load_template()
        if template:
            return self._render_from_template(schedule, template, title, order_gantt)
        else:
            # Fallback to inline generation
            data = self._prepare_data(schedule)
            return self._build_html(data, title)

    def _prepare_data(self, schedule: List[Dict]) -> Dict:
        """准备D3.js所需的数据格式"""
        tasks = []
        machines = set()
        products = set()

        min_time = None
        max_time = None

        for item in schedule:
            start = datetime.fromisoformat(item["start_time"])
            end = datetime.fromisoformat(item["end_time"])

            if min_time is None or start < min_time:
                min_time = start
            if max_time is None or end > max_time:
                max_time = end

            machines.add(item["machine_id"])
            products.add(item["product"])

            color = self._get_status_color(item["status"])

            tasks.append({
                "id": f"{item['order_id']}_{item['process_name']}",
                "order_id": item["order_id"],
                "product": item["product"],
                "process": item["process_name"],
                "machine": item["machine_id"],
                "start": item["start_time"],
                "end": item["end_time"],
                "quantity": item["quantity"],
                "status": item["status"],
                "delay_hours": item.get("delay_hours", 0),
                "color": color
            })

        return {
            "tasks": tasks,
            "machines": sorted(list(machines)),
            "products": sorted(list(products)),
            "min_time": min_time.isoformat() if min_time else "",
            "max_time": max_time.isoformat() if max_time else ""
        }

    def _get_status_color(self, status: str) -> str:
        """根据状态获取颜色"""
        color_map = {
            "ok": "#10b981",           # 绿色
            "delayed": "#ef4444",       # 红色
            "material_shortage": "#f59e0b"  # 橙色
        }
        return color_map.get(status, "#3b82f6")

    def _empty_gantt(self, title: str) -> str:
        """空甘特图"""
        return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; margin: 20px; background: #f8fafc; }}
        .empty {{ text-align: center; padding: 60px; color: #94a3b8; }}
    </style>
</head>
<body>
    <div class="empty">
        <h2>暂无排产数据</h2>
        <p>请先执行排产计算</p>
    </div>
</body>
</html>
"""

    def _render_from_template(self, schedule: List[Dict], template: str, title: str, order_gantt: List[Dict] = None) -> str:
        """使用技能包模板渲染甘特图HTML"""
        # 统计摘要
        total_orders = len(set(item["order_id"] for item in schedule))
        scheduled_orders = total_orders
        total_items = len(schedule)
        shortage_count = sum(1 for item in schedule if item.get("material_status") == "shortage" or item.get("status") == "material_shortage")
        shortage_class = "danger" if shortage_count > 0 else "success"

        start_dates = [datetime.fromisoformat(item["start_time"]) for item in schedule]
        end_dates = [datetime.fromisoformat(item["end_time"]) for item in schedule]
        start_date = min(start_dates).strftime("%Y-%m-%d") if start_dates else "-"
        end_date = max(end_dates).strftime("%Y-%m-%d") if end_dates else "-"

        # 生成订单甘特图
        if order_gantt is None:
            order_gantt = self._derive_order_gantt(schedule)
        order_gantt_html = self._build_order_gantt_fragment(order_gantt)

        # 生成排产明细表
        detail_rows = self._build_detail_rows(schedule)

        # 生成物料需求行（占位，由调用方传入material_plan时填充）
        material_rows = '<tr><td colspan="7" style="text-align:center; color:#909399; padding:30px;">物料需求计划数据由排产引擎计算后填充</td></tr>'

        # 准备D3.js数据
        schedule_json = json.dumps(schedule, ensure_ascii=False, default=str)

        # 替换模板变量
        html = template
        html = html.replace("{{generation_time}}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        html = html.replace("{{total_orders}}", str(total_orders))
        html = html.replace("{{scheduled_orders}}", str(scheduled_orders))
        html = html.replace("{{total_items}}", str(total_items))
        html = html.replace("{{shortage_count}}", str(shortage_count))
        html = html.replace("{{shortage_class}}", shortage_class)
        html = html.replace("{{start_date}}", start_date)
        html = html.replace("{{end_date}}", end_date)
        html = html.replace("{{order_gantt_html}}", order_gantt_html)
        html = html.replace("{{detail_rows}}", "\n".join(detail_rows))
        html = html.replace("{{material_rows}}", material_rows)
        html = html.replace("{{schedule_json}}", schedule_json)

        return html

    def _derive_order_gantt(self, schedule: List[Dict]) -> List[Dict]:
        """从扁平schedule数据派生订单甘特图数据"""
        orders = {}
        for item in schedule:
            oid = item.get("order_id", "")
            if not oid:
                continue
            if oid not in orders:
                orders[oid] = {
                    "order_id": oid,
                    "product": item.get("product", ""),
                    "quantity": item.get("quantity", 0),
                    "is_delayed": item.get("status") == "delayed",
                    "processes": [],
                    "semi_finished": []
                }
            orders[oid]["processes"].append({
                "process_name": item.get("process_name", item.get("process", "")),
                "start_time": item.get("start_time", item.get("start", "")),
                "end_time": item.get("end_time", item.get("end", ""))
            })
        return list(orders.values())

    def _to_datetime(self, value) -> datetime:
        """将字符串转为datetime"""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                try:
                    return datetime.strptime(value, "%Y-%m-%d %H:%M")
                except ValueError:
                    try:
                        return datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        return None
        return None

    def _build_order_gantt_fragment(self, order_gantt: List[Dict]) -> str:
        """生成订单甘特图HTML片段（不含完整html/head/body包装）"""
        if not order_gantt:
            return '<div style="text-align:center; padding:40px; color:#94a3b8;">暂无订单数据</div>'

        all_starts, all_ends = [], []
        for order in order_gantt:
            for p in order.get("processes", []):
                s = self._to_datetime(p.get("start_time", p.get("start", "")))
                e = self._to_datetime(p.get("end_time", p.get("end", "")))
                if s: all_starts.append(s)
                if e: all_ends.append(e)
            for sf in order.get("semi_finished", []):
                for p in sf.get("processes", []):
                    s = self._to_datetime(p.get("start_time", p.get("start", "")))
                    e = self._to_datetime(p.get("end_time", p.get("end", "")))
                    if s: all_starts.append(s)
                    if e: all_ends.append(e)

        if not all_starts or not all_ends:
            return '<div style="text-align:center; padding:40px; color:#94a3b8;">暂无有效时间数据</div>'

        min_dt, max_dt = min(all_starts), max(all_ends)
        if min_dt == max_dt:
            max_dt = min_dt + timedelta(hours=1)
        total_span = max(1, (max_dt - min_dt).total_seconds())

        def fmt(dt):
            return dt.strftime("%m-%d %H:%M") if dt else "-"

        def pct(start, end):
            if not start or not end:
                return 0, 0
            left = max(0, (start - min_dt).total_seconds()) / total_span * 100
            width = max(1, (end - start).total_seconds()) / total_span * 100
            return left, width

        # 时间刻度
        tick_count = min(12, max(4, int(total_span / 3600)))
        time_ticks = []
        for i in range(tick_count + 1):
            ratio = i / tick_count
            tick_dt = min_dt + timedelta(seconds=total_span * ratio)
            time_ticks.append(f'<div class="og-tick" style="left:{ratio*100:.1f}%">{fmt(tick_dt)}</div>')

        order_rows = []
        for order in order_gantt:
            status_text = "延期" if order.get("is_delayed") else "正常"
            status_color = "#ef4444" if order.get("is_delayed") else "#10b981"
            processes = order.get("processes", [])
            semi_finished = order.get("semi_finished", [])
            color = self.colors[len(order_rows) % len(self.colors)]

            # 订单头
            order_rows.append(
                f'<div class="og-order">'
                f'<div class="og-order-header">'
                f'<span class="og-order-id">{order["order_id"]}</span>'
                f'<span class="og-product">{order.get("product", "")} × {order.get("quantity", 0)}</span>'
                f'<span class="og-status" style="color:{status_color}">{status_text}</span>'
                f'</div>'
            )

            # 工序时间线
            order_rows.append('<div class="og-order-row">')
            order_rows.append('<div style="width:100px; font-size:12px; color:#64748b; flex-shrink:0;">工序</div>')
            order_rows.append('<div class="og-timeline">')
            for p in processes:
                s = self._to_datetime(p.get("start_time", p.get("start", "")))
                e = self._to_datetime(p.get("end_time", p.get("end", "")))
                if not s or not e:
                    continue
                left, width = pct(s, e)
                pname = p.get("process_name", p.get("process", ""))
                order_rows.append(
                    f'<div class="og-bar" style="left:{left:.1f}%; width:{width:.1f}%; background:{color};" title="{pname} {fmt(s)}~{fmt(e)}">'
                    f'<span class="og-bar-label">{pname}</span></div>'
                )
            order_rows.append('</div></div>')

            # 半成品
            for sf in semi_finished:
                sf_color = "#94a3b8"
                order_rows.append(
                    f'<div class="og-semi-row">'
                    f'<div class="og-semi-label">{sf.get("product", "")} × {sf.get("quantity", 0)}</div>'
                    f'<div class="og-timeline">'
                )
                for p in sf.get("processes", []):
                    s = self._to_datetime(p.get("start_time", p.get("start", "")))
                    e = self._to_datetime(p.get("end_time", p.get("end", "")))
                    if not s or not e:
                        continue
                    left, width = pct(s, e)
                    pname = p.get("process_name", p.get("process", ""))
                    order_rows.append(
                        f'<div class="og-bar semi" style="left:{left:.1f}%; width:{width:.1f}%; background:{sf_color};" title="{pname} {fmt(s)}~{fmt(e)}">'
                        f'<span class="og-bar-label">{pname}</span></div>'
                    )
                order_rows.append('</div></div>')

            order_rows.append('</div>')

        return (
            f'<div class="og-container">'
            f'<div class="og-header">'
            f'<h3>订单甘特图</h3>'
            f'<div class="og-legend">'
            f'<div class="og-legend-item"><div class="og-legend-dot" style="background:#10b981"></div>正常</div>'
            f'<div class="og-legend-item"><div class="og-legend-dot" style="background:#ef4444"></div>延期</div>'
            f'<div class="og-legend-item"><div class="og-legend-dot" style="background:#94a3b8"></div>半成品</div>'
            f'</div></div>'
            f'<div class="og-timeline-wrap">'
            f'<div class="og-ticks">{"".join(time_ticks)}</div>'
            f'{"".join(order_rows)}'
            f'</div></div>'
        )

    def _build_detail_rows(self, schedule: List[Dict]) -> List[str]:
        """生成排产明细表行"""
        rows = []
        for item in schedule:
            status = item.get("status", "ok")
            mat_status = item.get("material_status", "ok")
            status_class = "status-shortage" if mat_status == "shortage" or status == "material_shortage" else "status-ok"
            status_text = "物料不足" if mat_status == "shortage" or status == "material_shortage" else "正常"
            start_str = "-"
            end_str = "-"
            try:
                start_str = datetime.fromisoformat(item["start_time"]).strftime("%m-%d %H:%M")
            except Exception:
                pass
            try:
                end_str = datetime.fromisoformat(item["end_time"]).strftime("%m-%d %H:%M")
            except Exception:
                pass
            seq = item.get("sequence", 0)
            seq_str = str(seq) if seq else "-"
            rows.append(
                f'<tr>'
                f'<td>{item.get("order_id", "")}</td>'
                f'<td>{item.get("product", "")}</td>'
                f'<td>{item.get("quantity", 0)}</td>'
                f'<td>{seq_str}</td>'
                f'<td>{item.get("process_name", item.get("process", ""))}</td>'
                f'<td>{item.get("machine_id", item.get("machine", ""))}</td>'
                f'<td>{start_str}</td>'
                f'<td>{end_str}</td>'
                f'<td><span class="status-badge {status_class}">{status_text}</span></td>'
                f'<td><div class="gantt-bar {self._get_process_class(item.get("process_name", item.get("process", "")))}">{item.get("order_id", "")}<div class="tooltip">{item.get("product", "")} {item.get("quantity", 0)}件</div></div></td>'
                f'</tr>'
            )
        return rows

    def _get_process_class(self, process_name: str) -> str:
        """根据工序名返回CSS类"""
        name = str(process_name).lower()
        if "cnc" in name or "加工" in name:
            return "cnc"
        elif "装配" in name or "组装" in name:
            return "assembly"
        elif "包装" in name or "打包" in name:
            return "pack"
        return "default"

    def _build_html(self, data: Dict, title: str) -> str:
        """构建完整的HTML页面（Fallback inline mode）"""
        tasks_json = json.dumps(data["tasks"], ensure_ascii=False, default=str)
        machines_json = json.dumps(data["machines"], ensure_ascii=False, default=str)
        min_time = data["min_time"]
        max_time = data["max_time"]

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://d3js.org/d3.v7.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f1f5f9;
            padding: 20px;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 24px;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .header h1 {{
            font-size: 20px;
            color: #1e293b;
            font-weight: 600;
        }}
        .legend {{
            display: flex;
            gap: 16px;
            font-size: 12px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 6px;
            color: #64748b;
        }}
        .legend-color {{
            width: 12px;
            height: 12px;
            border-radius: 3px;
        }}
        .gantt-wrapper {{
            overflow-x: auto;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
        }}
        .tooltip {{
            position: absolute;
            padding: 12px;
            background: rgba(15, 23, 42, 0.95);
            color: white;
            border-radius: 8px;
            font-size: 12px;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.2s;
            z-index: 1000;
            max-width: 280px;
            line-height: 1.6;
        }}
        .tooltip-row {{
            display: flex;
            justify-content: space-between;
            gap: 12px;
        }}
        .tooltip-label {{
            color: #94a3b8;
        }}
        .tooltip-value {{
            font-weight: 500;
        }}
        .axis text {{
            font-size: 11px;
            fill: #64748b;
        }}
        .axis path, .axis line {{
            stroke: #e2e8f0;
        }}
        .grid line {{
            stroke: #f1f5f9;
            stroke-dasharray: 2,2;
        }}
        .task-bar {{
            cursor: pointer;
            transition: opacity 0.2s;
        }}
        .task-bar:hover {{
            opacity: 0.8;
        }}
        .machine-label {{
            font-size: 12px;
            fill: #475569;
            font-weight: 500;
        }}
        .summary {{
            margin-top: 20px;
            padding: 16px;
            background: #f8fafc;
            border-radius: 8px;
            font-size: 13px;
            color: #475569;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
            margin-top: 12px;
        }}
        .summary-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            background: white;
            border-radius: 6px;
            border: 1px solid #e2e8f0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{title}</h1>
            <div class="legend">
                <div class="legend-item">
                    <div class="legend-color" style="background:#10b981"></div>
                    <span>正常</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background:#ef4444"></div>
                    <span>延期</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background:#f59e0b"></div>
                    <span>缺料</span>
                </div>
            </div>
        </div>
        <div class="gantt-wrapper" id="gantt"></div>
        <div class="summary" id="summary"></div>
    </div>
    <div class="tooltip" id="tooltip"></div>

    <script>
        const tasks = {tasks_json};
        const machines = {machines_json};
        const minTime = new Date("{min_time}");
        const maxTime = new Date("{max_time}");

        // 设置尺寸
        const margin = {{top: 40, right: 40, bottom: 40, left: 120}};
        const rowHeight = 48;
        const height = machines.length * rowHeight + margin.top + margin.bottom;
        const width = Math.max(1000, document.getElementById('gantt').clientWidth - 40);

        // 创建SVG
        const svg = d3.select("#gantt")
            .append("svg")
            .attr("width", width + margin.left + margin.right)
            .attr("height", height);

        const g = svg.append("g")
            .attr("transform", `translate(${{margin.left}},${{margin.top}})`);

        // 时间比例尺
        const xScale = d3.scaleTime()
            .domain([minTime, maxTime])
            .range([0, width]);

        // Y轴比例尺（设备）
        const yScale = d3.scaleBand()
            .domain(machines)
            .range([0, machines.length * rowHeight])
            .padding(0.2);

        // 添加网格线
        const xAxis = d3.axisTop(xScale)
            .ticks(d3.timeHour.every(12))
            .tickFormat(d3.timeFormat("%m-%d %H:%M"));

        g.append("g")
            .attr("class", "axis")
            .call(xAxis)
            .selectAll("text")
            .attr("transform", "rotate(-30)")
            .style("text-anchor", "start");

        // Y轴
        const yAxis = d3.axisLeft(yScale);
        g.append("g")
            .attr("class", "axis")
            .call(yAxis)
            .selectAll("text")
            .attr("class", "machine-label");

        // 网格线
        g.append("g")
            .attr("class", "grid")
            .call(d3.axisTop(xScale)
                .ticks(d3.timeHour.every(6))
                .tickSize(-machines.length * rowHeight)
                .tickFormat("")
            );

        // 绘制任务条
        const tooltip = d3.select("#tooltip");

        g.selectAll(".task-bar")
            .data(tasks)
            .enter()
            .append("rect")
            .attr("class", "task-bar")
            .attr("x", d => xScale(new Date(d.start)))
            .attr("y", d => yScale(d.machine))
            .attr("width", d => Math.max(4, xScale(new Date(d.end)) - xScale(new Date(d.start))))
            .attr("height", yScale.bandwidth())
            .attr("rx", 4)
            .attr("fill", d => d.color)
            .on("mouseover", function(event, d) {{
                const start = new Date(d.start);
                const end = new Date(d.end);
                const duration = ((end - start) / 3600000).toFixed(1);

                tooltip.style("opacity", 1)
                    .html(`
                        <div class="tooltip-row"><span class="tooltip-label">工单</span><span class="tooltip-value">${{d.order_id}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">产品</span><span class="tooltip-value">${{d.product}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">工序</span><span class="tooltip-value">${{d.process}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">设备</span><span class="tooltip-value">${{d.machine}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">数量</span><span class="tooltip-value">${{d.quantity}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">开始</span><span class="tooltip-value">${{start.toLocaleString('zh-CN')}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">结束</span><span class="tooltip-value">${{end.toLocaleString('zh-CN')}}</span></div>
                        <div class="tooltip-row"><span class="tooltip-label">时长</span><span class="tooltip-value">${{duration}}h</span></div>
                        ${{d.status === 'delayed' ? `<div class="tooltip-row"><span class="tooltip-label">延期</span><span class="tooltip-value" style="color:#ef4444">${{d.delay_hours.toFixed(1)}}h</span></div>` : ''}}
                    `)
                    .style("left", (event.pageX + 10) + "px")
                    .style("top", (event.pageY - 10) + "px");
            }})
            .on("mousemove", function(event) {{
                tooltip.style("left", (event.pageX + 10) + "px")
                    .style("top", (event.pageY - 10) + "px");
            }})
            .on("mouseout", function() {{
                tooltip.style("opacity", 0);
            }});

        // 添加任务标签（如果条够宽）
        g.selectAll(".task-label")
            .data(tasks)
            .enter()
            .append("text")
            .attr("class", "task-label")
            .attr("x", d => xScale(new Date(d.start)) + 4)
            .attr("y", d => yScale(d.machine) + yScale.bandwidth() / 2)
            .attr("dy", "0.35em")
            .attr("font-size", "10px")
            .attr("fill", "white")
            .text(d => {{
                const width = xScale(new Date(d.end)) - xScale(new Date(d.start));
                return width > 60 ? d.order_id : '';
            }});

        // 汇总统计
        const stats = {{
            total: tasks.length,
            ok: tasks.filter(t => t.status === 'ok').length,
            delayed: tasks.filter(t => t.status === 'delayed').length,
            shortage: tasks.filter(t => t.status === 'material_shortage').length
        }};

        document.getElementById('summary').innerHTML = `
            <div style="font-weight:600;margin-bottom:8px;">排产统计</div>
            <div class="summary-grid">
                <div class="summary-item">
                    <span>总任务数</span>
                    <strong>${{stats.total}}</strong>
                </div>
                <div class="summary-item">
                    <span style="color:#10b981">正常</span>
                    <strong style="color:#10b981">${{stats.ok}}</strong>
                </div>
                <div class="summary-item">
                    <span style="color:#ef4444">延期</span>
                    <strong style="color:#ef4444">${{stats.delayed}}</strong>
                </div>
                <div class="summary-item">
                    <span style="color:#f59e0b">缺料</span>
                    <strong style="color:#f59e0b">${{stats.shortage}}</strong>
                </div>
            </div>
        `;
    </script>
</body>
</html>"""
        return html

    def generate_summary_card(self, summary: Dict) -> str:
        """生成排产摘要卡片HTML"""
        on_time = summary.get("on_time", 0)
        delayed = summary.get("delayed", 0)
        material = summary.get("material_shortage", 0)
        total = summary.get("total_orders", 0)

        return f"""
<div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 16px 0;">
    <div style="background: white; border-radius: 12px; padding: 20px; border: 1px solid #e2e8f0;">
        <h3 style="margin: 0 0 16px 0; font-size: 16px; color: #1e293b;">排产结果摘要</h3>
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px;">
            <div style="text-align: center; padding: 12px; background: #f8fafc; border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: #3b82f6;">{total}</div>
                <div style="font-size: 12px; color: #64748b; margin-top: 4px;">总工单</div>
            </div>
            <div style="text-align: center; padding: 12px; background: #f0fdf4; border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: #10b981;">{on_time}</div>
                <div style="font-size: 12px; color: #64748b; margin-top: 4px;">正常</div>
            </div>
            <div style="text-align: center; padding: 12px; background: #fef2f2; border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: #ef4444;">{delayed}</div>
                <div style="font-size: 12px; color: #64748b; margin-top: 4px;">延期</div>
            </div>
            <div style="text-align: center; padding: 12px; background: #fffbeb; border-radius: 8px;">
                <div style="font-size: 24px; font-weight: 700; color: #f59e0b;">{material}</div>
                <div style="font-size: 12px; color: #64748b; margin-top: 4px;">缺料</div>
            </div>
        </div>
    </div>
</div>
"""

    def generate_order_gantt(self, order_gantt: List[Dict], title: str = "订单甘特图") -> str:
        """
        生成订单甘特图HTML（按订单分组展示工序时间线）
        
        Args:
            order_gantt: 订单甘特图数据
            title: 图表标题
        """
        if not order_gantt:
            return self._empty_gantt(title)

        # 收集所有时间点
        all_times = []
        for order in order_gantt:
            for p in order.get("processes", []):
                if p.get("start_time"):
                    all_times.append(datetime.fromisoformat(p["start_time"]))
                if p.get("end_time"):
                    all_times.append(datetime.fromisoformat(p["end_time"]))

        if not all_times:
            return self._empty_gantt(title)

        min_time = min(all_times)
        max_time = max(all_times)

        # 为每个订单生成一行
        order_rows = []
        colors = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4"]

        for idx, order in enumerate(order_gantt):
            color = colors[idx % len(colors)]
            processes = order.get("processes", [])
            semi_finished = order.get("semi_finished", [])

            # 根订单工序
            process_bars = []
            for p in processes:
                start = datetime.fromisoformat(p["start_time"]) if p.get("start_time") else None
                end = datetime.fromisoformat(p["end_time"]) if p.get("end_time") else None
                if start and end:
                    left_pct = ((start - min_time).total_seconds() / (max_time - min_time).total_seconds()) * 100
                    width_pct = ((end - start).total_seconds() / (max_time - min_time).total_seconds()) * 100
                    process_bars.append(f'''
                        <div class="og-bar" style="left:{left_pct}%;width:{width_pct}%;background:{color}"
                             title="{p['process_name']}&#10;{start.strftime('%m-%d %H:%M')} ~ {end.strftime('%m-%d %H:%M')}">
                            <span class="og-bar-label">{p['process_name']}</span>
                        </div>
                    ''')

            # 半成品工序（缩进显示）
            semi_rows = []
            for sf in semi_finished:
                sf_bars = []
                sf_color = colors[(idx + 1) % len(colors)]
                for p in sf.get("processes", []):
                    start = datetime.fromisoformat(p["start_time"]) if p.get("start_time") else None
                    end = datetime.fromisoformat(p["end_time"]) if p.get("end_time") else None
                    if start and end:
                        left_pct = ((start - min_time).total_seconds() / (max_time - min_time).total_seconds()) * 100
                        width_pct = ((end - start).total_seconds() / (max_time - min_time).total_seconds()) * 100
                        sf_bars.append(f'''
                            <div class="og-bar og-bar-semi" style="left:{left_pct}%;width:{width_pct}%;background:{sf_color}"
                                 title="{sf['product']} - {p['process_name']}&#10;{start.strftime('%m-%d %H:%M')} ~ {end.strftime('%m-%d %H:%M')}">
                                <span class="og-bar-label">{p['process_name']}</span>
                            </div>
                        ''')

                if sf_bars:
                    semi_rows.append(f'''
                        <div class="og-semi-row">
                            <div class="og-semi-label">{sf['product']} x{sf['quantity']}</div>
                            <div class="og-timeline">{''.join(sf_bars)}</div>
                        </div>
                    ''')

            status_badge = "延期" if order.get("is_delayed") else "正常"
            status_color = "#ef4444" if order.get("is_delayed") else "#10b981"

            order_rows.append(f'''
                <div class="og-order">
                    <div class="og-order-header">
                        <span class="og-order-id">{order['order_id']}</span>
                        <span class="og-product">{order['product']} x{order['quantity']}</span>
                        <span class="og-status" style="color:{status_color}">{status_badge}</span>
                    </div>
                    <div class="og-order-row">
                        <div class="og-timeline">{''.join(process_bars)}</div>
                    </div>
                    {''.join(semi_rows)}
                </div>
            ''')

        # 生成时间刻度
        time_ticks = []
        duration = (max_time - min_time).total_seconds()
        tick_count = 10
        for i in range(tick_count + 1):
            tick_time = min_time + timedelta(seconds=duration * i / tick_count)
            left_pct = (i / tick_count) * 100
            time_ticks.append(f'<div class="og-tick" style="left:{left_pct}%">{tick_time.strftime("%m-%d %H:%M")}</div>')

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f8fafc; padding:20px; }}
    .og-container {{ max-width:1400px; margin:0 auto; background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
    .og-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid #e2e8f0; }}
    .og-header h1 {{ font-size:18px; color:#1e293b; }}
    .og-legend {{ display:flex; gap:16px; font-size:12px; color:#64748b; }}
    .og-legend-item {{ display:flex; align-items:center; gap:6px; }}
    .og-legend-dot {{ width:10px; height:10px; border-radius:2px; }}
    .og-timeline-wrap {{ position:relative; margin-bottom:8px; }}
    .og-ticks {{ position:relative; height:24px; border-bottom:1px solid #e2e8f0; margin-bottom:8px; margin-left:120px; }}
    .og-tick {{ position:absolute; font-size:10px; color:#94a3b8; transform:translateX(-50%); }}
    .og-order {{ margin-bottom:16px; border:1px solid #e2e8f0; border-radius:8px; overflow:hidden; }}
    .og-order-header {{ display:flex; align-items:center; gap:12px; padding:8px 12px; background:#f8fafc; font-size:13px; }}
    .og-order-id {{ font-weight:600; color:#1e293b; }}
    .og-product {{ color:#475569; }}
    .og-status {{ font-size:11px; font-weight:500; margin-left:auto; }}
    .og-order-row, .og-semi-row {{ display:flex; align-items:center; height:36px; padding:0 12px; }}
    .og-semi-row {{ background:#fafafa; border-top:1px dashed #e2e8f0; padding-left:32px; }}
    .og-semi-label {{ width:100px; font-size:11px; color:#64748b; flex-shrink:0; }}
    .og-timeline {{ position:relative; flex:1; height:28px; margin-left:8px; }}
    .og-bar {{ position:absolute; top:4px; height:20px; border-radius:4px; color:white; font-size:10px; display:flex; align-items:center; padding:0 6px; overflow:hidden; white-space:nowrap; cursor:pointer; transition:opacity 0.2s; }}
    .og-bar:hover {{ opacity:0.8; }}
    .og-bar-semi {{ opacity:0.7; height:16px; top:6px; }}
    .og-bar-label {{ overflow:hidden; text-overflow:ellipsis; }}
</style>
</head>
<body>
<div class="og-container">
    <div class="og-header">
        <h1>{title}</h1>
        <div class="og-legend">
            <div class="og-legend-item"><div class="og-legend-dot" style="background:#10b981"></div>正常</div>
            <div class="og-legend-item"><div class="og-legend-dot" style="background:#ef4444"></div>延期</div>
            <div class="og-legend-item"><div class="og-legend-dot" style="background:#94a3b8"></div>半成品</div>
        </div>
    </div>
    <div class="og-timeline-wrap">
        <div class="og-ticks">{''.join(time_ticks)}</div>
        {''.join(order_rows)}
    </div>
</div>
</body>
</html>"""

    def generate_workcenter_load_chart(self, workcenter_load: Dict, title: str = "工作中心负荷") -> str:
        """
        生成工作中心负荷图HTML
        
        Args:
            workcenter_load: 工作中心负荷数据 {machine_id: {date: {load_hours, capacity_hours, load_percent, status}}}
            title: 图表标题
        """
        if not workcenter_load:
            return self._empty_gantt(title)

        # 收集所有日期
        all_dates = set()
        for machine_data in workcenter_load.values():
            all_dates.update(machine_data.keys())
        sorted_dates = sorted(all_dates)

        # 生成表格行
        rows = []
        for machine_id, dates in sorted(workcenter_load.items()):
            cells = []
            for date in sorted_dates:
                data = dates.get(date, {})
                load_pct = data.get("load_percent", 0)
                load_hours = data.get("load_hours", 0)
                status = data.get("status", "normal")

                if status == "overload":
                    bg = "#fee2e2"; color = "#dc2626"
                elif status == "warning":
                    bg = "#fef3c7"; color = "#d97706"
                else:
                    bg = "#dcfce7"; color = "#16a34a"

                bar_width = min(100, load_pct)
                cells.append(f'''
                    <td style="padding:4px; min-width:80px;">
                        <div style="background:{bg}; border-radius:4px; padding:6px; text-align:center;">
                            <div style="font-size:11px; font-weight:600; color:{color};">{load_pct}%</div>
                            <div style="font-size:9px; color:#64748b;">{load_hours}h</div>
                            <div style="height:3px; background:#e2e8f0; border-radius:2px; margin-top:4px;">
                                <div style="width:{bar_width}%; height:100%; background:{color}; border-radius:2px;"></div>
                            </div>
                        </div>
                    </td>
                ''')

            rows.append(f'''
                <tr style="border-bottom:1px solid #e2e8f0;">
                    <td style="padding:8px 12px; font-weight:500; color:#1e293b; font-size:12px; white-space:nowrap;">{machine_id}</td>
                    {''.join(cells)}
                </tr>
            ''')

        # 表头
        header_cells = ''.join(f'<th style="padding:8px; font-size:11px; color:#64748b; font-weight:500; text-align:center;">{d}</th>' for d in sorted_dates)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f8fafc; padding:20px; }}
    .wl-container {{ max-width:1400px; margin:0 auto; background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
    .wl-header {{ margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid #e2e8f0; }}
    .wl-header h1 {{ font-size:18px; color:#1e293b; }}
    .wl-table {{ width:100%; border-collapse:collapse; }}
    .wl-table th {{ background:#f8fafc; padding:10px; font-size:12px; color:#475569; font-weight:500; text-align:left; border-bottom:2px solid #e2e8f0; }}
    .wl-legend {{ display:flex; gap:16px; margin-top:12px; font-size:12px; }}
    .wl-legend-item {{ display:flex; align-items:center; gap:6px; color:#64748b; }}
    .wl-dot {{ width:8px; height:8px; border-radius:50%; }}
</style>
</head>
<body>
<div class="wl-container">
    <div class="wl-header">
        <h1>{title}</h1>
        <div class="wl-legend">
            <div class="wl-legend-item"><div class="wl-dot" style="background:#16a34a"></div>正常 (&lt;80%)</div>
            <div class="wl-legend-item"><div class="wl-dot" style="background:#d97706"></div>预警 (80-100%)</div>
            <div class="wl-legend-item"><div class="wl-dot" style="background:#dc2626"></div>超负荷 (&gt;100%)</div>
        </div>
    </div>
    <div style="overflow-x:auto;">
        <table class="wl-table">
            <thead>
                <tr>
                    <th>工作中心</th>
                    {header_cells}
                </tr>
            </thead>
            <tbody>
                {''.join(rows)}
            </tbody>
        </table>
    </div>
</div>
</body>
</html>"""

    def generate_bom_tree_view(self, bom_tree: List[Dict], title: str = "BOM层级追溯") -> str:
        """
        生成BOM层级追溯视图HTML
        
        Args:
            bom_tree: BOM树数据
            title: 图表标题
        """
        if not bom_tree:
            return self._empty_gantt(title)

        def render_node(node: Dict, depth: int = 0) -> str:
            """递归渲染节点"""
            indent = depth * 24
            level_colors = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444"]
            color = level_colors[min(depth, len(level_colors) - 1)]

            # 物料信息
            material_info = node.get("material_info", {})
            mat_badge = ""
            if material_info:
                gap = material_info.get("gap", 0)
                if gap > 0:
                    mat_badge = f'<span style="background:#fee2e2; color:#dc2626; padding:2px 6px; border-radius:4px; font-size:10px; margin-left:8px;">缺料 {gap}</span>'
                else:
                    mat_badge = f'<span style="background:#dcfce7; color:#16a34a; padding:2px 6px; border-radius:4px; font-size:10px; margin-left:8px;">齐套</span>'

            # 排程信息
            schedule_info = ""
            start = node.get("schedule_start")
            end = node.get("schedule_end")
            if start and end:
                schedule_info = f'<span style="color:#64748b; font-size:11px; margin-left:8px;">{start[5:16]} ~ {end[5:16]}</span>'

            # 工序信息
            processes = node.get("processes", [])
            process_tags = ''.join([
                f'<span style="background:#f1f5f9; color:#475569; padding:2px 6px; border-radius:4px; font-size:10px; margin-right:4px;">{p["process_name"]}</span>'
                for p in processes
            ])

            children_html = ''.join([
                render_node(child, depth + 1)
                for child in node.get("children", [])
            ])

            level_labels = ["成品", "半成品", "原材料"]
            level_label = level_labels[min(depth, 2)] if depth < 3 else "原材料"

            return f'''
                <div class="bom-node" style="margin-left:{indent}px;">
                    <div class="bom-node-content">
                        <div class="bom-level-indicator" style="background:{color}"></div>
                        <div class="bom-info">
                            <div class="bom-title">
                                <span class="bom-product">{node["product"]}</span>
                                <span class="bom-quantity">x{node["quantity"]}</span>
                                <span class="bom-level-label" style="background:{color}20; color:{color};">{level_label}</span>
                                {mat_badge}
                            </div>
                            <div class="bom-meta">
                                {schedule_info}
                                <span style="color:#94a3b8; font-size:11px; margin-left:8px;">{node.get("order_id", "")}</span>
                            </div>
                            <div class="bom-processes">{process_tags}</div>
                        </div>
                    </div>
                    {children_html}
                </div>
            '''

        tree_html = ''.join([render_node(tree) for tree in bom_tree])

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f8fafc; padding:20px; }}
    .bom-container {{ max-width:900px; margin:0 auto; background:white; border-radius:12px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
    .bom-header {{ margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid #e2e8f0; }}
    .bom-header h1 {{ font-size:18px; color:#1e293b; }}
    .bom-node {{ margin-bottom:4px; }}
    .bom-node-content {{ display:flex; align-items:flex-start; padding:10px 12px; border:1px solid #e2e8f0; border-radius:8px; background:white; }}
    .bom-level-indicator {{ width:4px; height:40px; border-radius:2px; margin-right:10px; flex-shrink:0; }}
    .bom-info {{ flex:1; }}
    .bom-title {{ display:flex; align-items:center; flex-wrap:wrap; gap:4px; margin-bottom:4px; }}
    .bom-product {{ font-weight:600; color:#1e293b; font-size:13px; }}
    .bom-quantity {{ color:#64748b; font-size:12px; }}
    .bom-level-label {{ font-size:10px; padding:2px 6px; border-radius:4px; font-weight:500; }}
    .bom-meta {{ margin-bottom:4px; }}
    .bom-processes {{ display:flex; flex-wrap:wrap; gap:4px; }}
</style>
</head>
<body>
<div class="bom-container">
    <div class="bom-header">
        <h1>{title}</h1>
    </div>
    {tree_html}
</div>
</body>
</html>"""
