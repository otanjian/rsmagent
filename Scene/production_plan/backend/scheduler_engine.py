"""
增强型生产排产引擎 (Advanced Production Scheduling Engine)
基于约束满足 + 启发式搜索的有限能力排程
支持：多工序路线、设备独占约束、换线时间、维护窗口、物料齐套、人力班组约束
"""

import json
import copy
import random
import math
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from common.log import logger


@dataclass
class ProcessStep:
    """工序步骤"""
    process_name: str
    time_per_unit: float  # 单位耗时（分钟）
    machine_id: Optional[str] = None  # 指定设备/工作中心
    setup_time: float = 30.0  # 换线时间（分钟）
    sequence: int = 0  # 行号/工序序号
    mold_id: Optional[str] = None  # 模具编号


@dataclass
class WorkOrder:
    """工单"""
    order_id: str
    product: str
    quantity: int
    due_date: datetime
    priority: int = 3  # 1-5, 1最高
    process_route: List[ProcessStep] = field(default_factory=list)
    bom: Dict[str, float] = field(default_factory=dict)  # {物料: 单件用量}
    team_required: Optional[str] = None  # 班组需求
    customer: Optional[str] = None
    parent_id: Optional[str] = None  # 父级生产订单号（v3模板）
    sales_order_no: Optional[str] = None  # 销售单号
    batch_no: Optional[str] = None  # 批次
    batch_time: Optional[str] = None  # 批次时间
    product_code: Optional[str] = None  # 料号


@dataclass
class Machine:
    """设备/产线/工作中心"""
    machine_id: str
    machine_name: str
    capacity_per_day: int
    available_start: str = "08:00"  # 可用时段开始
    available_end: str = "20:00"  # 可用时段结束
    setup_time_same: float = 15.0  # 同产品换模时间
    setup_time_diff: float = 60.0  # 不同产品换线时间
    maintenance: List[Dict] = field(default_factory=list)  # [{"start": datetime, "end": datetime}]
    # 新模板增强字段
    setup_time_diff_mold: float = 480.0  # 不同模具换模时间（分钟）= 1工作日=8小时
    setup_time_same_mold_diff_product: float = 120.0  # 同模具不同产品换模时间（分钟）= 2小时
    daily_capacity: Optional[Dict[str, float]] = None  # {日期: 可用小时数} 新模板工作中心日历


@dataclass
class Material:
    """物料"""
    material_name: str
    stock: float
    on_the_way: float = 0.0
    lead_time: int = 0  # 采购周期（天）
    safety_stock: float = 0.0
    material_code: str = ""  # 料号


@dataclass
class Team:
    """班组/人力"""
    team_name: str
    headcount: int
    shift: str = "day"  # day/night/both
    skills: List[str] = field(default_factory=list)  # 可操作的工序
    work_hours_per_day: float = 8.0


@dataclass
class ScheduleResult:
    """排程结果"""
    order_id: str
    product: str
    process_name: str
    machine_id: str
    start_time: datetime
    end_time: datetime
    quantity: int
    setup_time: float
    status: str  # ok / delayed / material_shortage
    delay_hours: float = 0.0


class MaterialChecker:
    """物料齐套检查器"""

    def __init__(self, materials: List[Material]):
        self.materials = {m.material_name: m for m in materials}

    def check_availability(self, order: WorkOrder) -> Tuple[bool, Dict[str, Any]]:
        """
        检查工单物料齐套情况
        返回: (是否齐套, 详细信息)
        """
        result = {
            "order_id": order.order_id,
            "product": order.product,
            "materials": [],
            "all_ready": True,
            "ready_time": datetime.now(),
            "shortage": []
        }

        for material_name, unit_qty in order.bom.items():
            total_need = unit_qty * order.quantity
            mat = self.materials.get(material_name)

            if not mat:
                result["all_ready"] = False
                result["shortage"].append({
                    "material": material_name,
                    "need": total_need,
                    "available": 0,
                    "gap": total_need,
                    "ready_time": None
                })
                result["materials"].append({
                    "material": material_name,
                    "need": total_need,
                    "stock": 0,
                    "on_the_way": 0,
                    "gap": total_need
                })
                continue

            available = mat.stock + mat.on_the_way - mat.safety_stock
            gap = max(0, total_need - available)

            mat_info = {
                "material": material_name,
                "need": total_need,
                "stock": mat.stock,
                "on_the_way": mat.on_the_way,
                "safety_stock": mat.safety_stock,
                "available": available,
                "gap": gap
            }
            result["materials"].append(mat_info)

            if gap > 0:
                result["all_ready"] = False
                ready_time = datetime.now() + timedelta(days=mat.lead_time)
                result["shortage"].append({
                    "material": material_name,
                    "need": total_need,
                    "available": available,
                    "gap": gap,
                    "lead_time": mat.lead_time,
                    "ready_time": ready_time
                })
                if ready_time > result["ready_time"]:
                    result["ready_time"] = ready_time
            else:
                # 齐套，但考虑在途物料到达时间
                if mat.stock < total_need and mat.on_the_way > 0:
                    # 需要等部分在途物料
                    ready_time = datetime.now() + timedelta(days=mat.lead_time)
                    if ready_time > result["ready_time"]:
                        result["ready_time"] = ready_time

        return result["all_ready"], result

    def get_material_ready_time(self, order: WorkOrder) -> datetime:
        """获取物料齐套时间"""
        _, result = self.check_availability(order)
        return result["ready_time"]


class AdvancedScheduler:
    """
    增强型排产引擎
    基于约束满足 + 启发式搜索（禁忌搜索 + 模拟退火）
    """

    def __init__(self):
        self.machines: Dict[str, Machine] = {}
        self.teams: Dict[str, Team] = {}
        self.material_checker: Optional[MaterialChecker] = None
        self.schedule_mode = "forward"  # forward / backward
        self.objective = "tardiness"  # tardiness / makespan / cost / balanced
        self.constraints = []
        self.iterations = 500
        self.tabu_tenure = 20
        self.initial_temperature = 100.0
        self.cooling_rate = 0.995

    def set_resources(self, machines: List[Machine], teams: List[Team]):
        """设置资源"""
        self.machines = {m.machine_id: m for m in machines}
        self.teams = {t.team_name: t for t in teams}

    def set_materials(self, materials: List[Material]):
        """设置物料"""
        self.material_checker = MaterialChecker(materials)

    def schedule(self, orders: List[WorkOrder]) -> Dict[str, Any]:
        """
        主排程方法

        Returns:
            {
                "schedule": List[ScheduleResult],
                "summary": Dict,
                "alerts": List[Dict],
                "bottleneck": Dict,
                "material_plan": List[Dict]
            }
        """
        if not orders:
            return {"schedule": [], "summary": {}, "alerts": [], "bottleneck": {}, "material_plan": []}

        # 1. 物料齐套检查
        material_results = []
        for order in orders:
            ready, result = self.material_checker.check_availability(order)
            material_results.append((order, ready, result))

        # 2. 生成初始解（优先级 + EDD）
        initial_solution = self._generate_initial_solution(orders)

        # 3. 启发式搜索优化
        best_solution = self._tabu_search(initial_solution)

        # 4. 构建排程结果
        schedule_results = self._build_schedule_results(best_solution)

        # 5. 生成汇总信息
        summary = self._generate_summary(schedule_results, orders)

        # 6. 生成预警信息
        alerts = self._generate_alerts(schedule_results, material_results)

        # 7. 瓶颈分析
        bottleneck = self._analyze_bottleneck(schedule_results)

        # 8. 物料需求计划
        material_plan = self._generate_material_plan(orders, schedule_results)

        return {
            "schedule": [self._result_to_dict(r) for r in schedule_results],
            "summary": summary,
            "alerts": alerts,
            "bottleneck": bottleneck,
            "material_plan": material_plan
        }

    def schedule_with_bom_tree(self, orders: List[WorkOrder],
                                bom_db: Dict[str, List[Any]],
                                process_db: Optional[Dict[str, List[Any]]] = None,
                                transfer_time_hours: float = 5.0) -> Dict[str, Any]:
        """
        基于BOM树的排程方法（融合方案）
        支持：订单树构建、无限/有限产能倒排、齐套检查、多视图输出
        v3模板：使用显式父级字段构建订单树
        v2模板：使用BOM结构自动展开订单树

        Args:
            orders: 工单列表
            bom_db: BOM数据库 {产品名: [BOMItem, ...]}
            process_db: 工序数据库 {产品名: [ProcessStep, ...]}
            transfer_time_hours: 工序间转移时间（小时）

        Returns:
            {
                "schedule": List[Dict],           # 设备甘特图数据
                "order_gantt": List[Dict],        # 订单甘特图数据
                "workcenter_load": Dict,          # 工作中心负荷
                "bom_tree": Dict,                 # BOM层级追溯
                "summary": Dict,
                "alerts": List[Dict],
                "material_kit_result": Dict,      # 齐套检查结果
                "backward_schedule": Dict,        # 倒排运算结果
                "daily_plan": List[Dict]          # 每日计划产量
            }
        """
        from Scene.production_plan.backend.bom_tree import (
            BOMTreeBuilder, BackwardScheduler, MaterialKitChecker,
            BOMItem, ExplicitParentTreeBuilder, WorkTimeHelper
        )

        if not orders:
            return self._empty_bom_result()

        result = {
            "schedule": [],
            "order_gantt": [],
            "workcenter_load": {},
            "bom_tree": {},
            "summary": {},
            "alerts": [],
            "material_kit_result": {},
            "backward_schedule": {},
            "daily_plan": []
        }

        # 构建物料检查器
        materials_dict = {}
        if self.material_checker:
            materials_dict = self.material_checker.materials
        mat_kit_checker = MaterialKitChecker(materials_dict)

        # 判断模板类型：v3模板所有订单都包含parent_id字段（成品为"0"，半成品为父级订单号）
        # 只要存在订单的parent_id不为None，就认为是v3显式父级模式
        has_explicit_parent = any(
            getattr(o, 'parent_id', None) is not None
            for o in orders
        )

        # 构建订单树
        all_trees: List[Any] = []
        if has_explicit_parent:
            # v3模板：使用显式父级构建订单树
            parent_builder = ExplicitParentTreeBuilder(process_db)
            all_trees = parent_builder.build_trees(orders)
        else:
            # v2模板：每个根订单用BOM结构自动展开
            for order in orders:
                builder = BOMTreeBuilder(bom_db, process_db)
                tree = builder.build_order_tree(
                    order.order_id,
                    order.product,
                    order.quantity,
                    order.due_date,
                    order.process_route
                )
                all_trees.append(tree)

        # 为每棵树执行倒排运算
        all_backward_schedules = []
        all_infinite_schedules = []  # 无限产能结果，用于计算理论交期
        all_kit_results = []

        # 共享BackwardScheduler以跨订单检测设备冲突
        backward_finite = BackwardScheduler(self.machines, transfer_time_hours)
        backward_infinite = BackwardScheduler(self.machines, transfer_time_hours)

        for tree in all_trees:
            # 齐套检查（只看原材料）
            kit_result = mat_kit_checker.check_tree_kits(tree)
            all_kit_results.append({
                "order_id": tree.order_id,
                "product": tree.product,
                **kit_result
            })

            # 无限产能倒排（理论时间）
            infinite_result = backward_infinite.schedule_infinite(tree)
            all_infinite_schedules.append({
                "order_id": tree.order_id,
                "tree": tree,
                "result": infinite_result
            })

            # 有限产能倒排（实际排程）
            finite_result = backward_finite.schedule_finite(tree)
            all_backward_schedules.append({
                "order_id": tree.order_id,
                "tree": tree,
                "result": finite_result
            })

            result["backward_schedule"][tree.order_id] = {
                "infinite": infinite_result,
                "finite": finite_result
            }

        # 生成设备甘特图数据（使用有限产能结果）
        result["schedule"] = self._build_schedule_from_backward(
            all_backward_schedules, all_infinite_schedules
        )

        # 生成订单甘特图数据
        result["order_gantt"] = self._build_order_gantt(
            all_backward_schedules, all_infinite_schedules
        )

        # 生成工作中心负荷数据
        result["workcenter_load"] = self._build_workcenter_load(
            result["schedule"], all_backward_schedules
        )

        # 生成BOM层级追溯数据
        result["bom_tree"] = self._build_bom_tree_view(all_backward_schedules, all_kit_results)

        # 生成每日计划产量
        result["daily_plan"] = self._build_daily_plan(all_backward_schedules)

        # 生成汇总信息
        result["summary"] = self._generate_bom_summary(
            all_backward_schedules, all_kit_results, orders
        )

        # 生成预警信息
        result["alerts"] = self._generate_bom_alerts(
            all_backward_schedules, all_kit_results, orders, all_infinite_schedules
        )

        # 齐套结果汇总
        result["material_kit_result"] = {
            "all_ready": all(k.get("all_ready", False) for k in all_kit_results),
            "details": all_kit_results
        }

        return result

    def _empty_bom_result(self) -> Dict[str, Any]:
        """空结果"""
        return {
            "schedule": [],
            "order_gantt": [],
            "workcenter_load": {},
            "bom_tree": {},
            "summary": {},
            "alerts": [],
            "material_kit_result": {},
            "backward_schedule": {},
            "daily_plan": []
        }

    def _build_schedule_from_backward(self, backward_schedules: List[Dict],
                                       infinite_schedules: List[Dict]) -> List[Dict]:
        """从倒排结果构建设备甘特图数据（有限产能 vs 无限产能理论）"""
        # 建立无限产能理论结束时间映射
        infinite_end_map: Dict[str, datetime] = {}
        for inf in infinite_schedules:
            for item in inf["result"].get("schedule", []):
                key = f"{item['order_id']}#{item['process_name']}"
                infinite_end_map[key] = item["end_time"]

        schedule = []
        for bs in backward_schedules:
            for item in bs["result"].get("schedule", []):
                tree = bs["tree"]
                key = f"{item['order_id']}#{item['process_name']}"
                infinite_end = infinite_end_map.get(key)

                # 判断是否逾期（相对于需求交期）
                is_delayed = False
                delay_days = 0.0
                if tree.due_date and item["end_time"] > tree.due_date:
                    is_delayed = True
                    delay_days = (item["end_time"] - tree.due_date).total_seconds() / 86400

                # 与无限产能理论时间比较
                delay_vs_infinite_hours = 0.0
                if infinite_end and item["end_time"] > infinite_end:
                    delay_vs_infinite_hours = (item["end_time"] - infinite_end).total_seconds() / 3600

                status = "ok"
                if is_delayed:
                    status = "delayed"

                schedule.append({
                    "order_id": item["order_id"],
                    "product": item["product"],
                    "process_name": item["process_name"],
                    "machine_id": item.get("machine_id", ""),
                    "start_time": item["start_time"].isoformat(),
                    "end_time": item["end_time"].isoformat(),
                    "quantity": item["quantity"],
                    "status": status,
                    "delay_days": round(delay_days, 2),
                    "delay_vs_infinite_hours": round(delay_vs_infinite_hours, 2),
                    "level": item.get("level", 0),
                    "sequence": item.get("sequence", 0),
                    "mold_id": item.get("mold_id"),
                    "setup_time_minutes": item.get("setup_time_minutes", 0)
                })
        return schedule

    def _build_order_gantt(self, backward_schedules: List[Dict],
                            infinite_schedules: List[Dict]) -> List[Dict]:
        """构建订单甘特图数据（按订单分组，含无限产能理论时间）"""
        # 建立无限产能节点排程映射
        infinite_node_map: Dict[str, Dict] = {}
        for inf in infinite_schedules:
            node_schedules = inf["result"].get("node_schedules", {})
            for nid, ns in node_schedules.items():
                infinite_node_map[nid] = ns

        order_gantt = []
        for bs in backward_schedules:
            tree = bs["tree"]
            node_schedules = bs["result"].get("node_schedules", {})

            # 根订单
            root_schedule = node_schedules.get(tree.order_id, {})
            root_infinite = infinite_node_map.get(tree.order_id, {})
            processes = root_schedule.get("processes", [])

            # 判断是否逾期
            is_delayed = False
            delay_days = 0.0
            actual_end = root_schedule.get("end_time")
            if actual_end and tree.due_date and actual_end > tree.due_date:
                is_delayed = True
                delay_days = (actual_end - tree.due_date).total_seconds() / 86400

            # 无限产能理论结束时间
            infinite_end = root_infinite.get("end_time")
            delay_vs_infinite = 0.0
            if infinite_end and actual_end and actual_end > infinite_end:
                delay_vs_infinite = (actual_end - infinite_end).total_seconds() / 86400

            order_item = {
                "order_id": tree.order_id,
                "product": tree.product,
                "quantity": tree.quantity,
                "due_date": tree.due_date.isoformat() if tree.due_date else None,
                "actual_start": root_schedule.get("start_time", "").isoformat() if root_schedule.get("start_time") else None,
                "actual_end": actual_end.isoformat() if actual_end else None,
                "infinite_end": infinite_end.isoformat() if infinite_end else None,
                "is_delayed": is_delayed,
                "delay_days": round(delay_days, 2),
                "delay_vs_infinite_days": round(delay_vs_infinite, 2),
                "sales_order_no": tree.sales_order_no,
                "batch_no": tree.batch_no,
                "processes": [
                    {
                        "process_name": p["process_name"],
                        "start_time": p["start_time"].isoformat(),
                        "end_time": p["end_time"].isoformat(),
                        "machine_id": p.get("machine_id", ""),
                        "duration_hours": round(p["processing_time_minutes"] / 60, 2),
                        "sequence": p.get("sequence", 0),
                        "mold_id": p.get("mold_id"),
                        "setup_time_minutes": p.get("setup_time_minutes", 0)
                    }
                    for p in processes
                ],
                "semi_finished": []
            }

            # 添加半成品信息
            for child in tree.children:
                child_schedule = node_schedules.get(child.order_id, {})
                child_infinite = infinite_node_map.get(child.order_id, {})
                child_processes = child_schedule.get("processes", [])
                order_item["semi_finished"].append({
                    "order_id": child.order_id,
                    "product": child.product,
                    "quantity": child.quantity,
                    "start_time": child_schedule.get("start_time", "").isoformat() if child_schedule.get("start_time") else None,
                    "end_time": child_schedule.get("end_time", "").isoformat() if child_schedule.get("end_time") else None,
                    "infinite_end": child_infinite.get("end_time", "").isoformat() if child_infinite.get("end_time") else None,
                    "processes": [
                        {
                            "process_name": p["process_name"],
                            "start_time": p["start_time"].isoformat(),
                            "end_time": p["end_time"].isoformat(),
                            "machine_id": p.get("machine_id", ""),
                            "mold_id": p.get("mold_id")
                        }
                        for p in child_processes
                    ]
                })

            order_gantt.append(order_item)
        return order_gantt

    def _build_workcenter_load(self, schedule: List[Dict],
                                backward_schedules: List[Dict]) -> Dict[str, Any]:
        """构建工作中心负荷数据（使用工作中心每天实际容量）"""
        from Scene.production_plan.backend.bom_tree import WorkTimeHelper

        # 按天统计每个工作中心的负荷
        daily_load: Dict[str, Dict[str, Dict]] = {}

        for item in schedule:
            machine_id = item.get("machine_id")
            if not machine_id:
                continue

            start = datetime.fromisoformat(item["start_time"])
            end = datetime.fromisoformat(item["end_time"])
            date_key = start.strftime("%Y-%m-%d")
            duration_hours = (end - start).total_seconds() / 3600

            if machine_id not in daily_load:
                daily_load[machine_id] = {}
            if date_key not in daily_load[machine_id]:
                daily_load[machine_id][date_key] = {
                    "load_hours": 0.0,
                    "normal_hours": 0.0,
                    "overtime_hours": 0.0
                }

            daily_load[machine_id][date_key]["load_hours"] += duration_hours
            # 拆分正常工时和加班工时
            work_detail = WorkTimeHelper.get_work_hours_detail(start, end)
            daily_load[machine_id][date_key]["normal_hours"] += work_detail["normal_hours"]
            daily_load[machine_id][date_key]["overtime_hours"] += work_detail["overtime_hours"]

        # 计算负荷百分比（基于每天实际容量）
        workcenter_load = {}
        for machine_id, days in daily_load.items():
            machine = self.machines.get(machine_id)
            workcenter_load[machine_id] = {}
            for date_key, info in days.items():
                day_date = datetime.strptime(date_key, "%Y-%m-%d")
                daily_capacity = WorkTimeHelper.get_day_capacity_hours(machine, day_date)
                load_hours = info["load_hours"]
                load_percent = min(100, round((load_hours / daily_capacity) * 100, 1)) if daily_capacity > 0 else 0
                workcenter_load[machine_id][date_key] = {
                    "load_hours": round(load_hours, 2),
                    "normal_hours": round(info["normal_hours"], 2),
                    "overtime_hours": round(info["overtime_hours"], 2),
                    "capacity_hours": daily_capacity,
                    "load_percent": load_percent,
                    "status": "normal" if load_percent < 80 else "warning" if load_percent < 100 else "overload"
                }

        return workcenter_load

    def _build_bom_tree_view(self, backward_schedules: List[Dict],
                              kit_results: List[Dict]) -> List[Dict]:
        """构建BOM层级追溯视图数据"""
        bom_trees = []

        for bs in backward_schedules:
            tree = bs["tree"]
            node_schedules = bs["result"].get("node_schedules", {})

            # 找到对应的齐套结果
            kit_result = {}
            for kr in kit_results:
                if kr.get("order_id") == tree.order_id:
                    kit_result = kr
                    break

            def build_node_view(node):
                """递归构建节点视图"""
                node_schedule = node_schedules.get(node.order_id, {})

                view = {
                    "order_id": node.order_id,
                    "product": node.product,
                    "quantity": node.quantity,
                    "level": node.level,
                    "schedule_start": node_schedule.get("start_time", "").isoformat() if node_schedule.get("start_time") else None,
                    "schedule_end": node_schedule.get("end_time", "").isoformat() if node_schedule.get("end_time") else None,
                    "processes": [
                        {
                            "process_name": p["process_name"],
                            "machine_id": p.get("machine_id", ""),
                            "start_time": p["start_time"].isoformat(),
                            "end_time": p["end_time"].isoformat()
                        }
                        for p in node_schedule.get("processes", [])
                    ],
                    "children": [build_node_view(child) for child in node.children]
                }

                # 如果是叶子节点，添加物料齐套信息
                if node.is_leaf():
                    materials_info = kit_result.get("materials", [])
                    for mat in materials_info:
                        if mat.get("material") == node.product:
                            view["material_info"] = mat
                            break

                return view

            bom_trees.append(build_node_view(tree))

        return bom_trees

    def _generate_bom_summary(self, backward_schedules: List[Dict],
                               kit_results: List[Dict],
                               orders: List[WorkOrder]) -> Dict[str, Any]:
        """生成BOM排程汇总信息"""
        total_orders = len(orders)
        delayed = 0
        on_time = 0
        material_shortage = 0

        for bs in backward_schedules:
            tree = bs["tree"]
            node_schedules = bs["result"].get("node_schedules", {})
            root_schedule = node_schedules.get(tree.order_id, {})
            actual_end = root_schedule.get("end_time")

            if actual_end and tree.due_date and actual_end > tree.due_date:
                delayed += 1
            else:
                on_time += 1

        for kr in kit_results:
            if not kr.get("all_ready", True):
                material_shortage += 1

        # 计算总工期
        all_ends = []
        all_starts = []
        for bs in backward_schedules:
            for ns in bs["result"].get("node_schedules", {}).values():
                if ns.get("start_time"):
                    all_starts.append(ns["start_time"])
                if ns.get("end_time"):
                    all_ends.append(ns["end_time"])

        makespan_days = 0
        if all_starts and all_ends:
            makespan = max(all_ends) - min(all_starts)
            makespan_days = makespan.total_seconds() / 86400

        kit_ok = sum(1 for kr in kit_results if kr.get("all_ready", True))
        kit_nok = len(kit_results) - kit_ok

        return {
            "total_orders": total_orders,
            "on_time": on_time,
            "delayed": delayed,
            "material_shortage": material_shortage,
            "on_time_rate": f"{round((on_time / total_orders) * 100)}%" if total_orders > 0 else "0%",
            "makespan_days": round(makespan_days, 2),
            "mode": self.schedule_mode,
            "late_orders": delayed,
            "makespan": f"{round(makespan_days, 1)}天" if makespan_days > 0 else "-",
            "kit_ok_orders": kit_ok,
            "kit_nok_orders": kit_nok
        }

    def _generate_bom_alerts(self, backward_schedules: List[Dict],
                              kit_results: List[Dict],
                              orders: List[WorkOrder],
                              infinite_schedules: List[Dict]) -> List[Dict]:
        """生成BOM排程预警信息"""
        alerts = []

        # 建立无限产能理论结束时间映射
        infinite_end_map: Dict[str, datetime] = {}
        for inf in infinite_schedules:
            node_schedules = inf["result"].get("node_schedules", {})
            for nid, ns in node_schedules.items():
                infinite_end_map[nid] = ns.get("end_time")

        # 延期预警（相对于需求交期）
        for bs in backward_schedules:
            tree = bs["tree"]
            node_schedules = bs["result"].get("node_schedules", {})
            root_schedule = node_schedules.get(tree.order_id, {})
            actual_end = root_schedule.get("end_time")

            if actual_end and tree.due_date and actual_end > tree.due_date:
                delay_days = (actual_end - tree.due_date).total_seconds() / 86400
                alerts.append({
                    "type": "delay",
                    "level": "high" if delay_days > 1 else "medium",
                    "message": f"工单 {tree.order_id} ({tree.product}) 预计延期 {delay_days:.1f} 天",
                    "suggestion": "建议与客户协商延期或安排加班/外协"
                })

            # 与无限产能理论时间对比预警
            infinite_end = infinite_end_map.get(tree.order_id)
            if actual_end and infinite_end and actual_end > infinite_end:
                diff_days = (actual_end - infinite_end).total_seconds() / 86400
                if diff_days > 0.5:
                    alerts.append({
                        "type": "capacity_constraint",
                        "level": "medium",
                        "message": f"工单 {tree.order_id} 有限产能排程比无限产能理论时间晚 {diff_days:.1f} 天",
                        "suggestion": "工作中心负荷紧张，建议评估产能瓶颈"
                    })

        # 物料预警
        for kr in kit_results:
            if not kr.get("all_ready", True):
                for shortage in kr.get("shortages", []):
                    alerts.append({
                        "type": "material",
                        "level": "high",
                        "message": f"工单 {kr.get('order_id')} 缺 {shortage['material']} {shortage['gap']:.1f} 单位",
                        "suggestion": f"建议立即采购，预计 {shortage.get('lead_time', 'N/A')} 天后到货"
                    })

        # 不可行预警（排程早于当前时间）
        for bs in backward_schedules:
            if not bs["result"].get("is_feasible", True):
                alerts.append({
                    "type": "infeasible",
                    "level": "high",
                    "message": f"工单 {bs['order_id']} 倒排结果不可行（开始时间早于当前时间）",
                    "suggestion": "建议调整交期或减少订单量"
                })

        return alerts

    def _build_daily_plan(self, backward_schedules: List[Dict]) -> List[Dict]:
        """
        构建每日计划产量
        按工序订单每天拆分计划产量
        """
        daily_plan = []
        for bs in backward_schedules:
            for item in bs["result"].get("schedule", []):
                start = item["start_time"]
                end = item["end_time"]
                total_qty = item["quantity"]
                total_minutes = item.get("processing_time_minutes", 0)
                if total_minutes <= 0 or total_qty <= 0:
                    continue

                # 按天拆分产量
                current = start
                while current < end:
                    day_end = current.replace(hour=23, minute=59, second=59)
                    if day_end > end:
                        day_end = end

                    day_start = max(current, start)
                    if day_end <= day_start:
                        # 当天无剩余时间，跳到次日避免死循环
                        current = datetime.combine(current.date() + timedelta(days=1), datetime.min.time())
                        continue

                    day_minutes = (day_end - day_start).total_seconds() / 60
                    day_qty = int(round(total_qty * (day_minutes / total_minutes)))
                    if day_qty <= 0:
                        day_qty = 1
                    if day_qty > total_qty:
                        day_qty = total_qty

                    daily_plan.append({
                        "order_id": item["order_id"],
                        "product": item["product"],
                        "process_name": item["process_name"],
                        "machine_id": item.get("machine_id", ""),
                        "date": current.strftime("%Y-%m-%d"),
                        "plan_qty": day_qty,
                        "plan_hours": round(day_minutes / 60, 2)
                    })

                    current = day_end + timedelta(seconds=1)

        return daily_plan

    def _generate_initial_solution(self, orders: List[WorkOrder]) -> List[Tuple[WorkOrder, List[Dict]]]:
        """
        生成初始可行解
        按优先级+EDD排序，然后按顺序安排
        """
        # 排序：优先级升序（1最优先），然后交期升序
        sorted_orders = sorted(orders, key=lambda o: (o.priority, o.due_date))

        solution = []
        machine_schedules: Dict[str, List[Dict]] = {mid: [] for mid in self.machines}

        for order in sorted_orders:
            order_schedule = []
            prev_end_time = datetime.now()

            # 获取物料齐套时间
            if self.material_checker:
                material_ready_time = self.material_checker.get_material_ready_time(order)
                prev_end_time = max(prev_end_time, material_ready_time)

            for step in order.process_route:
                machine_id = step.machine_id
                if not machine_id or machine_id not in self.machines:
                    # 分配到第一个可用设备
                    machine_id = self._find_best_machine(step, machine_schedules, prev_end_time)

                machine = self.machines.get(machine_id)
                if not machine:
                    continue

                # 计算加工时间（分钟）
                processing_time = step.time_per_unit * order.quantity

                # 查找可用时间窗口
                start_time, end_time = self._find_time_window(
                    machine_id, machine, machine_schedules, prev_end_time,
                    processing_time, step.setup_time, order.product
                )

                schedule_item = {
                    "order": order,
                    "step": step,
                    "machine_id": machine_id,
                    "start_time": start_time,
                    "end_time": end_time,
                    "processing_time": processing_time,
                    "setup_time": step.setup_time
                }
                order_schedule.append(schedule_item)
                machine_schedules[machine_id].append(schedule_item)
                prev_end_time = end_time

            solution.append((order, order_schedule))

        return solution

    def _find_best_machine(self, step: ProcessStep, machine_schedules: Dict, earliest_start: datetime) -> str:
        """为工序找到最佳设备"""
        available_machines = []
        for mid, machine in self.machines.items():
            # 检查设备是否能做该工序（简化：所有设备通用，实际可按技能匹配）
            available_machines.append(mid)

        if not available_machines:
            return list(self.machines.keys())[0] if self.machines else ""

        # 选择最早可用的设备
        best_machine = available_machines[0]
        best_start = datetime.max

        for mid in available_machines:
            machine = self.machines[mid]
            start, _ = self._find_time_window(
                mid, machine, machine_schedules, earliest_start,
                step.time_per_unit, step.setup_time, ""
            )
            if start < best_start:
                best_start = start
                best_machine = mid

        return best_machine

    def _find_time_window(self, machine_id: str, machine: Machine,
                          machine_schedules: Dict, earliest_start: datetime,
                          processing_time: float, setup_time: float,
                          product: str) -> Tuple[datetime, datetime]:
        """
        在设备上查找可用时间窗口
        考虑：维护窗口、已有排程、换线时间
        """
        current_time = max(earliest_start, datetime.now())
        current_time = self._round_to_shift_start(current_time, machine)

        max_attempts = 100
        for _ in range(max_attempts):
            # 检查是否在维护窗口内
            if self._is_in_maintenance(machine, current_time):
                current_time = self._skip_maintenance(machine, current_time)
                continue

            # 计算结束时间
            end_time = current_time + timedelta(minutes=processing_time + setup_time)

            # 检查是否与已有排程冲突
            conflict = False
            for item in machine_schedules.get(machine_id, []):
                item_start = item["start_time"]
                item_end = item["end_time"]

                # 检查时间重叠
                if not (end_time <= item_start or current_time >= item_end):
                    # 有冲突，需要换线时间
                    actual_setup = setup_time
                    if item["order"].product == product:
                        actual_setup = machine.setup_time_same
                    else:
                        actual_setup = machine.setup_time_diff

                    # 重新计算
                    current_time = item_end + timedelta(minutes=actual_setup)
                    conflict = True
                    break

            if not conflict:
                # 检查是否跨班次（简化处理）
                if self._crosses_shift(machine, current_time, end_time):
                    current_time = self._next_shift_start(machine, current_time)
                    continue

                return current_time, end_time

        # 找不到合适窗口，强制安排
        end_time = current_time + timedelta(minutes=processing_time + setup_time)
        return current_time, end_time

    def _is_in_maintenance(self, machine: Machine, dt: datetime) -> bool:
        """检查是否在维护窗口"""
        for maint in machine.maintenance:
            if maint["start"] <= dt < maint["end"]:
                return True
        return False

    def _skip_maintenance(self, machine: Machine, dt: datetime) -> datetime:
        """跳过维护窗口"""
        for maint in machine.maintenance:
            if maint["start"] <= dt < maint["end"]:
                return maint["end"]
        return dt

    def _round_to_shift_start(self, dt: datetime, machine: Machine) -> datetime:
        """将时间对齐到班次开始"""
        hour = dt.hour
        start_hour = int(machine.available_start.split(":")[0])
        if hour < start_hour:
            return dt.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        return dt

    def _crosses_shift(self, machine: Machine, start: datetime, end: datetime) -> bool:
        """检查是否跨班次"""
        end_hour = int(machine.available_end.split(":")[0])
        if end.hour > end_hour or (end.hour == end_hour and end.minute > 0):
            return True
        return False

    def _next_shift_start(self, machine: Machine, dt: datetime) -> datetime:
        """获取下一个班次开始时间"""
        next_day = dt + timedelta(days=1)
        start_hour = int(machine.available_start.split(":")[0])
        return next_day.replace(hour=start_hour, minute=0, second=0, microsecond=0)

    def _tabu_search(self, initial_solution: List) -> List:
        """
        禁忌搜索优化
        """
        current_solution = copy.deepcopy(initial_solution)
        best_solution = copy.deepcopy(current_solution)
        best_score = self._evaluate_solution(best_solution)

        tabu_list = []

        for iteration in range(self.iterations):
            # 生成邻域解
            neighbors = self._generate_neighbors(current_solution)

            best_neighbor = None
            best_neighbor_score = float('inf')

            for neighbor in neighbors:
                neighbor_key = self._solution_key(neighbor)
                score = self._evaluate_solution(neighbor)

                if neighbor_key in tabu_list and score >= best_score:
                    continue

                if score < best_neighbor_score:
                    best_neighbor = neighbor
                    best_neighbor_score = score

            if best_neighbor is None:
                break

            current_solution = best_neighbor
            current_score = best_neighbor_score

            if current_score < best_score:
                best_solution = copy.deepcopy(current_solution)
                best_score = current_score

            # 更新禁忌表
            tabu_list.append(self._solution_key(current_solution))
            if len(tabu_list) > self.tabu_tenure:
                tabu_list.pop(0)

        return best_solution

    def _generate_neighbors(self, solution: List) -> List:
        """生成邻域解（交换两个工单顺序）"""
        neighbors = []
        n = len(solution)

        if n < 2:
            return neighbors

        # 随机交换几个邻居
        for _ in range(min(10, n * (n - 1) // 2)):
            i, j = random.sample(range(n), 2)
            neighbor = copy.deepcopy(solution)
            neighbor[i], neighbor[j] = neighbor[j], neighbor[i]

            # 重新计算排程
            neighbor = self._reschedule_solution(neighbor)
            neighbors.append(neighbor)

        return neighbors

    def _reschedule_solution(self, solution: List) -> List:
        """根据新的工单顺序重新排程"""
        orders = [item[0] for item in solution]
        return self._generate_initial_solution(orders)

    def _evaluate_solution(self, solution: List) -> float:
        """
        评估解的质量
        目标：最小化总延期时间 + 换线时间 + 设备不平衡度
        """
        total_tardiness = 0.0
        total_setup = 0.0
        machine_loads: Dict[str, float] = {}

        for order, schedule in solution:
            if not schedule:
                continue

            last_step = schedule[-1]
            end_time = last_step["end_time"]

            # 延期惩罚
            if end_time > order.due_date:
                tardiness = (end_time - order.due_date).total_seconds() / 3600
                # 优先级高的订单延期惩罚更重
                priority_weight = 6 - order.priority
                total_tardiness += tardiness * priority_weight * 100

            # 换线时间
            for item in schedule:
                total_setup += item["setup_time"]
                mid = item["machine_id"]
                machine_loads[mid] = machine_loads.get(mid, 0) + item["processing_time"]

        # 设备负载不平衡惩罚
        if machine_loads:
            avg_load = sum(machine_loads.values()) / len(machine_loads)
            imbalance = sum(abs(load - avg_load) for load in machine_loads.values())
        else:
            imbalance = 0

        return total_tardiness + total_setup * 0.1 + imbalance * 0.01

    def _solution_key(self, solution: List) -> str:
        """生成解的标识（用于禁忌表）"""
        return "->".join(order.order_id for order, _ in solution)

    def _build_schedule_results(self, solution: List) -> List[ScheduleResult]:
        """构建排程结果列表"""
        results = []

        for order, schedule in solution:
            for item in schedule:
                status = "ok"
                delay_hours = 0.0

                if item == schedule[-1]:  # 最后一道工序
                    if item["end_time"] > order.due_date:
                        status = "delayed"
                        delay_hours = (item["end_time"] - order.due_date).total_seconds() / 3600

                # 检查物料
                if self.material_checker:
                    ready, mat_result = self.material_checker.check_availability(order)
                    if not ready and item == schedule[0]:
                        if item["start_time"] < mat_result["ready_time"]:
                            status = "material_shortage"

                result = ScheduleResult(
                    order_id=order.order_id,
                    product=order.product,
                    process_name=item["step"].process_name,
                    machine_id=item["machine_id"],
                    start_time=item["start_time"],
                    end_time=item["end_time"],
                    quantity=order.quantity,
                    setup_time=item["setup_time"],
                    status=status,
                    delay_hours=delay_hours
                )
                results.append(result)

        return results

    def _generate_summary(self, results: List[ScheduleResult], orders: List[WorkOrder]) -> Dict:
        """生成排产摘要"""
        if not results:
            return {}

        # 按工单统计（每个工单只看最后一道工序的状态）
        order_status = {}
        for r in results:
            if r.order_id not in order_status:
                order_status[r.order_id] = r.status
            else:
                # 最后一道工序决定工单状态
                order_status[r.order_id] = r.status

        total_orders = len(orders)
        completed_on_time = sum(1 for s in order_status.values() if s == "ok")
        delayed = sum(1 for s in order_status.values() if s == "delayed")
        material_issues = sum(1 for s in order_status.values() if s == "material_shortage")

        # 计算设备利用率
        machine_usage: Dict[str, float] = {}
        for r in results:
            duration = (r.end_time - r.start_time).total_seconds() / 3600
            machine_usage[r.machine_id] = machine_usage.get(r.machine_id, 0) + duration

        # 预计完成时间
        max_end_time = max(r.end_time for r in results) if results else datetime.now()

        on_time_rate = f"{round((completed_on_time / total_orders) * 100)}%" if total_orders > 0 else "0%"
        return {
            "total_orders": total_orders,
            "on_time": completed_on_time,
            "delayed": delayed,
            "material_shortage": material_issues,
            "max_end_time": max_end_time.isoformat(),
            "machine_utilization": {mid: round(hours, 2) for mid, hours in machine_usage.items()},
            "on_time_rate": on_time_rate,
            "makespan": str(max_end_time),
            "late_orders": delayed
        }

    def _generate_alerts(self, results: List[ScheduleResult],
                         material_results: List) -> List[Dict]:
        """生成预警信息"""
        alerts = []

        # 延期预警
        delayed_orders = {}
        for r in results:
            if r.status == "delayed":
                if r.order_id not in delayed_orders:
                    delayed_orders[r.order_id] = {
                        "order_id": r.order_id,
                        "product": r.product,
                        "delay_hours": r.delay_hours
                    }

        for order_id, info in delayed_orders.items():
            alerts.append({
                "type": "delay",
                "level": "high" if info["delay_hours"] > 24 else "medium",
                "message": f"工单 {order_id} ({info['product']}) 预计延期 {info['delay_hours']:.1f} 小时",
                "suggestion": "建议与客户协商延期或安排加班/外协"
            })

        # 物料预警
        for order, ready, result in material_results:
            if not ready:
                for shortage in result.get("shortage", []):
                    alerts.append({
                        "type": "material",
                        "level": "high",
                        "message": f"工单 {order.order_id} 缺 {shortage['material']} {shortage['gap']:.1f} 单位",
                        "suggestion": f"建议立即采购，预计 {shortage.get('lead_time', 'N/A')} 天后到货"
                    })

        return alerts

    def _analyze_bottleneck(self, results: List[ScheduleResult]) -> Dict:
        """瓶颈分析"""
        if not results:
            return {}

        machine_loads: Dict[str, float] = {}
        machine_orders: Dict[str, List[str]] = {}

        for r in results:
            duration = (r.end_time - r.start_time).total_seconds() / 3600
            machine_loads[r.machine_id] = machine_loads.get(r.machine_id, 0) + duration
            if r.order_id not in machine_orders.get(r.machine_id, []):
                machine_orders.setdefault(r.machine_id, []).append(r.order_id)

        if not machine_loads:
            return {}

        # 找出负载最高的设备
        bottleneck_machine = max(machine_loads, key=machine_loads.get)
        max_load = machine_loads[bottleneck_machine]

        # 计算利用率（简化：假设每天12小时可用）
        total_available = 12 * 7  # 一周
        utilization = min(100, (max_load / total_available) * 100) if total_available > 0 else 0

        return {
            "bottleneck_machine": bottleneck_machine,
            "bottleneck_load_hours": round(max_load, 2),
            "utilization_percent": round(utilization, 1),
            "affected_orders": machine_orders.get(bottleneck_machine, []),
            "suggestion": f"设备 {bottleneck_machine} 是瓶颈，建议安排加班、外协或增加设备"
        }

    def _generate_material_plan(self, orders: List[WorkOrder],
                                results: List[ScheduleResult]) -> List[Dict]:
        """生成物料需求计划"""
        material_needs: Dict[str, Dict] = {}

        for order in orders:
            # 找到该工单第一道工序的开始时间
            start_time = None
            for r in results:
                if r.order_id == order.order_id:
                    start_time = r.start_time
                    break

            for mat_name, unit_qty in order.bom.items():
                total_need = unit_qty * order.quantity

                if mat_name not in material_needs:
                    material_needs[mat_name] = {
                        "material": mat_name,
                        "total_need": 0,
                        "orders": [],
                        "earliest_need": start_time or datetime.now()
                    }

                material_needs[mat_name]["total_need"] += total_need
                material_needs[mat_name]["orders"].append(order.order_id)
                if start_time and start_time < material_needs[mat_name]["earliest_need"]:
                    material_needs[mat_name]["earliest_need"] = start_time

        plan = []
        for mat_name, info in material_needs.items():
            mat = self.material_checker.materials.get(mat_name) if self.material_checker else None
            available = mat.stock + mat.on_the_way if mat else 0
            gap = max(0, info["total_need"] - available)

            plan.append({
                "material": mat_name,
                "total_need": round(info["total_need"], 2),
                "available": round(available, 2),
                "gap": round(gap, 2),
                "earliest_need": info["earliest_need"].isoformat(),
                "affected_orders": info["orders"],
                "purchase_suggestion": f"建议采购 {gap:.1f} 单位" if gap > 0 else "库存充足"
            })

        return plan

    def _result_to_dict(self, result: ScheduleResult) -> Dict:
        """将结果转换为字典"""
        return {
            "order_id": result.order_id,
            "product": result.product,
            "process_name": result.process_name,
            "machine_id": result.machine_id,
            "start_time": result.start_time.isoformat(),
            "end_time": result.end_time.isoformat(),
            "quantity": result.quantity,
            "setup_time": result.setup_time,
            "status": result.status,
            "delay_hours": round(result.delay_hours, 2)
        }

    def what_if_analysis(self, orders: List[WorkOrder], rush_order: WorkOrder) -> Dict:
        """
        急单插单影响分析
        返回：插入前后的对比
        """
        # 原始排程
        original = self.schedule(orders)

        # 插入急单后的排程
        new_orders = orders + [rush_order]
        new_schedule = self.schedule(new_orders)

        # 对比分析
        original_end = {r["order_id"]: r["end_time"] for r in original["schedule"]}
        new_end = {r["order_id"]: r["end_time"] for r in new_schedule["schedule"]}

        impacted = []
        for order_id, orig_end in original_end.items():
            if order_id in new_end and new_end[order_id] != orig_end:
                impacted.append({
                    "order_id": order_id,
                    "original_end": orig_end,
                    "new_end": new_end[order_id]
                })

        return {
            "rush_order": rush_order.order_id,
            "original_summary": original["summary"],
            "new_summary": new_schedule["summary"],
            "impacted_orders": impacted,
            "suggestion": f"插入急单影响 {len(impacted)} 个订单，建议评估是否接受"
        }
