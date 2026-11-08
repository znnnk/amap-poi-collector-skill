# -*- coding: utf-8 -*-
"""CLI 层测试：只走不需要网络的路径（dry-run、参数校验、key 解析）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from amap_poi import cli  # noqa: E402
from amap_poi.client import AmapError  # noqa: E402
from amap_poi.report import EXIT_FATAL, EXIT_OK  # noqa: E402


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch, tmp_path):
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDryRun:
    def test_returns_zero_without_key(self):
        assert cli.main(["-k", "汽修厂", "-r", "海口市", "--dry-run"]) == EXIT_OK

    def test_no_network_called(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("dry-run 不应发起请求")

        monkeypatch.setattr("amap_poi.client.http_get_json", boom)
        assert cli.main(["-k", "汽修厂", "-r", "海口市", "--dry-run"]) == EXIT_OK


class TestRegionValidation:
    def test_district_name_rejected(self):
        assert cli.main(["-k", "汽修厂", "-r", "龙华区", "--dry-run"]) == EXIT_FATAL

    def test_adcode_accepted(self):
        assert cli.main(["-k", "汽修厂", "-r", "460106", "--dry-run"]) == EXIT_OK

    def test_province_accepted(self):
        assert cli.main(["-k", "汽修厂", "-r", "海南省", "--dry-run"]) == EXIT_OK

    def test_validate_regions_returns_kinds(self):
        parsed = cli.validate_regions(["海口市", "460106", "海南省"])
        assert [kind for kind, _ in parsed] == ["city", "adcode", "province"]

    def test_validate_regions_collects_all_errors(self):
        with pytest.raises(ValueError) as excinfo:
            cli.validate_regions(["龙华区", "浦东新区"])
        text = str(excinfo.value)
        assert "龙华区" in text
        assert "浦东新区" in text


class TestResolveKey:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AMAP_KEY", "a" * 32)
        config = tmp_path / "config.json"
        config.write_text('{"key": "%s"}' % ("b" * 32), encoding="utf-8")
        assert cli.resolve_key(str(config)) == "a" * 32

    def test_config_file(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text('{"key": "%s"}' % ("b" * 32), encoding="utf-8")
        assert cli.resolve_key(str(config)) == "b" * 32

    def test_placeholder_config_is_not_a_key(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text('{"key": "在此填入你的高德key"}', encoding="utf-8")
        with pytest.raises(AmapError) as excinfo:
            cli.resolve_key(str(config), allow_prompt=False)
        assert excinfo.value.action == "fatal"
        assert "AMAP_KEY" in str(excinfo.value)

    def test_missing_key_message_names_web_service(self):
        with pytest.raises(AmapError) as excinfo:
            cli.resolve_key("nope.json", allow_prompt=False)
        assert "Web服务" in str(excinfo.value)

    def test_config_found_in_skill_root_from_other_cwd(self, monkeypatch, tmp_path):
        """技能根目录的 config.json 能被找到——Agent 在别的工作目录调用也不丢 key。"""
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        (skill_root / "config.json").write_text(
            '{"key": "%s"}' % ("c" * 32), encoding="utf-8"
        )
        monkeypatch.setattr(cli, "SKILL_ROOT", str(skill_root))
        assert cli.resolve_key("config.json", allow_prompt=False) == "c" * 32

    def test_cwd_config_wins_over_skill_root(self, monkeypatch, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        (skill_root / "config.json").write_text(
            '{"key": "%s"}' % ("d" * 32), encoding="utf-8"
        )
        monkeypatch.setattr(cli, "SKILL_ROOT", str(skill_root))
        cwd_config = tmp_path / "config.json"
        cwd_config.write_text('{"key": "%s"}' % ("e" * 32), encoding="utf-8")
        assert cli.resolve_key("config.json", allow_prompt=False) == "e" * 32


class TestParser:
    def test_keyword_and_region_required(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_defaults(self):
        args = cli.build_parser().parse_args(["-k", "汽修厂", "-r", "海口市"])
        assert args.max_records == 0
        assert args.format == "csv"
        assert args.interval == 0.4
        assert args.max_requests == 0
        assert not args.yes

    def test_repeatable(self):
        args = cli.build_parser().parse_args(
            ["-k", "汽修厂", "-k", "汽车维修", "-r", "海口市", "-r", "460106"]
        )
        assert args.keywords == ["汽修厂", "汽车维修"]
        assert args.regions == ["海口市", "460106"]
