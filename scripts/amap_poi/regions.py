# -*- coding: utf-8 -*-
"""行政区划解析：把「区县名」换成 6 位 adcode，并列出下级单位。

为什么需要这一层
----------------
``v3/place/text`` 的 ``city`` 参数只接受名称或 6 位 adcode，但**区县名称有歧义**：
实测 ``city=龙华区`` 会被解析成深圳的龙华区，而海口的龙华区是 460106——静默返回
错误地区的数据，没有任何报错。全国区县里有 50 个名字被 2 个以上地方共用（涉及
116 个区县），让调用方靠猜或靠搜索都不可靠。

本模块用 ``v3/config/district`` **一次**拉下全国三级行政区划（34 省级 + 390 地级 +
3213 区县，实测 1 次请求 / 约 490 KB / 约 400 ms），缓存在本地。此后所有解析都在
本地完成，不再消耗配额。

分层约束
--------
这一层**不发 print**（输出格式归 cli），也**不碰业务参数**，只做：拉取、缓存、
按名字或 adcode 查找、列下级。

未验证的部分
------------
* 行政区划调整后，缓存最多过期 30 天；可用 ``--refresh-regions`` 强制刷新。
* 港澳台的层级结构与大陆不同（香港的 18 区直接挂在省级下，没有地级层），
  已按实际返回结构处理，但这些地区的 POI 覆盖情况**未验证**。
* 东莞、中山、济源、湖北四个省直管市、新疆兵团各市等**没有区县级单位**
  （下级直接是街道/镇），这些城市只能按城市整体检索。
"""

import io
import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .client import AmapError, classify, describe, http_get_json

#: 行政区划查询接口
DISTRICT_URL = "https://restapi.amap.com/v3/config/district"

#: 缓存格式版本；结构变了就加一，旧缓存会被当作无效
CACHE_VERSION = 1

#: 缓存的默认有效期（秒）。行政区划调整很少，30 天足够。
CACHE_TTL_SECONDS = 30 * 24 * 3600

#: 拉全树时的根关键词。用 nationality 名称拉，返回的是「中国」这个根节点。
ROOT_KEYWORD = "中国"

_LEVEL_LABELS = {
    "province": "省级",
    "city": "地级",
    "district": "区县",
}

#: 判断「省略后缀」时允许剥掉的后缀，长的在前，避免 "林区" 被 "区" 先吃掉
_SUFFIXES = ("特别行政区", "自治区", "林区", "特区", "省", "市", "区", "县", "旗")


class Region(object):
    """一个行政区节点。``parent_adcode`` 为空表示它是根（省级）。"""

    __slots__ = ("name", "adcode", "level", "citycode", "parent_adcode")

    def __init__(
        self,
        name: str,
        adcode: str,
        level: str,
        citycode: str = "",
        parent_adcode: str = "",
    ):
        self.name = name
        self.adcode = adcode
        self.level = level
        self.citycode = citycode
        self.parent_adcode = parent_adcode

    @property
    def level_label(self) -> str:
        return _LEVEL_LABELS.get(self.level, self.level or "未知")

    def as_list(self) -> List[str]:
        """缓存用的紧凑表示。"""
        return [self.name, self.adcode, self.level, self.citycode, self.parent_adcode]

    def __repr__(self) -> str:
        return "Region(%s/%s)" % (self.adcode, self.name)


class RegionIndex(object):
    """扁平化的行政区划索引。全部查询都在内存里完成，不发请求。"""

    def __init__(self, nodes: List[Region], fetched_at: str = ""):
        self.nodes = nodes
        self.fetched_at = fetched_at
        self._by_adcode: Dict[str, Region] = {}
        for node in nodes:
            # 同一 adcode 在扁平表里应唯一；万一上游出现重复，保留第一个，
            # 避免后写覆盖前写带来的不确定性。
            self._by_adcode.setdefault(node.adcode, node)
        self._children: Dict[str, List[Region]] = {}
        for node in nodes:
            if node.parent_adcode:
                self._children.setdefault(node.parent_adcode, []).append(node)

    # -- 查询 ------------------------------------------------------

    def get(self, adcode: str) -> Optional[Region]:
        return self._by_adcode.get((adcode or "").strip())

    def children(self, region: Region) -> List[Region]:
        return list(self._children.get(region.adcode, []))

    def path(self, region: Region) -> str:
        """返回「省 / 市 / 区县」形式的可读路径。"""
        parts = [region.name]
        seen = {region.adcode}
        current = region
        while current.parent_adcode and current.parent_adcode in self._by_adcode:
            parent = self._by_adcode[current.parent_adcode]
            if parent.adcode in seen:  # 防御：结构异常时不要死循环
                break
            seen.add(parent.adcode)
            parts.append(parent.name)
            current = parent
        return " / ".join(reversed(parts))

    def find(self, query: str) -> List[Region]:
        """按 adcode、全名、省略后缀的名字、或「父级+子级」复合名查找。

        返回列表：空 = 没找到，1 个 = 唯一命中，多个 = 有同名，调用方必须自己选。
        这里**绝不返回「猜一个」的结果**——同名区县猜错的后果是拿到别的城市的数据。

        支持复合写法是因为重名很常见（全国 50 个区县名有重名）。用户和 Agent 都更
        习惯说「海口市龙华区」而不是先查一次 adcode；只要这句里的父子关系能对上，
        就能唯一确定。
        """
        text = (query or "").strip()
        if not text:
            return []

        if text.isdigit() and len(text) == 6:
            node = self.get(text)
            return [node] if node else []

        exact = [node for node in self.nodes if node.name == text]
        if exact:
            return exact

        compound = self._find_compound(text)
        if compound:
            return compound

        prefix = [node for node in self.nodes if node.name.startswith(text)]
        if prefix:
            return prefix

        bare = _strip_suffix(text)
        if bare and bare != text:
            return [node for node in self.nodes if _strip_suffix(node.name) == bare]
        return []

    def _find_compound(self, text: str) -> List[Region]:
        """把「海口市龙华区」这类「父级 + 子级」写法解析成唯一节点。

        允许两种省略：名字里可以少写后缀（「海口龙华区」），也可以跳过中间层
        （「北京市朝阳区」——中间那个「北京城区」在用户说法里不出现）。
        只在整条字符串刚好被消耗完时才算命中，避免「海口」把「海口市」以外的
        东西也匹配进来。
        """
        top = [node for node in self.nodes if not node.parent_adcode]

        def walk(rest: str, candidates: List[Region], depth: int) -> List[Region]:
            if not rest:
                return []
            if depth > 4:
                return []
            for node in candidates:
                names = [node.name]
                bare = _strip_suffix(node.name)
                if bare and bare != node.name:
                    names.append(bare)
                for name in names:
                    if rest == name:
                        return [node]
                    if rest.startswith(name):
                        hits = walk(rest[len(name):], self._children.get(node.adcode, []), depth + 1)
                        if hits:
                            return hits
                # 跳过这一层（名字里没写中间的「城区」「郊县」等）
                hits = walk(rest, self._children.get(node.adcode, []), depth + 1)
                if hits:
                    return hits
            return []

        return walk(text, top, 0)

    # -- 序列化 ----------------------------------------------------

    def to_payload(self) -> Dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "source": "%s?subdistrict=3" % DISTRICT_URL,
            "nodes": [node.as_list() for node in self.nodes],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> Optional["RegionIndex"]:
        """从缓存内容还原；结构不认识时返回 ``None``（调用方应重新拉取）。"""
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != CACHE_VERSION:
            return None
        rows = payload.get("nodes")
        if not isinstance(rows, list):
            return None
        nodes = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            padded = list(row) + [""] * (5 - len(row))
            nodes.append(Region(padded[0], padded[1], padded[2], padded[3], padded[4]))
        if not nodes:
            return None
        return cls(nodes, fetched_at=str(payload.get("fetched_at") or ""))


def _strip_suffix(name: str) -> str:
    text = (name or "").strip()
    for suffix in _SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


# ---------------------------------------------------------------- 拉取与缓存


def resolve_all(index: RegionIndex, parsed_regions: List[Any]) -> Any:
    """把 ``(kind, value)`` 列表里的 ``district`` 项换成 adcode。

    返回 ``(resolved, notes, errors)``：

    * ``resolved`` —— 新的列表，``district`` 项已变成 ``("adcode", "460106")``
    * ``notes`` —— 给用户看的一行行说明（「龙华区 -> 460106（海南省 / 海口市 / 龙华区）」）
    * ``errors`` —— 无法解析或存在同名歧义的说明；非空时调用方必须中止

    **歧义绝不猜**：50 个区县名在全国有重名（涉及 116 个区县），猜错就等于拿到
    另一个城市的数据，而且看不出错。宁可报错让用户补一个限定条件。
    """
    resolved = []
    notes = []
    errors = []
    for kind, value in parsed_regions:
        if kind != "district":
            resolved.append((kind, value))
            continue
        matches = [node for node in index.find(value) if node.level == "district"]
        if not matches:
            # 也许用户写的是「浦东新区」这种本身没有同名、但恰好被 find 命中的情况；
            # 也可能压根不存在。给出可操作的建议。
            errors.append(
                "找不到区县 %r。请确认名称，或直接用 6 位 adcode；"
                "也可以用 --list-regions <城市名> 列出该市的区县与 adcode。" % value
            )
            continue
        if len(matches) > 1:
            lines = ["区县名 %r 有 %d 个同名，无法自动确定是哪一个：" % (value, len(matches))]
            for node in matches:
                lines.append("  %s  %s" % (node.adcode, index.path(node)))
            lines.append("请改用 6 位 adcode 重新指定。")
            errors.append("\n".join(lines))
            continue
        node = matches[0]
        resolved.append(("adcode", node.adcode))
        notes.append("%s -> %s（%s）" % (value, node.adcode, index.path(node)))
    return resolved, notes, errors


def build_url(key: str) -> str:
    """构造「拉全国三级行政区划」的请求地址。"""
    params = [
        ("key", key),
        ("keywords", ROOT_KEYWORD),
        ("subdistrict", "3"),
        ("extensions", "base"),
    ]
    import urllib.parse

    return DISTRICT_URL + "?" + urllib.parse.urlencode(params)


def parse_tree(payload: Dict[str, Any]) -> List[Region]:
    """把嵌套响应压成扁平节点列表，**丢掉 street 级**。

    丢弃 street 级有两个原因：一是区县级已经是检索粒度的下限（再细就是按街道
    检索，返回量极小且没有意义），二是东莞/中山这类城市的下级街道与城市共用
    同一个 adcode，留着会让「按 adcode 查名字」变得有歧义。
    """
    nodes: List[Region] = []
    districts = (payload or {}).get("districts")
    if not isinstance(districts, list):
        return nodes

    def walk(node: Dict[str, Any], parent_adcode: str) -> None:
        if not isinstance(node, dict):
            return
        level = str(node.get("level") or "")
        adcode = str(node.get("adcode") or "")
        if level in _LEVEL_LABELS and adcode:
            nodes.append(
                Region(
                    name=str(node.get("name") or ""),
                    adcode=adcode,
                    level=level,
                    citycode=str(node.get("citycode") or ""),
                    parent_adcode=parent_adcode,
                )
            )
            for child in node.get("districts") or []:
                walk(child, adcode)
            return
        # 不认识的层级：``keywords=中国`` 的根节点是 ``level="country"`` /
        # ``adcode=100000``，它本身不该进索引（不是检索单位），但**必须往下走**，
        # 否则整棵树都是空的。street 级也走这条路——它没有下级，自然就停了。
        if level == "street":
            return
        for child in node.get("districts") or []:
            walk(child, parent_adcode)

    for item in districts:
        walk(item, "")
    return nodes


def fetch_index(
    key: str,
    timeout: float = 30.0,
    transport: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> RegionIndex:
    """真发一次请求，拉回并解析。失败抛 :class:`AmapError`。"""
    getter = transport or (lambda url: http_get_json(url, timeout=timeout))
    payload = getter(build_url(key))

    status = str((payload or {}).get("status") or "")
    infocode = str((payload or {}).get("infocode") or "")
    if status != "1":
        raise AmapError(
            "行政区划查询失败：%s（infocode=%s）" % (describe(infocode), infocode or "无"),
            infocode=infocode,
            action=classify(infocode) if infocode else "fatal",
        )

    nodes = parse_tree(payload)
    if not nodes:
        raise AmapError(
            "行政区划查询返回了空结构，无法建立索引（可能是接口变更）", action="fatal"
        )
    return RegionIndex(nodes, fetched_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))


def cache_is_fresh(path: str, ttl: int = CACHE_TTL_SECONDS, now: Optional[float] = None) -> bool:
    """缓存文件存在且未过期。"""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    current = now if now is not None else datetime.now().timestamp()
    return (current - mtime) < ttl


def load_index(path: str) -> Optional[RegionIndex]:
    """读缓存；文件不存在、损坏、版本不符都返回 ``None``。"""
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return RegionIndex.from_payload(payload)


def save_index(index: RegionIndex, path: str) -> bool:
    """写缓存，**失败不抛异常**（只读挂载、磁盘满都不该让检索失败）。"""
    directory = os.path.dirname(os.path.abspath(path))
    try:
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(index.to_payload(), ensure_ascii=False, separators=(",", ":")))
        return True
    except (OSError, ValueError):
        return False


def default_cache_path(skill_root: str) -> str:
    return os.path.join(skill_root, ".cache", "regions.json")


def get_index(
    cache_path: str,
    key: str = "",
    refresh: bool = False,
    ttl: int = CACHE_TTL_SECONDS,
    transport: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> RegionIndex:
    """优先用缓存；过期或强制刷新时才发请求。

    缓存新鲜时**不需要 key**——这意味着装过一次之后，``--resolve`` 可以完全离线。
    """
    if not refresh and cache_is_fresh(cache_path, ttl=ttl):
        cached = load_index(cache_path)
        if cached is not None:
            return cached
    elif not refresh:
        # 缓存过期但能读（离线场景下比没有强）：仅在拿不到 key 时才回退到它
        stale = load_index(cache_path)
        if not key:
            if stale is not None:
                return stale
            raise AmapError(
                "本地没有行政区划缓存，需要一次在线拉取，但没有可用的 key。"
                "请设置 AMAP_KEY 或配置 config.json",
                action="fatal",
            )
    index = fetch_index(key, transport=transport)
    save_index(index, cache_path)
    return index
