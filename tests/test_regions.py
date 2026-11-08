# -*- coding: utf-8 -*-
"""行政区划解析层测试：全部离线，用人工构造的小树验证逻辑。

不读真实缓存、不发真请求。真实数据的形态取自 2026-11-08 实测（见
``references/api-notes.md``「行政区划索引」一节），但这里只放最小样本。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from amap_poi import regions  # noqa: E402
from amap_poi.client import AmapError  # noqa: E402


def _tree():
    """构造一棵和真实响应同形的小树：1 省 + 1 市 + 3 区县 + 若干街道。"""
    return {
        "status": "1",
        "infocode": "10000",
        "districts": [
            {
                "name": "海南省",
                "adcode": "460000",
                "level": "province",
                "citycode": "",
                "districts": [
                    {
                        "name": "海口市",
                        "adcode": "460100",
                        "level": "city",
                        "citycode": "0898",
                        "districts": [
                            {
                                "name": "龙华区",
                                "adcode": "460106",
                                "level": "district",
                                "citycode": "0898",
                                "districts": [
                                    {
                                        "name": "中山街道",
                                        "adcode": "460106001",
                                        "level": "street",
                                        "districts": [],
                                    }
                                ],
                            },
                            {
                                "name": "美兰区",
                                "adcode": "460108",
                                "level": "district",
                                "citycode": "0898",
                                "districts": [],
                            },
                            {
                                "name": "秀英区",
                                "adcode": "460105",
                                "level": "district",
                                "citycode": "0898",
                                "districts": [],
                            },
                        ],
                    }
                ],
            }
        ],
    }


def _index():
    return regions.RegionIndex(regions.parse_tree(_tree()), fetched_at="2026-11-08T00:00:00")


class TestParseTree:
    def test_flattens_three_levels(self):
        nodes = regions.parse_tree(_tree())
        assert [node.adcode for node in nodes] == ["460000", "460100", "460106", "460108", "460105"]

    def test_street_level_is_dropped(self):
        """街道与城市可能共用 adcode（东莞/中山），留着会让按 adcode 反查有歧义。"""
        nodes = regions.parse_tree(_tree())
        assert all(node.level != "street" for node in nodes)

    def test_parent_links(self):
        nodes = regions.parse_tree(_tree())
        by_code = {node.adcode: node for node in nodes}
        assert by_code["460000"].parent_adcode == ""
        assert by_code["460100"].parent_adcode == "460000"
        assert by_code["460106"].parent_adcode == "460100"

    def test_empty_payload(self):
        assert regions.parse_tree({}) == []
        assert regions.parse_tree({"districts": []}) == []

    def test_malformed_node_is_skipped(self):
        payload = {"districts": [{"name": "坏节点"}]}  # 没有 adcode / level
        assert regions.parse_tree(payload) == []


class TestRegionIndex:
    def test_get_by_adcode(self):
        index = _index()
        assert index.get("460106").name == "龙华区"
        assert index.get("999999") is None

    def test_children(self):
        index = _index()
        names = [node.name for node in index.children(index.get("460100"))]
        assert sorted(names) == sorted(["美兰区", "秀英区", "龙华区"])

    def test_path(self):
        index = _index()
        assert index.path(index.get("460106")) == "海南省 / 海口市 / 龙华区"

    def test_find_by_adcode(self):
        assert [node.adcode for node in _index().find("460106")] == ["460106"]

    def test_find_exact_name(self):
        assert [node.adcode for node in _index().find("龙华区")] == ["460106"]

    def test_find_without_suffix(self):
        """用户说「龙华」也要能找到「龙华区」。"""
        assert [node.adcode for node in _index().find("龙华")] == ["460106"]

    def test_find_compound_full(self):
        """「海口市龙华区」这种完整父子写法必须能唯一定位。"""
        assert [node.adcode for node in _index().find("海口市龙华区")] == ["460106"]

    def test_find_compound_without_suffixes(self):
        """两级都省略后缀：「海口龙华」也要解析出来。"""
        assert [node.adcode for node in _index().find("海口龙华")] == ["460106"]

    def test_find_compound_skips_intermediate_level(self):
        """北京的实际层级是「北京市 → 北京城区 → 朝阳区」，用户不会说中间那层。"""
        nodes = [
            regions.Region("北京市", "110000", "province"),
            regions.Region("北京城区", "110100", "city", "010", "110000"),
            regions.Region("朝阳区", "110105", "district", "010", "110100"),
        ]
        index = regions.RegionIndex(nodes)
        assert [node.adcode for node in index.find("北京市朝阳区")] == ["110105"]

    def test_find_compound_wrong_parent_is_empty(self):
        """父子关系不成立时不能硬凑——「深圳市龙华区」在本树里应查不到。"""
        assert _index().find("深圳市龙华区") == []

    def test_find_compound_survives_duplicate_child(self):
        """重名区县在同一父级下不存在；跨父级时仍必须按父级收敛到唯一。"""
        nodes = [
            regions.Region("广东省", "440000", "province"),
            regions.Region("深圳市", "440300", "city", "0755", "440000"),
            regions.Region("龙华区", "440309", "district", "0755", "440300"),
            regions.Region("海南省", "460000", "province"),
            regions.Region("海口市", "460100", "city", "0898", "460000"),
            regions.Region("龙华区", "460106", "district", "0898", "460100"),
        ]
        index = regions.RegionIndex(nodes)
        assert [node.adcode for node in index.find("海口市龙华区")] == ["460106"]
        assert [node.adcode for node in index.find("深圳市龙华区")] == ["440309"]

    def test_find_unknown_returns_empty(self):
        assert _index().find("不存在的地方") == []

    def test_find_empty_query(self):
        assert _index().find("") == []


class TestAmbiguity:
    def _two_longhua(self):
        nodes = [
            regions.Region("广东省", "440000", "province"),
            regions.Region("深圳市", "440300", "city", "0755", "440000"),
            regions.Region("龙华区", "440309", "district", "0755", "440300"),
            regions.Region("海南省", "460000", "province"),
            regions.Region("海口市", "460100", "city", "0898", "460000"),
            regions.Region("龙华区", "460106", "district", "0898", "460100"),
        ]
        return regions.RegionIndex(nodes)

    def test_duplicate_name_returns_all_candidates(self):
        matches = self._two_longhua().find("龙华区")
        assert [node.adcode for node in matches] == ["440309", "460106"]

    def test_resolve_all_refuses_to_guess(self):
        """重名必须报错让人选，绝不静默挑一个——猜错就是拿到另一个城市的数据。"""
        resolved, notes, errors = regions.resolve_all(
            self._two_longhua(), [("district", "龙华区")]
        )
        assert resolved == []  # 没有产出任何「猜的」结果
        assert notes == []
        assert len(errors) == 1
        assert "440309" in errors[0] and "460106" in errors[0]
        assert "深圳市" in errors[0] and "海口市" in errors[0]

    def test_resolve_all_unique(self):
        index = _index()
        resolved, notes, errors = regions.resolve_all(index, [("district", "龙华区")])
        assert resolved == [("adcode", "460106")]
        assert errors == []
        assert "460106" in notes[0]
        assert "海南省" in notes[0]

    def test_resolve_all_passes_through_others(self):
        resolved, notes, errors = regions.resolve_all(
            _index(), [("city", "海口市"), ("province", "海南省"), ("adcode", "460106")]
        )
        assert resolved == [("city", "海口市"), ("province", "海南省"), ("adcode", "460106")]
        assert notes == [] and errors == []

    def test_resolve_all_missing_name(self):
        resolved, notes, errors = regions.resolve_all(_index(), [("district", "不存在区")])
        assert resolved == []
        assert "--list-regions" in errors[0]

    def test_city_level_match_is_not_accepted_as_district(self):
        """「海口市」被误标成 district 时，不该拿市级节点冒充区县。"""
        resolved, _notes, errors = regions.resolve_all(_index(), [("district", "海口市")])
        assert resolved == []
        assert errors


class TestSerialization:
    def test_roundtrip(self):
        index = _index()
        restored = regions.RegionIndex.from_payload(index.to_payload())
        assert restored is not None
        assert [node.adcode for node in restored.nodes] == [n.adcode for n in index.nodes]
        assert restored.get("460106").parent_adcode == "460100"

    def test_version_mismatch_rejected(self):
        payload = _index().to_payload()
        payload["version"] = 999
        assert regions.RegionIndex.from_payload(payload) is None

    def test_garbage_rejected(self):
        assert regions.RegionIndex.from_payload(None) is None
        assert regions.RegionIndex.from_payload({"nodes": "nope"}) is None
        assert regions.RegionIndex.from_payload({"version": regions.CACHE_VERSION, "nodes": []}) is None


class TestCache:
    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "sub" / "regions.json")
        assert regions.save_index(_index(), path) is True
        restored = regions.load_index(path)
        assert restored is not None and restored.get("460108").name == "美兰区"

    def test_load_missing_returns_none(self, tmp_path):
        assert regions.load_index(str(tmp_path / "nope.json")) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert regions.load_index(str(path)) is None

    def test_freshness(self, tmp_path):
        path = tmp_path / "regions.json"
        path.write_text("{}", encoding="utf-8")
        now = os.path.getmtime(str(path))
        assert regions.cache_is_fresh(str(path), ttl=3600, now=now + 10) is True
        assert regions.cache_is_fresh(str(path), ttl=3600, now=now + 7200) is False

    def test_freshness_missing_file(self, tmp_path):
        assert regions.cache_is_fresh(str(tmp_path / "nope.json")) is False


class TestGetIndex:
    def test_uses_cache_without_network(self, tmp_path):
        path = str(tmp_path / "regions.json")
        regions.save_index(_index(), path)

        def boom(_url):
            raise AssertionError("缓存新鲜时不应发请求")

        index = regions.get_index(path, key="", transport=boom)
        assert index.get("460106").name == "龙华区"

    def test_fetches_when_cache_missing(self, tmp_path):
        path = str(tmp_path / "regions.json")
        calls = []

        def transport(url):
            calls.append(url)
            return _tree()

        index = regions.get_index(path, key="k" * 32, transport=transport)
        assert index.get("460108").name == "美兰区"
        assert len(calls) == 1
        assert os.path.isfile(path), "拉取后应写入缓存"

    def test_refresh_ignores_fresh_cache(self, tmp_path):
        path = str(tmp_path / "regions.json")
        regions.save_index(_index(), path)
        calls = []

        def transport(url):
            calls.append(url)
            return _tree()

        regions.get_index(path, key="k" * 32, refresh=True, transport=transport)
        assert len(calls) == 1

    def test_no_cache_no_key_is_fatal(self, tmp_path):
        with pytest.raises(AmapError) as excinfo:
            regions.get_index(str(tmp_path / "regions.json"), key="")
        assert excinfo.value.action == "fatal"
        assert "AMAP_KEY" in str(excinfo.value)

    def test_stale_cache_used_when_offline(self, tmp_path):
        """缓存过期但没有 key：宁可给旧数据也别报错——行政区划一年也变不了几次。"""
        path = str(tmp_path / "regions.json")
        regions.save_index(_index(), path)
        old = os.path.getmtime(path) - regions.CACHE_TTL_SECONDS - 1000
        os.utime(path, (old, old))
        index = regions.get_index(path, key="")
        assert index.get("460106").name == "龙华区"

    def test_api_error_raises_amap_error(self, tmp_path):
        def transport(_url):
            return {"status": "0", "infocode": "10001", "info": "INVALID_USER_KEY"}

        with pytest.raises(AmapError) as excinfo:
            regions.get_index(str(tmp_path / "r.json"), key="bad", transport=transport)
        assert excinfo.value.infocode == "10001"


class TestBuildUrl:
    def test_contains_required_params(self):
        url = regions.build_url("k" * 32)
        assert "config/district" in url
        assert "subdistrict=3" in url
        assert "extensions=base" in url


class TestListNote:
    """城市没有区县层时（东莞/中山/济源），要说明「再拆也没用」。"""

    def test_city_without_districts(self, capsys):
        """东莞这类城市下级只有街道，parse_tree 丢掉街道后就没有下级了。

        真实数据形态：东莞市 441900 下挂 32 个街道，全部与市共用 adcode。
        """
        from amap_poi import cli

        payload = {
            "districts": [
                {
                    "name": "广东省",
                    "adcode": "440000",
                    "level": "province",
                    "districts": [
                        {
                            "name": "东莞市",
                            "adcode": "441900",
                            "level": "city",
                            "districts": [
                                {"name": "长安镇", "adcode": "441900", "level": "street"},
                                {"name": "虎门镇", "adcode": "441900", "level": "street"},
                            ],
                        }
                    ],
                }
            ]
        }
        index = regions.RegionIndex(regions.parse_tree(payload))
        assert cli.list_regions(index, "东莞市") == 0
        out = capsys.readouterr().out
        assert "没有下级行政单位" in out
        assert "加关键词变体" in out

    def test_lists_districts(self, capsys):
        from amap_poi import cli

        assert cli.list_regions(_index(), "海口市") == 0
        out = capsys.readouterr().out
        for name in ("龙华区", "美兰区", "秀英区"):
            assert name in out
        assert "460106" in out

    def test_unknown_city_is_fatal(self, capsys):
        from amap_poi import cli
        from amap_poi.report import EXIT_FATAL

        assert cli.list_regions(_index(), "火星市") == EXIT_FATAL
        assert "没找到" in capsys.readouterr().err
