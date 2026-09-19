#!/usr/bin/env python3
"""
气袋质量追溯查询模块
提供气袋总查询和气袋明细查询(限定面料LOT反查)功能
"""

import os
import json
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


class AirbagQuery:
    """气袋总查询"""

    def __init__(self):
        self.webhook_url = os.getenv(
            'AIRBAG_WEBHOOK_URL',
            '<your-airbag-webhook-url>'
        )

    def _get_headers(self):
        return {
            'Content-Type': 'application/json'
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(requests.exceptions.RequestException))
    def query_airbags(self, box_no=None, p_line=None, barcode_no=None):
        """
        气袋总查询（至少提供一个查询条件）

        Args:
            box_no (str, optional): 箱号
            p_line (str, optional): 产线
            barcode_no (str, optional): 气袋条码

        Returns:
            dict: 气袋查询结果

        Raises:
            ValueError: 当所有参数都为空时抛出异常
        """
        # 去除空白字符
        box_no = str(box_no).strip() if box_no else ""
        p_line = str(p_line).strip() if p_line else ""
        barcode_no = str(barcode_no).strip() if barcode_no else ""

        if not box_no and not p_line and not barcode_no:
            raise ValueError("必须至少提供一个查询条件（box_no/p_line/barcode_no），不能全部为空！")

        data = {
            "box_no": box_no,
            "p_line": p_line,
            "barcode_no": barcode_no
        }

        response = requests.post(
            self.webhook_url,
            headers=self._get_headers(),
            json=data,
            timeout=60
        )
        response.raise_for_status()

        return response.json()

    def format_result(self, result):
        """
        格式化查询结果

        Args:
            result (dict): API返回的原始结果

        Returns:
            str: 格式化后的文本描述
        """
        if not result:
            return "未查询到气袋数据"

        # 检查接口返回状态
        code = result.get('code', 200) if isinstance(result, dict) else 200
        if code != 200:
            msg = result.get('msg', '未知错误')
            data = result.get('data', {})
            err_msg = data.get('message', msg) if isinstance(data, dict) else msg
            return f"查询失败: {err_msg}"

        # 提取数据列表：兼容 data 为 list / dict(含res) / dict(含items) 等格式
        data = result.get('data', []) if isinstance(result, dict) else result
        if isinstance(data, dict):
            airbags = data.get('res', data.get('items', data.get('list', [])))
        elif isinstance(data, list):
            airbags = data
        else:
            airbags = []

        if not airbags:
            return "未查询到气袋数据"

        summary = f"查询到 {len(airbags)} 条气袋记录\n\n"

        for i, airbag in enumerate(airbags[:10], 1):
            summary += f"【{i}】"
            if 'box_no' in airbag:
                summary += f" 箱号: {airbag['box_no']}"
            if 'p_line' in airbag:
                summary += f" 产线: {airbag['p_line']}"
            if 'barcode_no' in airbag:
                summary += f" 条码: {airbag['barcode_no']}"
            if 'serial_no' in airbag:
                summary += f" 序列号: {airbag['serial_no']}"
            if 'status' in airbag:
                summary += f" 状态: {airbag['status']}"
            summary += "\n"

        if len(airbags) > 10:
            summary += f"\n... 还有 {len(airbags) - 10} 条记录未显示"

        return summary


class AirbagDetailQuery:
    """气袋明细查询（限定面料LOT反查）"""

    def __init__(self):
        self.webhook_url = os.getenv(
            'AIRBAG_DETAIL_WEBHOOK_URL',
            '<your-airbag-webhook-url>'
        )

    def _get_headers(self):
        return {
            'Content-Type': 'application/json'
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(requests.exceptions.RequestException))
    def query_by_fabric_lot(self, f_lot):
        """
        气袋明细查询（限定面料LOT反查）

        Args:
            f_lot (str): 面料LOT号，**必填参数，不允许为空**。

        Returns:
            dict: 气袋明细查询结果

        Raises:
            ValueError: 当 f_lot 为空或 None 时抛出异常
        """
        if not f_lot or not str(f_lot).strip():
            raise ValueError("面料LOT(f_lot)为必填参数，不能为空！")

        data = {
            "f_lot": str(f_lot).strip()
        }

        response = requests.post(
            self.webhook_url,
            headers=self._get_headers(),
            json=data,
            timeout=60
        )
        response.raise_for_status()

        return response.json()

    def format_result(self, result):
        """
        格式化查询结果

        Args:
            result (dict): API返回的原始结果

        Returns:
            str: 格式化后的文本描述
        """
        if not result:
            return "未查询到气袋明细数据"

        # 检查接口返回状态
        code = result.get('code', 200) if isinstance(result, dict) else 200
        if code != 200:
            msg = result.get('msg', '未知错误')
            data = result.get('data', {})
            err_msg = data.get('message', msg) if isinstance(data, dict) else msg
            return f"查询失败: {err_msg}"

        # 提取数据列表：兼容 data 为 list / dict(含res) / dict(含items) 等格式
        data = result.get('data', []) if isinstance(result, dict) else result
        if isinstance(data, dict):
            details = data.get('res', data.get('items', data.get('list', [])))
        elif isinstance(data, list):
            details = data
        else:
            details = []

        if not details:
            return "未查询到气袋明细数据"

        summary = f"查询到 {len(details)} 条气袋明细记录\n\n"

        for i, detail in enumerate(details[:10], 1):
            summary += f"【{i}】"
            if 'f_lot' in detail:
                summary += f" 面料LOT: {detail['f_lot']}"
            if 'box_no' in detail:
                summary += f" 箱号: {detail['box_no']}"
            if 'barcode_no' in detail:
                summary += f" 条码: {detail['barcode_no']}"
            if 'p_line' in detail:
                summary += f" 产线: {detail['p_line']}"
            if 'status' in detail:
                summary += f" 状态: {detail['status']}"
            summary += "\n"

        if len(details) > 10:
            summary += f"\n... 还有 {len(details) - 10} 条记录未显示"

        return summary


class AirbagMaterialQuery:
    """气袋原料追溯查询"""

    def __init__(self):
        self.webhook_url = os.getenv(
            'AIRBAG_MATERIAL_WEBHOOK_URL',
            '<your-airbag-webhook-url>'
        )
        self._airbag_query = AirbagQuery()

    def _get_headers(self):
        return {
            'Content-Type': 'application/json'
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(requests.exceptions.RequestException))
    def query_materials(self, header_id, cut_version=None, sew_version=None):
        """
        气袋原料追溯查询

        Args:
            header_id (str): 作业单号，**必填参数**。
            cut_version (str, optional): 裁片换料版本
            sew_version (str, optional): 线料换料版本

        Returns:
            dict: 原料追溯查询结果

        Raises:
            ValueError: 当 header_id 为空时抛出异常
        """
        if not header_id or not str(header_id).strip():
            raise ValueError("作业单号(header_id)为必填参数，不能为空！")

        data = {
            "header_id": str(header_id).strip(),
            "cut_version": str(cut_version).strip() if cut_version else "",
            "sew_version": str(sew_version).strip() if sew_version else ""
        }

        response = requests.post(
            self.webhook_url,
            headers=self._get_headers(),
            json=data,
            timeout=60
        )
        response.raise_for_status()

        return response.json()

    def trace_materials_by_barcode(self, barcode_no):
        """
        通过气袋条码自动追溯原料（两步查询）

        步骤：
        1. 用 barcode_no 调用气袋总查询，获取 header_id / cut_version / sew_version
        2. 用这些字段调用原料追溯接口，获取所用原料

        Args:
            barcode_no (str): 气袋条码，**必填参数**。

        Returns:
            dict: 包含气袋信息和原料追溯结果

        Raises:
            ValueError: 当 barcode_no 为空时抛出异常
            RuntimeError: 当气袋总查询无数据时抛出异常
        """
        if not barcode_no or not str(barcode_no).strip():
            raise ValueError("气袋条码(barcode_no)为必填参数，不能为空！")

        barcode_no = str(barcode_no).strip()

        # 第一步：查气袋总查询获取作业单号等信息
        airbag_result = self._airbag_query.query_airbags(barcode_no=barcode_no)

        code = airbag_result.get('code', 200)
        if code != 200:
            msg = airbag_result.get('msg', '查询失败')
            data = airbag_result.get('data', {})
            err_msg = data.get('message', msg) if isinstance(data, dict) else msg
            raise RuntimeError(f"气袋总查询失败: {err_msg}")

        data = airbag_result.get('data', [])
        if isinstance(data, dict):
            airbags = data.get('res', data.get('items', data.get('list', [])))
        elif isinstance(data, list):
            airbags = data
        else:
            airbags = []

        if not airbags:
            raise RuntimeError(f"未查询到条码 {barcode_no} 对应的气袋信息，无法追溯原料")

        # 取第一条记录的原料追溯字段
        airbag = airbags[0]
        header_id = airbag.get('header_id', '')
        cut_version = airbag.get('cut_version', '')
        sew_version = airbag.get('sew_version', '')

        if not header_id:
            raise RuntimeError(f"条码 {barcode_no} 的气袋记录中缺少作业单号(header_id)，无法追溯原料")

        # 第二步：查原料追溯
        material_result = self.query_materials(
            header_id=header_id,
            cut_version=cut_version,
            sew_version=sew_version
        )

        return {
            "airbag_info": airbag,
            "material_result": material_result
        }

    def format_result(self, result):
        """
        格式化原料追溯查询结果

        Args:
            result (dict): API返回的原始结果

        Returns:
            str: 格式化后的文本描述
        """
        if not result:
            return "未查询到原料追溯数据"

        # 检查接口返回状态
        code = result.get('code', 200) if isinstance(result, dict) else 200
        if code != 200:
            msg = result.get('msg', '未知错误')
            data = result.get('data', {})
            err_msg = data.get('message', msg) if isinstance(data, dict) else msg
            return f"查询失败: {err_msg}"

        # 提取数据列表
        data = result.get('data', []) if isinstance(result, dict) else result
        if isinstance(data, dict):
            materials = data.get('res', data.get('items', data.get('list', [])))
        elif isinstance(data, list):
            materials = data
        else:
            materials = []

        if not materials:
            return "未查询到原料追溯数据"

        summary = f"查询到 {len(materials)} 条原料记录\n\n"

        for i, mat in enumerate(materials[:10], 1):
            summary += f"【{i}】"
            if 'header_id' in mat:
                summary += f" 作业单号: {mat['header_id']}"
            if 'cut_version' in mat:
                summary += f" 裁片换料版本: {mat['cut_version']}"
            if 'sew_version' in mat:
                summary += f" 线料换料版本: {mat['sew_version']}"
            if 'material_name' in mat:
                summary += f" 原料名称: {mat['material_name']}"
            if 'material_lot' in mat:
                summary += f" 原料批次: {mat['material_lot']}"
            if 'material_code' in mat:
                summary += f" 原料编码: {mat['material_code']}"
            if 'qty' in mat:
                summary += f" 用量: {mat['qty']}"
            summary += "\n"

        if len(materials) > 10:
            summary += f"\n... 还有 {len(materials) - 10} 条记录未显示"

        return summary

    def format_trace_result(self, trace_result):
        """
        格式化条码追溯结果（气袋信息+原料）

        Args:
            trace_result (dict): trace_materials_by_barcode 返回的结果

        Returns:
            str: 格式化后的文本描述
        """
        airbag = trace_result.get('airbag_info', {})
        material_result = trace_result.get('material_result', {})

        summary = "=== 气袋信息 ===\n"
        if airbag:
            if 'barcode_no' in airbag:
                summary += f"气袋条码: {airbag['barcode_no']}\n"
            if 'header_id' in airbag:
                summary += f"作业单号: {airbag['header_id']}\n"
            if 'cut_version' in airbag:
                summary += f"裁片换料版本: {airbag['cut_version']}\n"
            if 'sew_version' in airbag:
                summary += f"线料换料版本: {airbag['sew_version']}\n"
            if 'box_no' in airbag:
                summary += f"箱号: {airbag['box_no']}\n"
            if 'p_line' in airbag:
                summary += f"产线: {airbag['p_line']}\n"
        else:
            summary += "无气袋信息\n"

        summary += f"\n=== 原料追溯 ===\n"
        summary += self.format_result(material_result)

        return summary


if __name__ == '__main__':
    # ========== 气袋总查询测试 ==========
    query = AirbagQuery()

    # 测试1：按箱号查询
    print("=== 测试1: 按箱号查询 (箱号: aa) ===")
    try:
        result = query.query_airbags(box_no="aa")
        print("原始结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n=== 格式化结果 ===")
        print(query.format_result(result))
    except Exception as e:
        print(f"查询失败: {e}")

    # 测试2：全部参数为空（应抛出异常）
    print("\n=== 测试2: 全部参数为空（应拒绝） ===")
    try:
        result = query.query_airbags()
        print("原始结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as e:
        print(f"正确拒绝: {e}")
    except Exception as e:
        print(f"查询失败: {e}")

    # ========== 气袋明细查询(面料LOT)测试 ==========
    detail_query = AirbagDetailQuery()

    # 测试3：按面料LOT查询
    print("\n=== 测试3: 按面料LOT查询 (f_lot: test_lot) ===")
    try:
        result = detail_query.query_by_fabric_lot(f_lot="test_lot")
        print("原始结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n=== 格式化结果 ===")
        print(detail_query.format_result(result))
    except Exception as e:
        print(f"查询失败: {e}")

    # 测试4：面料LOT为空（应抛出异常）
    print("\n=== 测试4: 面料LOT为空（应拒绝） ===")
    try:
        result = detail_query.query_by_fabric_lot(f_lot="")
        print("原始结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as e:
        print(f"正确拒绝: {e}")
    except Exception as e:
        print(f"查询失败: {e}")

    # ========== 气袋原料追溯查询测试 ==========
    material_query = AirbagMaterialQuery()

    # 测试5：直接按作业单号查原料
    print("\n=== 测试5: 按作业单号查原料 (header_id: test_header) ===")
    try:
        result = material_query.query_materials(header_id="test_header")
        print("原始结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n=== 格式化结果 ===")
        print(material_query.format_result(result))
    except Exception as e:
        print(f"查询失败: {e}")

    # 测试6：作业单号为空（应抛出异常）
    print("\n=== 测试6: 作业单号为空（应拒绝） ===")
    try:
        result = material_query.query_materials(header_id="")
        print("原始结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as e:
        print(f"正确拒绝: {e}")
    except Exception as e:
        print(f"查询失败: {e}")

    # 测试7：通过气袋条码自动追溯原料（两步查询）
    print("\n=== 测试7: 通过条码追溯原料 (barcode_no: test_barcode) ===")
    try:
        trace_result = material_query.trace_materials_by_barcode(barcode_no="test_barcode")
        print("追溯结果:")
        print(json.dumps(trace_result, ensure_ascii=False, indent=2))
        print("\n=== 格式化结果 ===")
        print(material_query.format_trace_result(trace_result))
    except Exception as e:
        print(f"追溯失败: {e}")
