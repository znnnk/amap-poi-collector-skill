# -*- coding: utf-8 -*-
"""采集编排：查询单元 × 分页 × 去重 × 触顶判定 × 预算。

这一层不认识 HTTP，也不写文件；它把 :mod:`amap_poi.client` 与
:mod:`amap_poi.normalize` 接起来，产出可被 export / report 消费的纯数据结构。
"""

import time
from typing import Any, Dict, List, Optional, Sequence

from .client import KNOWN_ITEM_CAP, AmapError, AmapClient
from .normalize import as_poi_list, merge_keyword, normalize_poi

#: 单单元页数保险丝：正常 offset=25 时 9 页内结束，这里按 offset 反算再留余量
SAFETY_EXTRA_PAGES = 5


class UnitResult(object):
    """一个查询单元（1 关键词 × 1 地区）的执行结果。"""

    __slots__ = (
        "keyword",
        "region",
        "requests",
        "raw",
        "new",
        "pages",
        "hit_cap",
        "count_hint",
        "infocodes",
        "status",
        "error",
    )

    def __init__(self, keyword: str, region: str):
        self.keyword = keyword
        self.region = region
        self.requests = 0
        self.raw = 0
        self.new = 0
        self.pages = 0
        self.hit_cap = False
        self.count_hint = 0
        self.infocodes: Dict[str, int] = {}
        self.status = "pending"  # ok / partial / failed
        self.error = ""

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "keyword": self.keyword,
            "region": self.region,
            "status": self.status,
            "requests": self.requests,
            "raw": self.raw,
            "new": self.new,
            "pages": self.pages,
            "hit_cap": self.hit_cap,
            "count_hint": self.count_hint,
            "infocodes": dict(self.infocodes),
        }
        if self.error:
            payload["error"] = self.error
        return payload


class CollectResult(object):
    """整次运行的结果集合。"""

    def __init__(self):
        self.records: List[Dict[str, str]] = []
        self.units: List[UnitResult] = []
        self.errors: List[Dict[str, str]] = []
        self.requests = 0
        self.stopped_early = False
        self.stop_reason = ""

    # -- 汇总 ------------------------------------------------------

    @property
    def unique(self) -> int:
        return len(self.records)

    @property
    def raw_total(self) -> int:
        return sum(unit.raw for unit in self.units)

    @property
    def tel_empty_rate(self) -> float:
        if not self.records:
            return 0.0
        empty = sum(1 for record in self.records if not record.get("tel"))
        return round(empty / float(len(self.records)), 4)

    @property
    def hit_cap_units(self) -> List[UnitResult]:
        return [unit for unit in self.units if unit.hit_cap]

    @property
    def failed_units(self) -> List[UnitResult]:
        """真正出错的单元。

        注意 ``status == "pending"``（还没轮到就因条数/请求上限停了）**不算失败**——
        那是用户主动设了上限，不是采集出了问题；把它算进来会让每次带 ``-n`` 的运行
        都报「有单元失败」，反而掩盖真正的错误。
        """
        return [unit for unit in self.units if unit.status in ("failed", "partial")]

    @property
    def fatal(self) -> bool:
        return any(item.get("action") == "fatal" for item in self.errors)


class Collector(object):
    """按查询单元串行采集。

    :param client: 已构造的 :class:`AmapClient`
    :param max_records: 本次总条数上限；``0`` 表示不限制（不建议）
    :param max_requests: 本次请求数硬刹车；``0`` 表示不限制
    :param offset: 每页条数
    :param sleep: 注入点，测试用
    """

    def __init__(
        self,
        client: AmapClient,
        max_records: int = 0,
        max_requests: int = 0,
        offset: int = 25,
        sleep=None,
        max_pages: int = 0,
    ):
        self.client = client
        self.max_records = max(0, int(max_records or 0))
        self.max_requests = max(0, int(max_requests or 0))
        self.offset = max(1, int(offset))
        # 保险丝按 offset 反算：实测可检索窗口约到第 200 条为止，
        # 再留 SAFETY_EXTRA_PAGES 页余量，避免把真末页当异常。
        self.max_pages = max(1, int(max_pages)) if max_pages else (
            KNOWN_ITEM_CAP // self.offset + 1 + SAFETY_EXTRA_PAGES
        )
        self._sleep = sleep or time.sleep

    # -- 主流程 ----------------------------------------------------

    def run(
        self,
        keywords: Sequence[str],
        regions: Sequence[str],
        on_unit_start=None,
        on_unit_done=None,
    ) -> CollectResult:
        result = CollectResult()
        index: Dict[str, Dict[str, str]] = {}
        order: List[str] = []
        self._stopped = False
        self._stop_reason = ""

        for keyword in keywords:
            for region in regions:
                if self._budget_hit(result, order, index):
                    break
                unit = UnitResult(keyword, region)
                result.units.append(unit)
                if on_unit_start:
                    on_unit_start(unit)
                try:
                    self._run_unit(unit, result, index, order, keyword, region)
                except AmapError as exc:
                    unit.status = "failed"
                    unit.error = str(exc)
                    result.errors.append(
                        {
                            "unit": "%s@%s" % (region, keyword),
                            "infocode": exc.infocode or "",
                            "action": exc.action,
                            "message": str(exc),
                        }
                    )
                    if exc.action == "fatal":
                        self._stopped = True
                        self._stop_reason = str(exc)
                if on_unit_done:
                    on_unit_done(unit)
                if self._stopped:
                    break
            if self._stopped:
                break

        result.records = [index[key] for key in order]
        result.stopped_early = self._stopped
        result.stop_reason = self._stop_reason
        return result

    # -- 预算 ------------------------------------------------------

    def _budget_hit(
        self,
        result: CollectResult,
        order: List[str],
        index: Dict[str, Dict[str, str]],
    ) -> bool:
        if self.max_records and len(order) >= self.max_records:
            self._stopped = True
            self._stop_reason = self._stop_reason or "已达条数上限 %d" % self.max_records
            return True
        if self.max_requests and result.requests >= self.max_requests:
            self._stopped = True
            self._stop_reason = self._stop_reason or "已达请求上限 %d" % self.max_requests
            return True
        return False

    # -- 单元执行 --------------------------------------------------

    def _run_unit(
        self,
        unit: UnitResult,
        result: CollectResult,
        index: Dict[str, Dict[str, str]],
        order: List[str],
        keyword: str,
        region: str,
    ) -> None:
        page = 1
        truncated = False

        while page <= self.max_pages:
            if self.max_requests and result.requests >= self.max_requests:
                truncated = True
                self._stopped = True
                self._stop_reason = self._stop_reason or "已达请求上限 %d" % self.max_requests
                break
            if self.max_records and len(order) >= self.max_records:
                truncated = True
                self._stopped = True
                self._stop_reason = self._stop_reason or "已达条数上限 %d" % self.max_records
                break

            request = self.client.search_page(keyword, region, page, self.offset)
            unit.requests += 1
            unit.pages += 1
            result.requests += 1
            unit.infocodes[request.infocode] = unit.infocodes.get(request.infocode, 0) + 1

            raw_pois = as_poi_list({"pois": request.pois})
            if raw_pois is None:
                raise AmapError("pois 结构异常，无法判断是否有下一页", action="skip")

            if not raw_pois:
                break  # 唯一终止条件：本页 0 条

            unit.raw += len(raw_pois)
            unit.count_hint = max(unit.count_hint, request.count)

            for raw in raw_pois:
                record = normalize_poi(raw, keyword=keyword)
                poi_id = record.get("poi_id")
                if not poi_id:
                    continue
                if poi_id in index:
                    merge_keyword(index[poi_id], keyword)
                    continue
                index[poi_id] = record
                order.append(poi_id)
                unit.new += 1
                if self.max_records and len(order) >= self.max_records:
                    truncated = True
                    self._stopped = True
                    self._stop_reason = self._stop_reason or (
                        "已达条数上限 %d" % self.max_records
                    )
                    break

            if truncated:
                break
            # 只认「本页 0 条」为终止信号。
            # 「本页不满 offset 条」虽然能省 1 次请求，但它不是文档化契约：
            # 一旦服务端在中间页返回短页，就会静默截断。为省一次请求不值得。
            page += 1

        # 触顶判定：这是**启发式**，不是协议事实。
        # 2026-11-07 实测天花板并不固定在 200，且总在 200 以上：
        #   海口/汽修厂 offset=25 → 225 条后空页
        #   海口/汽修厂 offset=20 → 220 条后空页
        #   海口/汽修厂 offset=10 → 210 条后空页
        #   成都/汽修厂 offset=25 → 200 条后空页
        #   成都/汽修厂 offset=10 → 200 条后空页
        #   文昌/汽修厂 offset=25 → 106 条后空页（count=108，属于数据本身少，不是触顶）
        # 所以判据只能是「取到量 ≥ 200」：低于 200 说明是自然翻到底，
        # 达到 200 才无法区分「刚好取完」和「被服务端截断」。
        # 不能拿 count>raw 当判据——count 永远比可取得的略大（文昌 108 vs 106），
        # 那样会让每个单元都误报触顶，退出码永远是 2。
        unit.hit_cap = unit.raw >= KNOWN_ITEM_CAP
        if unit.status == "pending":
            if truncated:
                unit.status = "partial"
            elif page > self.max_pages:
                # 撞上安全丝而非空页：数据可能没取完，别报 ok
                unit.status = "partial"
                unit.error = "超过安全页数上限 %d 页仍未翻空" % self.max_pages
            else:
                unit.status = "ok"


def estimate_requests(
    keyword_count: int, region_count: int, max_records: int = 0, offset: int = 25
) -> int:
    """预估本次请求数上限。

    单单元最坏情况：可检索窗口到第 200 条左右为止，再补一次空页确认到底。
    ``offset=25`` → 9 页有数据 + 1 页空 = 10 次（实测：海口/成都都是 10 次内结束）。
    ``offset=10`` → 21 页有数据 + 1 页空 = 22 次（实测海口 22 次）。

    ``max_records`` 小于总容量时按有效需求折算，避免把用户吓住。
    """
    units = max(0, keyword_count) * max(0, region_count)
    if not units:
        return 0
    per_unit = KNOWN_ITEM_CAP // max(1, offset) + 2  # 8 + 2 = 10
    total = units * per_unit
    if max_records and max_records < units * KNOWN_ITEM_CAP:
        effective_units = max(1, (max_records + KNOWN_ITEM_CAP - 1) // KNOWN_ITEM_CAP)
        total = min(total, effective_units * per_unit + units)
    return total
