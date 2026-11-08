# -*- coding: utf-8 -*-
"""字段归一化与地区参数校验。

这一层不发请求、不写文件，全部是纯函数，便于离线单测。

设计依据（2026-11-07 实测，见 README §3.1）：
  * 高德对空值返回 JSON 数组 ``[]``，不是空字符串。直接 str() 会得到字面量 "[]"。
  * ``tel`` 可能是字符串，也可能是 ``[]``；多号码用分号连接，如 "0898-00000000;13800000000"。
  * ``location`` 是 "经度,纬度" 字符串，缺失时为 ``[]``。
  * ``city=<区县名>`` 会歧义解析（实测 "龙华区" 命中深圳），区县必须用 6 位 adcode。
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 输出记录的规范化字段顺序（内部用英文键，export 层再映射成中文表头）
RECORD_FIELDS: Tuple[str, ...] = (
    "poi_id",
    "name",
    "tel",
    "type",
    "typecode",
    "address",
    "province",
    "city",
    "district",
    "adcode",
    "lng",
    "lat",
    "tag",
    "business_area",
    "website",
    "email",
    "rating",
    "open_time",
    "keyword",
)

#: 被视为「空」的原始值形态
_EMPTY = ("", None, [], {})

_DISTRICT_SUFFIXES = ("区", "县", "旗", "林区", "特区")
_PROVINCE_SUFFIXES = ("省", "自治区", "特别行政区")


def as_text(value: Any, sep: str = ";") -> str:
    """把高德的任意字段值压成干净字符串。

    ``[]`` / ``None`` / ``{}`` → ``""``；
    列表 → 用 ``sep`` 连接（元素再做一次 as_text）；字典 → 序列化为 ``k:v``。

    >>> as_text([])
    ''
    >>> as_text("0898-00000000")
    '0898-00000000'
    >>> as_text(["周一", "周二"])
    '周一;周二'
    """
    if value in _EMPTY:
        return ""
    if isinstance(value, str):
        text = value.strip()
        return "" if text == "[]" else text
    if isinstance(value, (list, tuple)):
        parts = [as_text(item, sep) for item in value]
        return sep.join(part for part in parts if part)
    if isinstance(value, dict):
        parts = ["%s:%s" % (k, as_text(v, sep)) for k, v in value.items() if as_text(v, sep)]
        return sep.join(parts)
    return str(value).strip()


def as_rating(value: Any) -> str:
    """``biz_ext`` 里的 rating 常为字符串，缺失时是 ``[]``。"""
    return as_text(value)


def as_nested_text(value: Any, key: str) -> str:
    """从 ``biz_ext`` 这类嵌套字典里取一个标量字段。"""
    if not isinstance(value, dict):
        return ""
    return as_text(value.get(key))


def split_location(value: Any) -> Tuple[str, str]:
    """把 ``"113.5,22.5"`` 拆成 (经度, 纬度)；缺失或畸形一律返回 ("", "")。

    >>> split_location("113.5,22.5")
    ('113.5', '22.5')
    >>> split_location([])
    ('', '')
    """
    text = as_text(value)
    if not text or "," not in text:
        return "", ""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2 or not all(parts):
        return "", ""
    return parts[0], parts[1]


def normalize_poi(raw: Dict[str, Any], keyword: str = "") -> Dict[str, str]:
    """把一条高德 v3 POI 原始对象转成规范记录。

    必有的键见 :data:`RECORD_FIELDS`，全部是字符串，空值一律为 ``""``。
    """
    raw = raw or {}
    typecode = as_text(raw.get("typecode"))
    type_name = as_text(raw.get("type"))
    label = type_name
    if typecode and typecode != type_name:
        label = "%s(%s)" % (type_name, typecode) if type_name else typecode

    lng, lat = split_location(raw.get("location"))
    biz_ext = raw.get("biz_ext")

    return {
        "poi_id": as_text(raw.get("id")),
        "name": as_text(raw.get("name")),
        "tel": as_text(raw.get("tel")),
        "type": label,
        "typecode": typecode,
        "address": as_text(raw.get("address")),
        "province": as_text(raw.get("pname")),
        "city": as_text(raw.get("cityname")),
        "district": as_text(raw.get("adname")),
        "adcode": as_text(raw.get("adcode")),
        "lng": lng,
        "lat": lat,
        "tag": as_text(raw.get("tag"), sep=";"),
        "business_area": as_text(raw.get("business_area")),
        "website": as_text(raw.get("website")),
        "email": as_text(raw.get("email")),
        "rating": as_nested_text(biz_ext, "rating"),
        "open_time": as_nested_text(biz_ext, "open_time"),
        "keyword": keyword,
    }


def merge_keyword(record: Dict[str, str], keyword: str) -> None:
    """把新命中的关键词并入 ``keyword`` 列，用 ``;`` 分隔且去重保序。"""
    if not keyword:
        return
    existing = [part for part in record.get("keyword", "").split(";") if part]
    if keyword not in existing:
        existing.append(keyword)
    record["keyword"] = ";".join(existing)


def parse_region(value: str) -> Tuple[str, str]:
    """校验并分类地区参数，返回 ``(kind, cleaned)``。

    kind 取值：``"adcode"`` / ``"province"`` / ``"city"``。

    区县名（以 区/县/旗 结尾且不是 6 位数字）会被**拒绝**：实测 ``city=龙华区``
    会被高德解析成深圳的龙华区，属于静默的数据错误，比直接报错更危险。

    >>> parse_region("460106")
    ('adcode', '460106')
    >>> parse_region("海南省")
    ('province', '海南省')
    >>> parse_region("成都")
    ('city', '成都')
    >>> parse_region("龙华区")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ValueError: ...
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("地区参数为空：请给城市名、省名或 6 位 adcode")

    if text.isdigit() and len(text) == 6:
        return "adcode", text

    if text.endswith(_PROVINCE_SUFFIXES):
        return "province", text

    if text.endswith(_DISTRICT_SUFFIXES):
        raise ValueError(
            "不接受区县名称 %r：高德会把同名区县解析到错误城市（实测「龙华区」命中深圳）。"
            "请改用 6 位 adcode，例如海口市龙华区是 460106。" % text
        )

    return "city", text


def collect_region_errors(values: Iterable[str]) -> List[str]:
    """批量校验地区参数，返回错误消息列表（空列表表示全部通过）。"""
    errors: List[str] = []
    for value in values:
        try:
            parse_region(value)
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def as_poi_list(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """从 API 响应里安全取出 pois 列表。

    高德在无结果时返回 ``[]``，偶尔返回 ``{}``；两种都归一成 ``[]``。
    返回 ``None`` 表示响应结构异常（不是列表也不是空字典），调用方应视为异常而非「没有数据」。
    """
    pois = (payload or {}).get("pois")
    if isinstance(pois, list):
        return pois
    if isinstance(pois, dict) and not pois:
        return []
    return None
