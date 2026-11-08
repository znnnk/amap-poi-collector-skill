# -*- coding: utf-8 -*-
"""export 层单元测试：真实写文件到 tmp_path，不发请求。"""

import csv
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from amap_poi.export import (  # noqa: E402
    COLUMNS,
    HEADERS,
    build_envelope,
    default_path,
    export,
    write_csv,
    write_json,
    xlsx_available,
)
from amap_poi.normalize import RECORD_FIELDS, normalize_poi  # noqa: E402


def record(poi_id="A", name="示例店", tel="0898-1234"):
    raw = {
        "id": poi_id,
        "name": name,
        "tel": tel,
        "type": "汽车服务;汽车维修",
        "typecode": "010104",
        "address": "示例路 1 号",
        "pname": "海南省",
        "cityname": "海口市",
        "adname": "龙华区",
        "adcode": "460106",
        "location": "110.33119,20.031971",
        "biz_ext": {"rating": "4.7", "open_time": "08:30-18:00"},
    }
    return normalize_poi(raw, keyword="汽修厂")


class FakeResult(object):
    """最小 CollectResult 替身。"""

    def __init__(self, records, requests=2, hit_cap=False, errors=None):
        self._records = records
        self.requests = requests
        self.errors = errors or []
        self.stopped_early = False
        self.stop_reason = ""
        self.units = []
        self._hit_cap = hit_cap

    @property
    def unique(self):
        return len(self._records)

    @property
    def raw_total(self):
        return len(self._records)

    @property
    def tel_empty_rate(self):
        if not self._records:
            return 0.0
        return round(sum(1 for r in self._records if not r.get("tel")) / float(len(self._records)), 4)

    @property
    def hit_cap_units(self):
        return []

    @property
    def failed_units(self):
        return []

    @property
    def fatal(self):
        return False


# ---------------------------------------------------------------- 列定义


class TestColumns:
    def test_every_column_has_header(self):
        for column in COLUMNS:
            assert column in HEADERS, column

    def test_columns_are_known_fields(self):
        for column in COLUMNS:
            assert column in RECORD_FIELDS, column

    def test_no_duplicate_columns(self):
        assert len(set(COLUMNS)) == len(COLUMNS)

    def test_poi_id_is_last(self):
        """POI ID 放最后：人看表格时不占第一屏。"""
        assert COLUMNS[-1] == "poi_id"


# ---------------------------------------------------------------- CSV


class TestCsv:
    def test_header_is_chinese(self, tmp_path):
        path = str(tmp_path / "a.csv")
        write_csv([record()], path)
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
        assert header[0] == "名称"
        assert "POI ID" in header
        assert len(header) == len(COLUMNS)

    def test_utf8_bom_for_excel(self, tmp_path):
        path = str(tmp_path / "a.csv")
        write_csv([record()], path)
        with open(path, "rb") as handle:
            assert handle.read(3) == b"\xef\xbb\xbf"

    def test_values_written(self, tmp_path):
        path = str(tmp_path / "a.csv")
        write_csv([record()], path)
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["名称"] == "示例店"
        assert rows[0]["电话"] == "0898-1234"
        assert rows[0]["经度"] == "110.33119"

    def test_empty_records_writes_header_only(self, tmp_path):
        path = str(tmp_path / "a.csv")
        write_csv([], path)
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        assert len(rows) == 1

    def test_no_literal_brackets(self, tmp_path):
        """回归：空值必须是 ""，不能出现字面量 [] 。"""
        raw = {"id": "B1", "name": "x", "tag": [], "website": [], "email": []}
        rec = normalize_poi(raw, keyword="k")
        path = str(tmp_path / "a.csv")
        write_csv([rec], path)
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            body = handle.read()
        assert "[]" not in body

    def test_creates_parent_dir(self, tmp_path):
        path = str(tmp_path / "nested" / "deep" / "a.csv")
        write_csv([record()], path)
        assert os.path.isfile(path)

    def test_comma_in_field_is_quoted(self, tmp_path):
        rec = record()
        rec["address"] = "示例路 1 号,2 栋"
        path = str(tmp_path / "a.csv")
        write_csv([rec], path)
        with io.open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["地址"] == "示例路 1 号,2 栋"


# ---------------------------------------------------------------- JSON


class TestJson:
    def test_envelope_shape(self, tmp_path):
        recs = [record()]
        envelope = build_envelope(
            recs,
            result=FakeResult(recs),
            params={"keywords": ["汽修厂"]},
            tool_version="0.1.0",
            started_at="2026-11-07 10:00:00",
        )
        path = str(tmp_path / "a.json")
        write_json(envelope, path)
        with io.open(path, encoding="utf-8") as handle:
            payload = json.load(handle)

        assert payload["tool_version"] == "0.1.0"
        assert payload["params"]["keywords"] == ["汽修厂"]
        assert payload["totals"]["unique"] == 1
        assert len(payload["records"]) == 1
        assert payload["records"][0]["name"] == "示例店"

    def test_envelope_without_result(self):
        envelope = build_envelope([record()], result=None)
        assert envelope["totals"]["unique"] == 1
        assert envelope["units"] == []
        assert envelope["errors"] == []

    def test_chinese_not_escaped(self, tmp_path):
        path = str(tmp_path / "a.json")
        write_json(build_envelope([record()]), path)
        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert "示例店" in text
        assert "\\u" not in text

    def test_json_has_trailing_newline(self, tmp_path):
        path = str(tmp_path / "a.json")
        write_json(build_envelope([]), path)
        with open(path, "rb") as handle:
            assert handle.read().endswith(b"\n")


# ---------------------------------------------------------------- XLSX


class TestXlsx:
    def test_fallback_when_no_openpyxl(self, tmp_path, monkeypatch):
        """模拟没装 openpyxl：必须回落到 CSV，且返回的是实际路径。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("openpyxl"):
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        path = str(tmp_path / "a.xlsx")
        written = export([record()], "xlsx", out_path=path)
        assert written.endswith(".csv")
        assert os.path.isfile(written)

    @pytest.mark.skipif(not xlsx_available(), reason="未安装 openpyxl")
    def test_real_xlsx(self, tmp_path):
        from openpyxl import load_workbook

        path = str(tmp_path / "a.xlsx")
        export([record()], "xlsx", out_path=path)
        book = load_workbook(path)
        assert "POI" in book.sheetnames
        sheet = book["POI"]
        assert sheet.max_row == 2
        assert sheet.cell(row=1, column=1).value == "名称"
        assert sheet.cell(row=2, column=1).value == "示例店"

    @pytest.mark.skipif(not xlsx_available(), reason="未安装 openpyxl")
    def test_xlsx_freezes_header(self, tmp_path):
        from openpyxl import load_workbook

        path = str(tmp_path / "a.xlsx")
        export([record()], "xlsx", out_path=path)
        book = load_workbook(path)
        assert book["POI"].freeze_panes == "A2"


# ---------------------------------------------------------------- 统一入口


class TestExportDispatch:
    def test_csv(self, tmp_path):
        path = str(tmp_path / "a.csv")
        assert export([record()], "csv", out_path=path) == path

    def test_json(self, tmp_path):
        path = str(tmp_path / "a.json")
        assert export([record()], "json", out_path=path) == path

    def test_format_with_dot(self, tmp_path):
        path = str(tmp_path / "a.csv")
        assert export([record()], ".csv", out_path=path) == path

    def test_format_case_insensitive(self, tmp_path):
        path = str(tmp_path / "a.csv")
        assert export([record()], "CSV", out_path=path) == path

    def test_unknown_format_raises(self, tmp_path):
        with pytest.raises(ValueError):
            export([record()], "parquet", out_path=str(tmp_path / "a.parquet"))

    def test_default_path_has_timestamp(self, tmp_path):
        path = default_path(str(tmp_path), "csv")
        assert path.endswith(".csv")
        assert "amap_poi_" in os.path.basename(path)

    def test_default_path_used_when_no_out_path(self, tmp_path):
        written = export([record()], "csv", out_dir=str(tmp_path))
        assert os.path.isfile(written)
        assert os.path.dirname(written) == str(tmp_path)
