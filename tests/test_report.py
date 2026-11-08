# -*- coding: utf-8 -*-
"""report 层单元测试：退出码、单元话术、报告字段契约。"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from amap_poi.collect import UnitResult  # noqa: E402
from amap_poi.report import (  # noqa: E402
    EXIT_FATAL,
    EXIT_OK,
    EXIT_PARTIAL,
    build_report,
    compute_exit_code,
    unit_note,
    write_report,
)


class FakeResult(object):
    def __init__(self, units=(), errors=(), requests=0, stopped=False, reason=""):
        self.units = list(units)
        self.errors = list(errors)
        self.requests = requests
        self.stopped_early = stopped
        self.stop_reason = reason
        self.records = []

    @property
    def unique(self):
        return 0

    @property
    def raw_total(self):
        return sum(u.raw for u in self.units)

    @property
    def tel_empty_rate(self):
        return 0.0

    @property
    def hit_cap_units(self):
        return [u for u in self.units if u.hit_cap]

    @property
    def failed_units(self):
        return [u for u in self.units if u.status in ("failed", "partial")]

    @property
    def fatal(self):
        return any(e.get("action") == "fatal" for e in self.errors)


def unit(status="ok", raw=10, hit_cap=False, error="", count_hint=0):
    item = UnitResult("汽修厂", "海口市")
    item.status = status
    item.raw = raw
    item.hit_cap = hit_cap
    item.error = error
    item.count_hint = count_hint
    return item


# ---------------------------------------------------------------- 退出码


class TestExitCode:
    def test_all_ok_is_zero(self):
        assert compute_exit_code(FakeResult([unit()])) == EXIT_OK

    def test_hit_cap_is_partial(self):
        assert compute_exit_code(FakeResult([unit(raw=225, hit_cap=True)])) == EXIT_PARTIAL

    def test_failed_unit_is_partial(self):
        assert compute_exit_code(FakeResult([unit(status="failed")])) == EXIT_PARTIAL

    def test_partial_unit_is_partial(self):
        assert compute_exit_code(FakeResult([unit(status="partial")])) == EXIT_PARTIAL

    def test_stopped_early_is_partial(self):
        assert compute_exit_code(FakeResult([unit()], stopped=True)) == EXIT_PARTIAL

    def test_fatal_wins_over_partial(self):
        result = FakeResult(
            [unit(raw=225, hit_cap=True)],
            errors=[{"action": "fatal", "message": "key 无效"}],
        )
        assert compute_exit_code(result) == EXIT_FATAL

    def test_constants_distinct(self):
        assert len({EXIT_OK, EXIT_PARTIAL, EXIT_FATAL}) == 3


# ---------------------------------------------------------------- 话术


class TestUnitNote:
    def test_ok(self):
        assert "正常" in unit_note(unit())

    def test_zero_rows(self):
        assert "无结果" in unit_note(unit(raw=0))

    def test_hit_cap_mentions_count(self):
        text = unit_note(unit(raw=225, hit_cap=True, count_hint=551))
        assert "225" in text
        assert "551" in text

    def test_hit_cap_without_count_hint(self):
        text = unit_note(unit(raw=200, hit_cap=True))
        assert "200" in text
        assert "count" not in text

    def test_failed(self):
        text = unit_note(unit(status="failed", error="参数错误"))
        assert "失败" in text
        assert "参数错误" in text

    def test_partial(self):
        text = unit_note(unit(status="partial", raw=30, error="已达条数上限 30"))
        assert "未跑完" in text
        assert "30" in text

    def test_no_percent_sign_crash(self):
        """回归：曾经写过 "取满 %d 条%@"，触发 ValueError。"""
        text = unit_note(unit(raw=225, hit_cap=True, count_hint=551))
        assert "%" not in text


# ---------------------------------------------------------------- 报告契约


class TestBuildReport:
    def test_contract_keys(self):
        report = build_report(
            FakeResult([unit()]),
            params={"keywords": ["汽修厂"], "regions": ["海口市"]},
            tool_version="0.1.0",
            started_at="2026-11-07 10:00:00",
            outputs={"csv": "out/a.csv"},
        )
        for key in ("tool_version", "started_at", "finished_at", "params", "units",
                    "totals", "stopped_early", "stop_reason", "hit_cap_units",
                    "errors", "outputs", "exit_code"):
            assert key in report

    def test_units_carry_note(self):
        report = build_report(FakeResult([unit()]), params={})
        assert "note" in report["units"][0]

    def test_totals_fields(self):
        report = build_report(FakeResult([unit(raw=10)]), params={})
        for key in ("requests", "raw", "unique", "tel_empty_rate"):
            assert key in report["totals"]

    def test_exit_code_matches(self):
        report = build_report(FakeResult([unit(raw=225, hit_cap=True)]), params={})
        assert report["exit_code"] == EXIT_PARTIAL

    def test_hit_cap_units_list(self):
        report = build_report(
            FakeResult([unit(raw=225, hit_cap=True), unit()]), params={}
        )
        assert len(report["hit_cap_units"]) == 1
        assert report["hit_cap_units"][0]["raw"] == 225

    def test_outputs_default_empty(self):
        report = build_report(FakeResult([]), params={})
        assert report["outputs"] == {}


# ---------------------------------------------------------------- 落盘


class TestWriteReport:
    def test_writes_valid_json(self, tmp_path):
        path = str(tmp_path / "r.json")
        write_report({"a": 1, "中文": "值"}, path)
        with io.open(path, encoding="utf-8") as handle:
            assert json.load(handle) == {"a": 1, "中文": "值"}

    def test_creates_parent_dir(self, tmp_path):
        path = str(tmp_path / "x" / "y" / "r.json")
        write_report({}, path)
        assert os.path.isfile(path)

    def test_chinese_not_escaped(self, tmp_path):
        path = str(tmp_path / "r.json")
        write_report({"unit": "海口市"}, path)
        with io.open(path, encoding="utf-8") as handle:
            assert "海口市" in handle.read()
