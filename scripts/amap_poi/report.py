# -*- coding: utf-8 -*-
"""运行报告：Skill 层读它决定话术，不靠匹配 stdout 里的符号。

报告与数据导出是两件事：
  * 数据文件（CSV/JSON/XLSX）里有记录；
  * 报告（``--report-json``）里只有「跑成什么样」——每单元条数、是否触顶、错误码。

退出码也在这里统一算，保证 CLI 与报告口径一致。
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_FATAL = 3


def compute_exit_code(result: Any) -> int:
    """0 = 全好；2 = 有触顶/部分单元失败；3 = 致命（key/配额）。"""
    if result.fatal:
        return EXIT_FATAL
    if result.hit_cap_units or result.failed_units or result.stopped_early:
        return EXIT_PARTIAL
    return EXIT_OK


def unit_note(unit: Any) -> str:
    """给单个单元一句人话说明，供报告与终端共用。"""
    if unit.status == "failed":
        return "失败：%s" % (unit.error or "未知错误")
    if unit.hit_cap:
        extra = ""
        if unit.count_hint > unit.raw:
            extra = "，服务端自报 count=%d" % unit.count_hint
        return ("疑似触顶：已取到 %d 条%s。已达单查询天花板区间，可能还有数据没拿到"
                % (unit.raw, extra))
    if unit.status == "partial":
        return "未跑完（%s），已取 %d 条" % (unit.error or "被上限中断", unit.raw)
    if unit.raw == 0:
        return "无结果：该关键词在此地区查不到数据"
    return "正常：%d 条（已翻到空页）" % unit.raw


def build_report(
    result: Any,
    params: Dict[str, Any],
    tool_version: str = "",
    started_at: str = "",
    outputs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """组装 report.json，字段与 README §6.1 契约一致。"""
    units = []
    for unit in result.units:
        entry = unit.as_dict()
        entry["note"] = unit_note(unit)
        units.append(entry)

    return {
        "tool_version": tool_version,
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": params,
        "units": units,
        "totals": {
            "requests": result.requests,
            "raw": result.raw_total,
            "unique": result.unique,
            "tel_empty_rate": result.tel_empty_rate,
        },
        "stopped_early": result.stopped_early,
        "stop_reason": result.stop_reason,
        "hit_cap_units": [
            {"keyword": unit.keyword, "region": unit.region, "raw": unit.raw}
            for unit in result.hit_cap_units
        ],
        "errors": list(result.errors),
        "outputs": dict(outputs or {}),
        "exit_code": compute_exit_code(result),
    }


def write_report(report: Dict[str, Any], path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path
