# -*- coding: utf-8 -*-
"""collect 层单元测试：注入假 client，不发真实请求。

重点回归（都是 2026-11-07 真机运行踩出来的）：
  * 中间页不满 offset 条不能当末页
  * 触顶判据只看 raw >= 200，不看 count>raw（否则小结果集全误报）
  * --max-records 达到后不应把「剩余单元」也标成失败
  * 预算刹车要如实记录停在哪个单元
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from amap_poi.client import AmapError, SearchPage  # noqa: E402
from amap_poi.collect import CollectResult, Collector, estimate_requests  # noqa: E402


def raw(poi_id, name=None):
    return {
        "id": poi_id,
        "name": name or ("示例店" + poi_id),
        "tel": "0898-0000000",
        "location": "110.1,20.1",
        "pname": "海南省",
        "cityname": "海口市",
        "adname": "龙华区",
        "adcode": "460106",
        "type": "汽车服务;汽车维修",
        "typecode": "010104",
        "biz_ext": {"rating": "4.5", "open_time": []},
    }


class FakeClient(object):
    """按 (keyword, region, page) 返回预置页；未预置则视为空页。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search_page(self, keyword, region, page=1, offset=25):
        self.calls.append((keyword, region, page))
        item = self.pages.get((keyword, region, page))
        if isinstance(item, Exception):
            raise item
        pois, count = item if item else ([], 0)
        return SearchPage(keyword, region, page, pois, count, "10000")


def build(pages, **kwargs):
    client = FakeClient(pages)
    kwargs.setdefault("offset", 25)
    collector = Collector(client, **kwargs)
    return collector, client


# ---------------------------------------------------------------- 主流程


class TestRun:
    def test_single_unit_collects_all_pages(self):
        pages = {
            ("汽修厂", "海口市", 1): ([raw("A"), raw("B")], 2),
            ("汽修厂", "海口市", 2): ([], 0),
        }
        collector, client = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.unique == 2
        assert result.raw_total == 2
        assert result.requests == 2
        assert result.units[0].status == "ok"
        assert not result.stopped_early

    def test_midway_short_page_keeps_going(self):
        """中间页只返回 1 条，后面还有数据 —— 不能提前停。"""
        pages = {
            ("汽修厂", "海口市", 1): ([raw("A")], 10),
            ("汽修厂", "海口市", 2): ([raw("B")], 10),
            ("汽修厂", "海口市", 3): ([], 0),
        }
        collector, client = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.unique == 2
        assert len(client.calls) == 3

    def test_two_keywords_two_regions(self):
        pages = {
            ("汽修厂", "海口市", 1): ([raw("A"), raw("B")], 2),
            ("汽修厂", "海口市", 2): ([], 0),
            ("汽修厂", "460106", 1): ([raw("C")], 1),
            ("汽修厂", "460106", 2): ([], 0),
            ("汽车维修", "海口市", 1): ([raw("D")], 1),
            ("汽车维修", "海口市", 2): ([], 0),
            ("汽车维修", "460106", 1): ([raw("E")], 1),
            ("汽车维修", "460106", 2): ([], 0),
        }
        collector, _ = build(pages)
        result = collector.run(["汽修厂", "汽车维修"], ["海口市", "460106"])
        assert result.unique == 5
        assert len(result.units) == 4

    def test_empty_result_is_ok_not_failure(self):
        collector, _ = build({})
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.unique == 0
        assert result.units[0].status == "ok"
        assert result.units[0].raw == 0
        assert not result.fatal
        assert not result.failed_units


# ---------------------------------------------------------------- 去重


class TestDedup:
    def test_same_poi_across_units_merged(self):
        pages = {
            ("汽修厂", "海口市", 1): ([raw("A"), raw("B")], 2),
            ("汽修厂", "海口市", 2): ([], 0),
            ("汽车维修", "海口市", 1): ([raw("B"), raw("C")], 2),
            ("汽车维修", "海口市", 2): ([], 0),
        }
        collector, _ = build(pages)
        result = collector.run(["汽修厂", "汽车维修"], ["海口市"])
        assert result.unique == 3
        by_id = {r["poi_id"]: r for r in result.records}
        assert by_id["B"]["keyword"] == "汽修厂;汽车维修"
        assert by_id["A"]["keyword"] == "汽修厂"

    def test_new_count_per_unit(self):
        pages = {
            ("汽修厂", "海口市", 1): ([raw("A")], 2),
            ("汽修厂", "海口市", 2): ([], 0),
            ("汽车维修", "海口市", 1): ([raw("A")], 2),
            ("汽车维修", "海口市", 2): ([], 0),
        }
        collector, _ = build(pages)
        result = collector.run(["汽修厂", "汽车维修"], ["海口市"])
        assert result.units[0].new == 1
        assert result.units[1].new == 0  # 全是重复
        assert result.units[1].raw == 1

    def test_record_without_id_skipped(self):
        bad = raw("")  # 没有 id
        pages = {("汽修厂", "海口市", 1): ([bad, raw("A")], 2),
                 ("汽修厂", "海口市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.unique == 1


# ---------------------------------------------------------------- 触顶判定


class TestHitCap:
    def test_under_200_is_not_cap(self):
        """文昌 106 条 count=108 是自然到底，不能报触顶。"""
        pois = [raw("P%03d" % i) for i in range(106)]
        pages = {("汽修厂", "文昌市", 1): (pois, 108),
                 ("汽修厂", "文昌市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["文昌市"])
        assert result.units[0].raw == 106
        assert not result.units[0].hit_cap
        assert result.hit_cap_units == []

    def test_200_or_more_is_cap(self):
        pois = [raw("P%03d" % i) for i in range(200)]
        pages = {("汽修厂", "成都市", 1): (pois, 572),
                 ("汽修厂", "成都市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["成都市"])
        assert result.units[0].hit_cap
        assert len(result.hit_cap_units) == 1

    def test_count_much_bigger_but_few_rows_is_not_cap(self):
        """count 虚高不能当触顶依据（否则退出码永远 2）。"""
        pages = {("汽修厂", "某市", 1): ([raw("A")], 999),
                 ("汽修厂", "某市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["某市"])
        assert not result.units[0].hit_cap


# ---------------------------------------------------------------- 预算


class TestBudget:
    def test_max_records_stops_collection(self):
        page1 = [raw("P%03d" % i) for i in range(25)]
        page2 = [raw("Q%03d" % i) for i in range(25)]
        pages = {
            ("汽修厂", "海口市", 1): (page1, 500),
            ("汽修厂", "海口市", 2): (page2, 500),
        }
        collector, _ = build(pages, max_records=30)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.unique == 30
        assert result.stopped_early
        assert "条数上限" in result.stop_reason

    def test_max_records_marks_unit_partial(self):
        pois = [raw("P%03d" % i) for i in range(25)]
        pages = {("汽修厂", "海口市", 1): (pois, 500)}
        collector, _ = build(pages, max_records=10)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.units[0].status == "partial"
        assert result.units[0].raw == 25  # 本页取完才截断

    def test_remaining_units_not_marked_failed(self):
        """达上限后剩余单元不应出现在 failed_units 里。"""
        pois = [raw("P%03d" % i) for i in range(200)]
        pages = {("汽修厂", "海口市", 1): (pois, 500)}
        collector, _ = build(pages, max_records=5)
        result = collector.run(["汽修厂", "汽车维修"], ["海口市", "460106"])
        assert len(result.failed_units) == 1
        assert result.failed_units[0].status == "partial"

    def test_max_requests_stops(self):
        pois = [raw("P%03d" % i) for i in range(25)]
        pages = {("汽修厂", "海口市", i): (pois, 500) for i in range(1, 10)}
        collector, _ = build(pages, max_requests=2)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.requests == 2
        assert result.stopped_early
        assert "请求上限" in result.stop_reason

    def test_max_pages_fuse_marks_partial(self):
        pois = [raw("P%03d" % i) for i in range(25)]
        pages = {("汽修厂", "海口市", i): (pois, 9999) for i in range(1, 30)}
        collector, _ = build(pages, max_pages=3)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.units[0].status == "partial"
        assert "页数" in result.units[0].error


# ---------------------------------------------------------------- 错误


class TestErrors:
    def test_skip_error_recorded_and_continues(self):
        pages = {
            ("汽修厂", "海口市", 1): AmapError("参数错误", infocode="20001", action="skip"),
            ("汽车维修", "海口市", 1): ([raw("A")], 1),
            ("汽车维修", "海口市", 2): ([], 0),
        }
        collector, _ = build(pages)
        result = collector.run(["汽修厂", "汽车维修"], ["海口市"])
        assert result.units[0].status == "failed"
        assert result.unique == 1  # 第二个单元照常跑
        assert not result.fatal
        assert len(result.errors) == 1
        assert result.errors[0]["infocode"] == "20001"

    def test_fatal_error_stops_everything(self):
        pages = {
            ("汽修厂", "海口市", 1): AmapError("key 无效", infocode="10001", action="fatal"),
        }
        collector, client = build(pages)
        result = collector.run(["汽修厂", "汽车维修"], ["海口市", "460106"])
        assert result.fatal
        assert result.stopped_early
        assert len(client.calls) == 1  # 后续单元没再发请求

    def test_error_message_keeps_infocode(self):
        pages = {("汽修厂", "海口市", 1): AmapError("配额超限", infocode="10003", action="fatal")}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert "10003" in result.errors[0]["message"]


# ---------------------------------------------------------------- 体检指标


class TestMetrics:
    def test_tel_empty_rate(self):
        a = raw("A")
        b = raw("B")
        b["tel"] = []
        pages = {("汽修厂", "海口市", 1): ([a, b], 2),
                 ("汽修厂", "海口市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.tel_empty_rate == 0.5

    def test_tel_empty_rate_zero_when_no_records(self):
        collector, _ = build({})
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.tel_empty_rate == 0.0

    def test_requests_counted(self):
        pages = {("汽修厂", "海口市", 1): ([raw("A")], 1),
                 ("汽修厂", "海口市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.requests == 2
        assert result.units[0].requests == 2
        assert result.units[0].pages == 2

    def test_infocodes_counted(self):
        pages = {("汽修厂", "海口市", 1): ([raw("A")], 1),
                 ("汽修厂", "海口市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert result.units[0].infocodes == {"10000": 2}


# ---------------------------------------------------------------- 预估


class TestEstimate:
    def test_basic(self):
        # 1 单元 × (200//25 + 2) = 10
        assert estimate_requests(1, 1) == 10

    def test_multiplication(self):
        assert estimate_requests(2, 3) == 60

    def test_zero_when_no_units(self):
        assert estimate_requests(0, 3) == 0
        assert estimate_requests(3, 0) == 0

    def test_small_offset_more_requests(self):
        assert estimate_requests(1, 1, offset=10) == 22

    def test_max_records_reduces_estimate(self):
        assert estimate_requests(5, 5, max_records=50) < estimate_requests(5, 5)


# ---------------------------------------------------------------- as_dict


class TestUnitDict:
    def test_fields_present(self):
        pages = {("汽修厂", "海口市", 1): ([raw("A")], 1),
                 ("汽修厂", "海口市", 2): ([], 0)}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        payload = result.units[0].as_dict()
        for key in ("keyword", "region", "status", "requests", "raw", "new",
                    "pages", "hit_cap", "count_hint", "infocodes"):
            assert key in payload
        assert "error" not in payload  # 正常单元不带 error 字段

    def test_error_field_only_when_failed(self):
        pages = {("汽修厂", "海口市", 1): AmapError("x", infocode="20001", action="skip")}
        collector, _ = build(pages)
        result = collector.run(["汽修厂"], ["海口市"])
        assert "error" in result.units[0].as_dict()
