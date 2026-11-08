# -*- coding: utf-8 -*-
"""导出层：CSV / JSON / XLSX。

三个格式的分工（不是「换个扩展名」）：

* **CSV** —— 只要数据行，给 Excel / 数透直接用。``utf-8-sig`` 保证双击不乱码。
* **JSON** —— 带 envelope：查询参数、汇总、每单元明细、错误列表、触顶标记。
  给 Agent 或下游程序读，机器可判定「数据完不完整」。
* **XLSX** —— 依赖 ``openpyxl``；缺失时**降级为 CSV**并返回实际写出的路径，
  绝不抛 traceback。

空值一律写空字符串，不写 ``[]``、不写 ``None``（normalize 层已保证）。
"""

import csv
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

#: 内部英文字段 → 表头中文名（对外可见的唯一映射点）
HEADERS = {
    "poi_id": "POI ID",
    "name": "名称",
    "tel": "电话",
    "type": "分类",
    "typecode": "分类编码",
    "address": "地址",
    "province": "省",
    "city": "城市",
    "district": "区县",
    "adcode": "行政区编码",
    "lng": "经度",
    "lat": "纬度",
    "tag": "标签",
    "business_area": "商圈",
    "website": "网站",
    "email": "邮箱",
    "rating": "评分",
    "open_time": "营业时间",
    "keyword": "命中关键词",
}

#: CSV / XLSX 的列顺序
COLUMNS = (
    "name",
    "tel",
    "address",
    "province",
    "city",
    "district",
    "adcode",
    "type",
    "typecode",
    "lng",
    "lat",
    "tag",
    "business_area",
    "website",
    "email",
    "rating",
    "open_time",
    "keyword",
    "poi_id",
)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_path(out_dir: str, fmt: str, prefix: str = "amap_poi") -> str:
    """生成默认输出路径：``<out_dir>/<prefix>_<时间戳>.<fmt>``。"""
    return os.path.join(out_dir, "%s_%s.%s" % (prefix, _stamp(), fmt))


# ---------------------------------------------------------------- CSV


def write_csv(records: List[Dict[str, str]], path: str) -> str:
    """写 CSV。返回实际路径。"""
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([HEADERS[column] for column in COLUMNS])
        for record in records:
            writer.writerow([record.get(column, "") or "" for column in COLUMNS])
    return path


# ---------------------------------------------------------------- JSON


def build_envelope(
    records: List[Dict[str, str]],
    result: Any = None,
    params: Optional[Dict[str, Any]] = None,
    tool_version: str = "",
    started_at: str = "",
) -> Dict[str, Any]:
    """组装 JSON envelope。``result`` 可为 None（只导出数据时）。"""
    envelope: Dict[str, Any] = {
        "tool_version": tool_version,
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": "amap-poi-collector-skill",
        "params": params or {},
        "totals": {
            "unique": len(records),
            "tel_empty_rate": 0.0,
            "requests": 0,
        },
        "units": [],
        "errors": [],
        "outputs": {},
        "records": records,
    }
    if result is not None:
        envelope["totals"] = {
            "unique": result.unique,
            "raw": result.raw_total,
            "requests": result.requests,
            "tel_empty_rate": result.tel_empty_rate,
        }
        envelope["units"] = [unit.as_dict() for unit in result.units]
        envelope["errors"] = list(result.errors)
        envelope["stopped_early"] = result.stopped_early
        envelope["stop_reason"] = result.stop_reason
        envelope["hit_cap_units"] = [
            {"keyword": unit.keyword, "region": unit.region, "raw": unit.raw}
            for unit in result.hit_cap_units
        ]
    return envelope


def write_json(envelope: Dict[str, Any], path: str) -> str:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


# ---------------------------------------------------------------- XLSX


def xlsx_available() -> bool:
    try:
        import openpyxl  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def write_xlsx(records: List[Dict[str, str]], path: str, summary=None) -> str:
    """写 XLSX。openpyxl 不可用时自动改写成同名 .csv，返回实际路径。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except Exception:  # noqa: BLE001
        fallback = os.path.splitext(path)[0] + ".csv"
        return write_csv(records, fallback)

    _ensure_parent(path)
    book = Workbook()
    sheet = book.active
    sheet.title = "POI"
    sheet.append([HEADERS[column] for column in COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for record in records:
        sheet.append([record.get(column, "") or "" for column in COLUMNS])
    sheet.freeze_panes = "A2"

    widths = {"名称": 32, "电话": 24, "地址": 46, "分类": 22, "命中关键词": 18}
    for idx, column in enumerate(COLUMNS, start=1):
        letter = sheet.cell(row=1, column=idx).column_letter
        sheet.column_dimensions[letter].width = widths.get(HEADERS[column], 14)

    if summary:
        info = book.create_sheet("运行摘要")
        for row in summary:
            info.append(list(row))
        info.column_dimensions["A"].width = 20
        info.column_dimensions["B"].width = 60

    book.save(path)
    return path


# ---------------------------------------------------------------- 统一入口


def export(
    records: List[Dict[str, str]],
    fmt: str,
    out_path: Optional[str] = None,
    out_dir: str = "output",
    result: Any = None,
    params: Optional[Dict[str, Any]] = None,
    tool_version: str = "",
    started_at: str = "",
) -> str:
    """按格式导出，返回实际写出的路径（xlsx 降级时可能不是 .xlsx）。"""
    fmt = (fmt or "csv").lower().lstrip(".")
    if fmt not in ("csv", "json", "xlsx"):
        raise ValueError("不支持的输出格式：%r（可选 csv / json / xlsx）" % fmt)

    path = out_path or default_path(out_dir, fmt)

    if fmt == "csv":
        return write_csv(records, path)
    if fmt == "json":
        envelope = build_envelope(
            records,
            result=result,
            params=params,
            tool_version=tool_version,
            started_at=started_at,
        )
        written = write_json(envelope, path)
        envelope.setdefault("outputs", {})["json"] = written
        return written

    summary = build_summary_rows(result) if result is not None else None
    return write_xlsx(records, path, summary=summary)


def build_summary_rows(result: Any) -> List[List[str]]:
    """XLSX 的「运行摘要」sheet 内容。"""
    rows: List[List[str]] = [
        ["项目", "值"],
        ["导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["唯一记录数", str(result.unique)],
        ["原始记录数", str(result.raw_total)],
        ["请求次数", str(result.requests)],
        ["电话空缺率", "%.1f%%" % (result.tel_empty_rate * 100)],
        ["", ""],
        ["关键词", "地区"],
    ]
    for unit in result.units:
        rows.append(
            [
                "%s（%s / %s 条 / %s 次请求）"
                % (unit.keyword, unit.region, unit.raw, unit.requests),
                "触顶，实际数据更多" if unit.hit_cap else unit.status,
            ]
        )
    if result.errors:
        rows.append(["", ""])
        rows.append(["错误", "详情"])
        for item in result.errors:
            rows.append([item.get("unit", ""), item.get("message", "")])
    return rows
