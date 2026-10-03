
from Scene._shared.host import _get_workspace_root
from Scene._shared.host import _require_auth
import json
from common.log import logger
import os
import web

class WorkbenchGenerateReportHandler:
    """POST /api/workbench/generate-report - 后端直接调用技能脚本生成报表。

    避免 agent 自己用 openpyxl 生成导致格式/排序不正确。
    后端调用 generate_excel_report.py 和 generate_charts.py，
    生成标准格式的 Excel 和 HTML 报表文件。

    Request body (JSON):
        {"data_file_path": "/path/to/data.json", "scene_id": "supplier_quote_comparison"}

    Response:
        {"status": "success", "excel_path": "...", "html_path": "..."}
    """

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            import subprocess
            import sys

            body = json.loads(web.data())
            data_file_path = body.get("data_file_path", "")

            if not data_file_path or not os.path.isfile(data_file_path):
                return json.dumps({"status": "error", "message": "数据文件不存在"})

            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            workspace_root = _get_workspace_root()
            tmp_dir = os.path.join(workspace_root, "tmp")
            os.makedirs(tmp_dir, exist_ok=True)

            excel_path = os.path.join(tmp_dir, "采购比价分析结果.xlsx")
            html_path = os.path.join(tmp_dir, "采购比价分析看板.html")
            template_path = os.path.join(project_root, "skills", "procurement-comparison", "assets", "upload_templates", "采购比价分析模板.XLSX")
            excel_script = os.path.join(project_root, "skills", "procurement-comparison", "scripts", "generate_excel_report.py")
            charts_script = os.path.join(project_root, "skills", "procurement-comparison", "scripts", "generate_charts.py")

            results = {"status": "success", "excel_path": "", "html_path": "", "summary_path": ""}

            # 生成分析摘要 JSON（供 agent 直接 read 读取，避免 Windows cmd 编码问题）
            try:
                import collections
                with open(data_file_path, encoding="utf-8") as f:
                    raw_data = json.load(f)
                raw_records = raw_data.get("records", [])
                groups = collections.defaultdict(list)
                for rec in raw_records:
                    matnr = (rec.get("MATNR") or rec.get("matnr") or "").strip()
                    if not matnr:
                        continue
                    groups[matnr].append(rec)

                summary_list = []
                for matnr in sorted(groups.keys()):
                    recs = groups[matnr]
                    # 按日期升序排序，保证价格列表与日期列表一一对应且时间有序
                    recs = sorted(recs, key=lambda r: r.get("AEDAT") or r.get("aedat") or "")
                    prices = []
                    dates = []
                    for r in recs:
                        try:
                            p = float(r.get("NETPR", r.get("netpr", 0)) or 0)
                            prices.append(p)
                        except (ValueError, TypeError):
                            pass
                        d = r.get("AEDAT", r.get("aedat", ""))
                        if d:
                            dates.append(d)
                    summary_list.append({
                        "物料编码": matnr.lstrip("0") or "0",
                        "物料短文本": recs[0].get("TXZ01") or recs[0].get("maktx") or "",
                        "记录数": len(recs),
                        "最高价": max(prices) if prices else None,
                        "最低价": min(prices) if prices else None,
                        "平均价": round(sum(prices) / len(prices), 2) if prices else None,
                        "最早日期": dates[0] if dates else "",
                        "最晚日期": dates[-1] if dates else "",
                        "价格列表(按日期升序)": prices,
                        "日期列表(与价格一一对应)": dates,
                    })

                summary_path = os.path.join(tmp_dir, "analysis_summary.json")
                with open(summary_path, "w", encoding="utf-8") as f:
                    json.dump({"物料列表": summary_list}, f, ensure_ascii=False, indent=2)
                results["summary_path"] = summary_path
            except Exception as e:
                logger.error(f"[GenerateReport] Summary error: {e}")

            # 生成 Excel 报表
            try:
                proc = subprocess.run(
                    [sys.executable, excel_script, "--input", data_file_path,
                     "--template", template_path, "--output", excel_path, "--max-cols", "100"],
                    capture_output=True, text=True, encoding="utf-8", cwd=workspace_root
                )
                if proc.returncode == 0 and os.path.isfile(excel_path):
                    results["excel_path"] = excel_path
                else:
                    logger.error(f"[GenerateReport] Excel failed: {proc.stderr}")
            except Exception as e:
                logger.error(f"[GenerateReport] Excel error: {e}")

            # 生成 HTML 看板
            try:
                proc = subprocess.run(
                    [sys.executable, charts_script, "--input", data_file_path, "--output", html_path],
                    capture_output=True, text=True, encoding="utf-8", cwd=workspace_root
                )
                if proc.returncode == 0 and os.path.isfile(html_path):
                    results["html_path"] = html_path
                else:
                    logger.error(f"[GenerateReport] HTML failed: {proc.stderr}")
            except Exception as e:
                logger.error(f"[GenerateReport] HTML error: {e}")

            return json.dumps(results, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[GenerateReport] error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
