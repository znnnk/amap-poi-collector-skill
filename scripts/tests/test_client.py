# -*- coding: utf-8 -*-
"""client 层单元测试：注入假 transport，不发真实请求。

覆盖点：
  * URL 参数构造（v3/text + citylimit + extensions=all）
  * HTTP 200 但 status=0 的错误要被识别（这是旧项目的核心 bug）
  * infocode 三分类：fatal 直接抛、retry 退避后成功、skip 直接抛
  * 分页：翻到空页为止；页数保险丝
  * offset 越界被夹到 1..25
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from amap_poi.client import (  # noqa: E402
    FATAL_CODES,
    MAX_OFFSET,
    RETRY_CODES,
    SKIP_CODES,
    AmapClient,
    AmapError,
    classify,
    describe,
    http_get_json,
)


def poi(poi_id):
    return {"id": poi_id, "name": "示例店" + poi_id, "location": "110.1,20.1"}


class FakeTransport(object):
    """按脚本返回预置响应，并记录每次请求的 URL。"""

    def __init__(self, script):
        self.script = list(script)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if not self.script:
            raise AssertionError("transport 被调用了超出预期的次数：%s" % url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(pois, count=0, infocode="10000", status="1"):
    return {
        "status": status,
        "info": "OK" if status == "1" else "ERR",
        "infocode": infocode,
        "count": str(count),
        "pois": pois,
    }


def build(script, **kwargs):
    transport = FakeTransport(script)
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("sleep", lambda _s: None)
    client = AmapClient("fake-key", transport=transport, **kwargs)
    return client, transport


# ---------------------------------------------------------------- 错误码


class TestClassify:
    @pytest.mark.parametrize(
        "code",
        [
            "10001", "10002", "10003", "10005", "10006", "10007", "10009",
            "10010", "10011", "10012", "10013", "10026", "10041", "10044",
            "10045", "40000", "40002", "40003",
        ],
    )
    def test_fatal(self, code):
        assert classify(code) == "fatal"

    @pytest.mark.parametrize(
        "code",
        ["10004", "10014", "10015", "10016", "10017", "10019", "10020", "10021", "10029"],
    )
    def test_retry(self, code):
        assert classify(code) == "retry"

    @pytest.mark.parametrize(
        "code", ["10008", "20000", "20001", "20002", "20003", "20011", "20012", "20800"]
    )
    def test_skip(self, code):
        assert classify(code) == "skip"

    def test_unknown_defaults_to_skip(self):
        assert classify("99999") == "skip"

    @pytest.mark.parametrize("code", ["30001", "30002", "32000", "32203"])
    def test_engine_errors_are_retry_by_prefix(self, code):
        # 官方文档把 3 开头的引擎错误归为一类，没有逐码列出
        assert classify(code) == "retry"

    def test_describe_known_and_unknown(self):
        assert "key" in describe("10001")
        assert describe("99999") == ""
        assert describe("30001") == describe("32000") != ""

    def test_no_code_is_classified_twice(self):
        # 同一个 infocode 不能同时出现在两张表里（否则行为取决于查表顺序）
        overlap = (set(FATAL_CODES) & set(RETRY_CODES)) | (
            set(FATAL_CODES) & set(SKIP_CODES)
        ) | (set(RETRY_CODES) & set(SKIP_CODES))
        assert overlap == set()

    def test_every_code_has_description(self):
        for table in (FATAL_CODES, RETRY_CODES, SKIP_CODES):
            for code, text in table.items():
                assert text.strip(), code


# ---------------------------------------------------------------- URL


class TestBuildUrl:
    def test_params(self):
        client, transport = build([page([poi("A")])])
        client.search_page("汽修厂", "海口市", page=2, offset=25)
        url = transport.urls[0]
        assert url.startswith("https://restapi.amap.com/v3/place/text?")
        assert "keywords=" in url
        assert "city=" in url
        assert "citylimit=true" in url
        assert "offset=25" in url
        assert "page=2" in url
        assert "extensions=all" in url

    def test_chinese_keyword_is_encoded(self):
        client, transport = build([page([poi("A")])])
        client.search_page("汽修厂", "海口市")
        assert "%E6%B1%BD%E4%BF%AE%E5%8E%82" in transport.urls[0]


# ---------------------------------------------------------------- 错误识别


class TestErrorHandling:
    def test_status_zero_http_200_raises(self):
        """核心回归：HTTP 200 但 status=0 必须报错，不能当成空结果。"""
        client, _ = build(
            [{"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001", "pois": []}]
        )
        with pytest.raises(AmapError) as info:
            client.search_page("汽修厂", "海口市")
        assert info.value.infocode == "10001"
        assert info.value.action == "fatal"

    def test_fatal_not_retried(self):
        client, transport = build(
            [{"status": "0", "info": "x", "infocode": "10001", "pois": []}]
        )
        with pytest.raises(AmapError):
            client.search_page("汽修厂", "海口市")
        assert len(transport.urls) == 1  # 没有重试

    def test_retry_then_success(self):
        client, transport = build(
            [
                {"status": "0", "info": "TOO_FREQUENT", "infocode": "10004", "pois": []},
                page([poi("A")]),
            ]
        )
        result = client.search_page("汽修厂", "海口市")
        assert len(result.pois) == 1
        assert len(transport.urls) == 2

    def test_retry_exhausted_raises(self):
        err = {"status": "0", "info": "TOO_FREQUENT", "infocode": "10004", "pois": []}
        client, transport = build([err, err, err, err], max_retries=2)
        with pytest.raises(AmapError) as info:
            client.search_page("汽修厂", "海口市")
        assert info.value.action == "retry"
        assert len(transport.urls) == 3  # 1 次 + 2 次重试

    def test_skip_code_raises_immediately(self):
        client, transport = build(
            [{"status": "0", "info": "bad", "infocode": "20001", "pois": []}]
        )
        with pytest.raises(AmapError) as info:
            client.search_page("汽修厂", "海口市")
        assert info.value.action == "skip"
        assert len(transport.urls) == 1

    def test_empty_pois_is_not_error(self):
        client, _ = build([page([], count=0)])
        result = client.search_page("汽修厂", "海口市")
        assert result.is_empty
        assert result.pois == []

    def test_pois_given_as_empty_dict(self):
        """高德偶尔把空数组序列化成 {}，必须当空处理而不是崩。"""
        client, _ = build([{"status": "1", "infocode": "10000", "count": "0", "pois": {}}])
        result = client.search_page("汽修厂", "海口市")
        assert result.is_empty

    def test_pois_wrong_shape_raises_skip(self):
        client, _ = build([{"status": "1", "infocode": "10000", "pois": "oops"}])
        with pytest.raises(AmapError) as info:
            client.search_page("汽修厂", "海口市")
        assert info.value.action == "skip"

    def test_non_dict_payload_retried(self):
        client, transport = build([["not", "a", "dict"], page([poi("A")])])
        result = client.search_page("汽修厂", "海口市")
        assert len(result.pois) == 1
        assert len(transport.urls) == 2


# ---------------------------------------------------------------- 分页


class TestIterAll:
    def test_stops_on_empty_page(self):
        client, transport = build(
            [
                page([poi("A"), poi("B")], count=100),
                page([poi("C")], count=100),
                page([], count=0),
            ]
        )
        pages = list(client.iter_all("汽修厂", "海口市", offset=25, max_pages=10))
        assert [len(p.pois) for p in pages] == [2, 1, 0]
        assert len(transport.urls) == 3

    def test_midway_short_page_does_not_stop(self):
        """回归：中间页不满 offset 条时**不能**推断到底，后面可能还有数据。"""
        client, transport = build(
            [
                page([poi("A")], count=100),     # 1 条，不满 25
                page([poi("B")], count=100),     # 还有
                page([], count=0),
            ]
        )
        pages = list(client.iter_all("汽修厂", "海口市", offset=25, max_pages=10))
        assert sum(len(p.pois) for p in pages) == 2
        assert len(transport.urls) == 3

    def test_max_pages_fuse(self):
        full = [poi("A")] * 25
        client, transport = build([page(full, count=999) for _ in range(5)])
        pages = list(client.iter_all("汽修厂", "海口市", offset=25, max_pages=3))
        assert len(pages) == 3
        assert len(transport.urls) == 3

    def test_page_numbers_increment(self):
        client, transport = build(
            [page([poi("A")]), page([])]
        )
        list(client.iter_all("汽修厂", "海口市"))
        assert "page=1" in transport.urls[0]
        assert "page=2" in transport.urls[1]


# ---------------------------------------------------------------- offset


class TestOffsetClamp:
    def test_offset_above_max_is_clamped(self):
        client, transport = build([page([])])
        client.search_page("汽修厂", "海口市", offset=100)
        assert "offset=%d" % MAX_OFFSET in transport.urls[0]

    def test_offset_zero_becomes_one(self):
        client, transport = build([page([])])
        client.search_page("汽修厂", "海口市", offset=0)
        assert "offset=1" in transport.urls[0]


# ---------------------------------------------------------------- 构造


class TestConstruct:
    def test_empty_key_rejected(self):
        with pytest.raises(AmapError):
            AmapClient("")

    def test_key_is_stripped(self):
        client, _ = build([page([])])
        assert client.key == "fake-key"


# ---------------------------------------------------------------- 传输层


class TestHttpGetJson:
    def test_bad_url_raises_amap_error(self):
        with pytest.raises(AmapError):
            http_get_json("http://127.0.0.1:1/nope", timeout=1)
