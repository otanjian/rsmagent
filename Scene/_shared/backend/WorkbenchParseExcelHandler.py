
from Scene._shared.host import _require_auth
import json
from common.log import logger
import os
import web

class WorkbenchParseExcelHandler:
    """POST /api/workbench/parse-excel - Parse Excel file and return structured data.
    
    Supports both horizontal layout (headers in first row) and vertical layout 
    (headers in first column, years in first row).
    """

    def POST(self):
        _require_auth()
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            import tempfile
            import uuid
            import re
            from openpyxl import load_workbook

            data = web.data()
            if not data:
                return json.dumps({"status": "error", "message": "No data received"})

            body = json.loads(data)
            file_content = body.get("file_content", "")
            filename = body.get("filename", "data.xlsx")

            if not file_content:
                return json.dumps({"status": "error", "message": "No file content provided"})

            # Decode base64 content
            import base64
            try:
                binary_data = base64.b64decode(file_content)
            except Exception:
                return json.dumps({"status": "error", "message": "Invalid file content"})

            # Save to temp file
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, f"workbench_{uuid.uuid4().hex[:8]}_{filename}")
            with open(temp_path, "wb") as f:
                f.write(binary_data)

            try:
                # Parse Excel with openpyxl
                wb = load_workbook(temp_path, data_only=True)
                sheets = []

                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    rows = []
                    for row in ws.iter_rows(values_only=True):
                        # Convert None to empty string and handle non-serializable types
                        cleaned_row = []
                        for cell in row:
                            if cell is None:
                                cleaned_row.append("")
                            elif isinstance(cell, (int, float, str, bool)):
                                cleaned_row.append(cell)
                            else:
                                cleaned_row.append(str(cell))
                        rows.append(cleaned_row)

                    if len(rows) > 0:
                        # Detect layout direction and transform if needed
                        transformed = self._transform_financial_data(rows)
                        sheets.append({
                            "name": sheet_name,
                            "headers": transformed["headers"],
                            "data": transformed["data"],
                            "total_rows": len(transformed["data"]),
                            "layout": transformed["layout"],
                            "original_headers": rows[0] if rows else []
                        })

                # Clean up temp file
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

                return json.dumps({
                    "status": "success",
                    "filename": filename,
                    "sheets": sheets,
                    "total_sheets": len(sheets)
                }, ensure_ascii=False)

            except Exception as e:
                # Clean up temp file on error
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                return json.dumps({"status": "error", "message": f"Excel parse error: {str(e)}"})

        except Exception as e:
            logger.error(f"[WebChannel] Workbench parse excel error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def _transform_financial_data(self, rows):
        """Detect layout direction and transform financial statement data.
        
        Horizontal layout: First row = field names, first column = year/period
        Vertical layout: First column = field names, first row = year/period
        
        Handles empty rows, merged cells, and title rows.
        """
        import re
        
        if not rows or len(rows) < 2:
            return {"headers": rows[0] if rows else [], "data": rows[1:] if len(rows) > 1 else [], "layout": "horizontal"}
        
        # Filter out completely empty rows
        non_empty_rows = [row for row in rows if any(str(cell).strip() for cell in row)]
        if len(non_empty_rows) < 2:
            return {"headers": rows[0] if rows else [], "data": [], "layout": "horizontal"}
        
        # Find the header row (first row that looks like a header)
        # Header row should have "项目" or "科目" or year indicators in first row
        year_pattern = re.compile(r'(20\d{2}|\d{4}年|年度|年份|year|period|期间)', re.IGNORECASE)
        
        header_row_idx = 0
        for i, row in enumerate(non_empty_rows):
            row_str = ','.join(str(cell) for cell in row if cell)
            # Check if this row looks like a header
            has_year = bool(year_pattern.search(row_str))
            has_project = '项目' in row_str or '科目' in row_str or 'item' in row_str.lower()
            has_financial = any(kw in row_str for kw in ['资产', '负债', '权益', '收入', '成本', '利润', '现金'])
            
            if has_project or (has_year and has_financial):
                header_row_idx = i
                break
        
        # Extract data starting from header row
        data_rows = non_empty_rows[header_row_idx:]
        if len(data_rows) < 2:
            return {"headers": data_rows[0] if data_rows else [], "data": [], "layout": "horizontal"}
        
        first_row = data_rows[0]
        first_col = [row[0] if row else "" for row in data_rows]
        
        # Check if first row contains year indicators (horizontal headers)
        first_row_has_years = any(year_pattern.search(str(cell)) for cell in first_row[1:] if cell)
        
        # Check if first column contains year indicators (vertical headers)
        first_col_has_years = any(year_pattern.search(str(cell)) for cell in first_col[1:] if cell)
        
        # Check if first row contains financial field names
        financial_fields = ['资产', '负债', '权益', '收入', '成本', '利润', '现金', '应收', '存货', 
                           '固定资产', '货币资金', '流动资产', '非流动资产', '流动负债', '非流动负债',
                           '所有者权益', '股东权益', '实收资本', '资本公积', '未分配利润',
                           '营业收入', '营业成本', '营业利润', '利润总额', '净利润',
                           '经营活动', '投资活动', '筹资活动', '现金流入', '现金流出']
        first_row_has_fields = any(any(f in str(cell) for f in financial_fields) for cell in first_row[1:] if cell)
        first_col_has_fields = any(any(f in str(cell) for f in financial_fields) for cell in first_col[1:] if cell)
        
        # Determine layout
        # If first column has financial fields and first row has years -> vertical layout
        if first_col_has_fields and first_row_has_years and not first_row_has_fields:
            layout = "vertical"
        # If first row has financial fields and first column has years -> horizontal layout
        elif first_row_has_fields and (first_col_has_years or str(first_col[0]).strip() in ['', '项目', '科目', 'item']):
            layout = "horizontal"
        else:
            # Default: check if first row looks like headers (contains years or field names)
            if first_row_has_years or first_row_has_fields:
                layout = "horizontal"
            else:
                layout = "vertical"
        
        if layout == "horizontal":
            # Standard horizontal layout - no transformation needed
            # Filter out empty data rows
            clean_data = [row for row in data_rows[1:] if any(str(cell).strip() for cell in row)]
            return {
                "headers": first_row,
                "data": clean_data,
                "layout": "horizontal"
            }
        else:
            # Vertical layout - need to transpose
            # First column = field names, first row = years
            field_names = first_col
            years = first_row
            
            # Build transposed data
            # New headers: [field_name_column_name] + years
            new_headers = ["项目/科目"] + [str(y) for y in years[1:] if str(y).strip()]
            
            # New data: each row is a field with values across years
            new_data = []
            for i in range(1, len(field_names)):
                field_name = str(field_names[i]).strip()
                if not field_name:
                    continue
                row_data = [field_name]
                for j in range(1, len(years)):
                    if i < len(data_rows) and j < len(data_rows[i]):
                        row_data.append(data_rows[i][j])
                    else:
                        row_data.append("")
                # Only add if row has some data
                if any(str(cell).strip() for cell in row_data[1:]):
                    new_data.append(row_data)
            
            return {
                "headers": new_headers,
                "data": new_data,
                "layout": "vertical"
            }
