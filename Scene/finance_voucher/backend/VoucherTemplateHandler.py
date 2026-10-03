
from Scene._shared.host import _get_upload_dir
from Scene._shared.host import _require_auth
import datetime
import json
from common.log import logger
import os
import web

class VoucherTemplateHandler:
    """POST /api/voucher/generate-template - Generate voucher import template CSV.

    Supports: kingdee (金蝶), yonyou (用友), sap, oracle
    """

    # Template column definitions per software type
    TEMPLATE_SCHEMAS = {
        "kingdee": {
            "columns": [
                "凭证类别", "凭证编号", "日期", "摘要", "科目编码", "科目名称",
                "借方金额", "贷方金额", "币别", "汇率", "数量", "单价",
                "辅助核算-客户", "辅助核算-供应商", "辅助核算-员工",
                "辅助核算-项目", "辅助核算-部门", "制单人", "审核人"
            ],
            "sample_rows": [
                ["记", "001", "2024-01-15", "收到客户货款", "1002", "银行存款", "100000", "", "人民币", "1", "", "", "客户A", "", "", "", "", "张三", ""],
                ["记", "001", "2024-01-15", "收到客户货款", "1122", "应收账款", "", "100000", "人民币", "1", "", "", "客户A", "", "", "", "", "张三", ""],
            ]
        },
        "yonyou": {
            "columns": [
                "凭证类别", "凭证编号", "制单日期", "摘要", "科目编码", "科目名称",
                "借方", "贷方", "币种", "汇率", "数量", "单价",
                "客户", "供应商", "个人", "项目", "部门", "业务员", "审核人"
            ],
            "sample_rows": [
                ["记账凭证", "0001", "2024-01-15", "收到客户货款", "100201", "银行存款-工行", "100000", "", "人民币", "1", "", "", "客户A", "", "", "", "", "", ""],
                ["记账凭证", "0001", "2024-01-15", "收到客户货款", "1122", "应收账款", "", "100000", "人民币", "1", "", "", "客户A", "", "", "", "", "", ""],
            ]
        },
        "sap": {
            "columns": [
                "Document Type", "Document Number", "Posting Date", "Header Text",
                "Line Item", "GL Account", "Account Description",
                "Debit Amount", "Credit Amount", "Currency", "Exchange Rate",
                "Cost Center", "Profit Center", "Segment", "Internal Order",
                "Vendor", "Customer", "Assignment", "Reference"
            ],
            "sample_rows": [
                ["SA", "1000000001", "2024-01-15", "Receive customer payment", "1", "100200", "Bank-CNY", "100000", "", "CNY", "1", "", "", "", "", "", "CUST001", "", ""],
                ["SA", "1000000001", "2024-01-15", "Receive customer payment", "2", "112200", "AR-Domestic", "", "100000", "CNY", "1", "", "", "", "", "", "CUST001", "", ""],
            ]
        },
        "oracle": {
            "columns": [
                "Batch Name", "Journal Name", "Journal Category", "Accounting Date",
                "Currency Code", "Exchange Rate Type", "Exchange Rate",
                "Line Number", "Entered Debit", "Entered Credit",
                "Account", "Account Description", "Line Description",
                "Reference 1", "Reference 2", "Attribute 1", "Attribute 2", "Attribute 3"
            ],
            "sample_rows": [
                ["BATCH_001", "JV_001", "Manual", "2024-01-15", "CNY", "User", "1", "1", "100000", "", "01.1002.0000.0000", "Cash and Bank", "Receive customer payment", "CUST001", "", "客户A", "", ""],
                ["BATCH_001", "JV_001", "Manual", "2024-01-15", "CNY", "User", "1", "2", "", "100000", "01.1122.0000.0000", "Accounts Receivable", "Receive customer payment", "CUST001", "", "客户A", "", ""],
            ]
        },
    }

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data() or b"{}")
            software = (data.get("software") or "").strip().lower()
            book_name = (data.get("ledger_code") or "").strip()
            voucher_type = (data.get("voucher_word") or "").strip()
            date_from = (data.get("start_date") or "").strip()
            date_to = (data.get("end_date") or "").strip()

            if not software:
                return json.dumps({"status": "error", "message": "software type required"})

            if software not in self.TEMPLATE_SCHEMAS:
                supported = ", ".join(self.TEMPLATE_SCHEMAS.keys())
                return json.dumps({"status": "error", "message": f"unsupported software: {software}. supported: {supported}"})

            schema = self.TEMPLATE_SCHEMAS[software]

            # Build CSV content in memory
            import csv
            import io
            output = io.StringIO(newline="")
            writer = csv.writer(output)

            # Write metadata header rows (commented / descriptive)
            writer.writerow([f"# Voucher Import Template for {software.upper()}"])
            if book_name:
                writer.writerow([f"# Book: {book_name}"])
            if voucher_type:
                writer.writerow([f"# Voucher Type: {voucher_type}"])
            if date_from and date_to:
                writer.writerow([f"# Period: {date_from} to {date_to}"])
            writer.writerow([])  # blank row

            # Write column headers
            writer.writerow(schema["columns"])

            # Write sample data rows
            for row in schema["sample_rows"]:
                writer.writerow(row)

            # Write blank template rows for user to fill
            writer.writerow([])
            writer.writerow(["# --- Please fill your data below ---"])
            for _ in range(5):
                writer.writerow([""] * len(schema["columns"]))

            csv_content = output.getvalue()
            output.close()

            # Save to uploads directory
            upload_dir = _get_upload_dir()
            os.makedirs(upload_dir, exist_ok=True)

            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_software = "".join(ch for ch in software if ch.isalnum())
            filename = f"voucher_template_{safe_software}_{ts}.csv"
            filepath = os.path.join(upload_dir, filename)

            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                f.write(csv_content)

            # Also copy to a stable path for easy download
            download_url = f"/uploads/{filename}"
            file_url = f"file:///{filepath.replace(os.sep, '/')}"

            logger.info(f"[VoucherTemplateHandler] Generated template: {filepath}")

            return json.dumps({
                "status": "success",
                "software": software,
                "filename": filename,
                "filepath": filepath,
                "download_url": download_url,
                "file_url": file_url,
                "columns": schema["columns"],
                "sample_rows": len(schema["sample_rows"]),
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[VoucherTemplateHandler] Error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
