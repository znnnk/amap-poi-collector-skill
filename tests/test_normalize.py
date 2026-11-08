# -*- coding: utf-8 -*-
"""normalize 层单元测试：全部离线，不发请求。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from amap_poi.normalize import (  # noqa: E402
    RECORD_FIELDS,
    as_poi_list,
    as_text,
    collect_region_errors,
    merge_keyword,
    normalize_poi,
    parse_region,
    split_location,
)

#: 真实结构、假数据（字段形态照 2026-11-07 实测，不含真实商户信息）
RAW_POI = {
    "id": "B0TEST0001",
    "name": "示例汽车修理厂",
    "type": "汽车服务;汽车维修",
    "typecode": "010104",
    "tel": "0898-00000000;13800000000",
    "address": "示例路 1 号",
    "pname": "海南省",
    "cityname": "海口市",
    "adname": "龙华区",
    "adcode": "460106",
    "location": "110.33119,20.031971",
    "tag": [],
    "business_area": [],
    "website": [],
    "email": [],
    "biz_ext": {"rating": "4.7", "open_time": "08:30-18:00", "cost": []},
}

RAW_MINIMAL = {
    "id": "B0TEST0002",
    "name": "空字段样例",
    "tel": [],
    "location": [],
    "tag": [],
    "biz_ext": [],
}


class TestAsText:
    @pytest.mark.parametrize("value", [[], None, "", {}, (), "[]"])
    def test_empty_shapes_become_empty_string(self, value):
        assert as_text(value) == ""

    def test_list_joined_with_semicolon(self):
        assert as_text(["周一", "周二"]) == "周一;周二"

    def test_list_drops_empty_items(self):
        assert as_text(["", "周一", []]) == "周一"

    def test_dict_serialized(self):
        assert as_text({"a": "1", "b": []}) == "a:1"

    def test_string_stripped(self):
        assert as_text("  海口市  ") == "海口市"

    def test_semicolon_in_user_data_preserved(self):
        assert as_text("0898-1;139") == "0898-1;139"


class TestSplitLocation:
    def test_valid(self):
        assert split_location("110.33119,20.031971") == ("110.33119", "20.031971")

    @pytest.mark.parametrize("value", [[], "", "abc", "110.3", "1,2,3", ",", None])
    def test_invalid_returns_empty_pair(self, value):
        assert split_location(value) == ("", "")


class TestNormalizePoi:
    def test_all_fields_present_and_strings(self):
        record = normalize_poi(RAW_POI, keyword="汽修厂")
        assert tuple(record.keys()) == RECORD_FIELDS
        assert all(isinstance(v, str) for v in record.values())

    def test_telephone_preserved_as_is(self):
        record = normalize_poi(RAW_POI)
        assert record["tel"] == "0898-00000000;13800000000"

    def test_location_split(self):
        record = normalize_poi(RAW_POI)
        assert (record["lng"], record["lat"]) == ("110.33119", "20.031971")

    def test_type_label_includes_typecode(self):
        assert normalize_poi(RAW_POI)["type"] == "汽车服务;汽车维修(010104)"

    def test_biz_ext_scalars_extracted(self):
        record = normalize_poi(RAW_POI)
        assert record["rating"] == "4.7"
        assert record["open_time"] == "08:30-18:00"

    def test_empty_list_fields_never_leak_brackets(self):
        record = normalize_poi(RAW_MINIMAL)
        for field, value in record.items():
            assert "[]" not in value, "字段 %s 泄漏了字面量 []" % field

    def test_missing_fields_default_to_empty(self):
        record = normalize_poi({"id": "X"}, keyword="kw")
        assert record["name"] == ""
        assert record["keyword"] == "kw"

    def test_none_raw_is_tolerated(self):
        record = normalize_poi(None)
        assert record["poi_id"] == ""


class TestMergeKeyword:
    def test_append_new(self):
        record = {"keyword": "汽修厂"}
        merge_keyword(record, "汽车维修")
        assert record["keyword"] == "汽修厂;汽车维修"

    def test_no_duplicate(self):
        record = {"keyword": "汽修厂"}
        merge_keyword(record, "汽修厂")
        assert record["keyword"] == "汽修厂"

    def test_into_empty(self):
        record = {"keyword": ""}
        merge_keyword(record, "汽修厂")
        assert record["keyword"] == "汽修厂"

    def test_blank_keyword_ignored(self):
        record = {"keyword": "汽修厂"}
        merge_keyword(record, "")
        assert record["keyword"] == "汽修厂"


class TestParseRegion:
    @pytest.mark.parametrize("value", ["460106", "110108"])
    def test_six_digit_is_adcode(self, value):
        assert parse_region(value) == ("adcode", value)

    @pytest.mark.parametrize("value", ["海南省", "新疆维吾尔自治区", "香港特别行政区"])
    def test_province_suffix(self, value):
        assert parse_region(value) == ("province", value)

    @pytest.mark.parametrize("value", ["海口市", "成都", "北京市"])
    def test_city_name_accepted(self, value):
        assert parse_region(value) == ("city", value)

    @pytest.mark.parametrize("value", ["龙华区", "中牟县", "浦东新区"])
    def test_district_name_is_its_own_kind(self, value):
        """区县名不再直接拒绝，而是标成 district，交给 regions 层换 adcode。

        直接拒绝会让用户被迫自己查 adcode；但直接把区县名传给 API 会静默命中
        同名的另一个城市（实测「龙华区」命中深圳）。折中：识别出来，本地查表。
        """
        assert parse_region(value) == ("district", value)

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            parse_region("   ")

    def test_short_digit_string_is_not_adcode(self):
        assert parse_region("123") == ("city", "123")

    def test_collect_region_errors_aggregates(self):
        errors = collect_region_errors(["海口市", "", "460106"])
        assert len(errors) == 1
        assert "为空" in errors[0]

    def test_collect_region_errors_clean_input(self):
        assert collect_region_errors(["海口市", "460106", "龙华区"]) == []


class TestAsPoiList:
    def test_normal_list(self):
        assert as_poi_list({"pois": [{"id": "1"}]}) == [{"id": "1"}]

    def test_empty_list(self):
        assert as_poi_list({"pois": []}) == []

    def test_empty_dict_normalized(self):
        assert as_poi_list({"pois": {}}) == []

    def test_missing_key_is_structural_error(self):
        assert as_poi_list({"status": "0"}) is None

    def test_string_pois_is_structural_error(self):
        assert as_poi_list({"pois": "oops"}) is None
