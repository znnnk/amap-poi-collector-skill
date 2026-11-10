# -*- coding: utf-8 -*-
"""CLI 层测试：只走不需要网络的路径（dry-run、参数校验、key 解析）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from amap_poi import cli  # noqa: E402
from amap_poi.client import AmapError  # noqa: E402
from amap_poi.report import EXIT_FATAL, EXIT_OK  # noqa: E402


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch, tmp_path):
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    # 行政区划缓存也指到临时目录，避免测试读到本机真实缓存、也避免写脏仓库
    monkeypatch.setattr(cli, "_region_cache", lambda args: str(tmp_path / "regions.json"))
    return tmp_path


class TestDryRun:
    def test_returns_zero_without_key(self):
        assert cli.main(["-k", "汽修厂", "-r", "海口市", "--dry-run"]) == EXIT_OK

    def test_no_network_called(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("dry-run 不应发起请求")

        monkeypatch.setattr("amap_poi.client.http_get_json", boom)
        monkeypatch.setattr("amap_poi.regions.http_get_json", boom)
        assert cli.main(["-k", "汽修厂", "-r", "海口市", "--dry-run"]) == EXIT_OK

    def test_district_path_also_offline(self, monkeypatch, capsys):
        """区县名路径才是真正会联网的分支：dry-run 时必须同样零请求。

        有 key、无缓存、还带了 --refresh-regions（强制联网），三重诱惑下也只能读本地。
        """
        def boom(*args, **kwargs):
            raise AssertionError("dry-run 不应发起任何请求（含区县名解析）")

        monkeypatch.setattr("amap_poi.client.http_get_json", boom)
        monkeypatch.setattr("amap_poi.regions.http_get_json", boom)
        monkeypatch.setenv("AMAP_KEY", "a" * 32)
        code = cli.main(["-k", "汽修厂", "-r", "龙华区", "--dry-run", "--refresh-regions"])
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "龙华区" in out
        assert "未发起任何请求" in out

    def test_district_uses_cache_offline(self, monkeypatch, capsys, tmp_path):
        """有缓存时 dry-run 不联网，且能把区县名解析成 adcode。"""
        from amap_poi import regions

        payload = {
            "status": "1",
            "districts": [
                {
                    "name": "海南省",
                    "adcode": "460000",
                    "level": "province",
                    "districts": [
                        {
                            "name": "海口市",
                            "adcode": "460100",
                            "level": "city",
                            "districts": [
                                {"name": "龙华区", "adcode": "460106", "level": "district",
                                 "districts": []}
                            ],
                        }
                    ],
                }
            ],
        }
        index = regions.RegionIndex(regions.parse_tree(payload))
        regions.save_index(index, str(tmp_path / "regions.json"))

        def boom(*args, **kwargs):
            raise AssertionError("有缓存时 dry-run 不应联网")

        monkeypatch.setattr("amap_poi.client.http_get_json", boom)
        monkeypatch.setattr("amap_poi.regions.http_get_json", boom)
        monkeypatch.setenv("AMAP_KEY", "a" * 32)

        assert cli.main(["-k", "汽修厂", "-r", "龙华区", "--dry-run"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "460106" in out

    def test_district_without_cache_or_key_still_plans(self, capsys):
        """dry-run 时区县名解析不了也不该失败——计划本身就是给用户看的。"""
        assert cli.main(["-k", "汽修厂", "-r", "龙华区", "--dry-run"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "龙华区" in out
        assert "dry-run" in out


class TestRegionValidation:
    def test_district_without_key_or_cache_is_fatal(self, capsys):
        """真跑时解析不了就停下——绝不能把「龙华区」原样交给 API 查到深圳。"""
        code = cli.main(["-k", "汽修厂", "-r", "龙华区", "--yes"])
        assert code == EXIT_FATAL
        err = capsys.readouterr().err
        assert "adcode" in err

    def test_adcode_accepted(self):
        assert cli.main(["-k", "汽修厂", "-r", "460106", "--dry-run"]) == EXIT_OK

    def test_province_accepted(self):
        assert cli.main(["-k", "汽修厂", "-r", "海南省", "--dry-run"]) == EXIT_OK

    def test_validate_regions_returns_kinds(self):
        parsed = cli.validate_regions(["海口市", "460106", "海南省", "龙华区"])
        assert [kind for kind, _ in parsed] == ["city", "adcode", "province", "district"]

    def test_validate_regions_collects_all_errors(self):
        with pytest.raises(ValueError) as excinfo:
            cli.validate_regions(["海口市", "   "])
        assert "为空" in str(excinfo.value)


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
    def test_missing_keyword_or_region_is_fatal(self, capsys):
        """参数层不再用 argparse 的 required（--list-regions 不需要 -k），
        改由 main() 判定：缺关键词/地区 → 3。"""
        assert cli.main([]) == EXIT_FATAL
        assert "关键词" in capsys.readouterr().err

    def test_region_missing_but_keyword_given(self, capsys):
        assert cli.main(["-k", "汽修厂"]) == EXIT_FATAL
        assert "地区" in capsys.readouterr().err

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


class TestConsoleEncoding:
    """Windows 中文控制台默认 GBK，装不下 ⚠ / ✗。

    回归背景：不加兜底时，``print_result()`` 会在**数据已写盘之后**抛
    ``UnicodeEncodeError``，进程以 1 退出，调用方拿不到本该是 2 / 3 的退出码。
    """

    def test_unencodable_char_does_not_raise(self):
        import io as _io

        raw = _io.BytesIO()
        stream = _io.TextIOWrapper(raw, encoding="gbk", errors="strict")
        hardened = cli._harden_console(stream)

        hardened.write("结果 \u26a0 触顶")  # GBK 装不下 U+26A0
        hardened.flush()

        assert raw.getvalue(), "字符应被替换后写出，而不是抛异常"

    def test_none_stream_is_safe(self):
        assert cli._harden_console(None) is None

    def test_harden_console_is_idempotent(self):
        cli.harden_console()
        cli.harden_console()  # 重复调用不应报错
        assert cli._CONSOLE_HARDENED is True

    def test_print_result_failure_does_not_change_exit_code(self, monkeypatch, tmp_path):
        """收尾打印炸了也不能改退出码——产物已写好，退出码必须如实返回。"""
        from amap_poi import report
        from amap_poi.collect import CollectResult, UnitResult

        result = CollectResult()
        unit = UnitResult("汽修厂", "海口市")
        unit.status = "ok"
        unit.raw = 225
        unit.hit_cap = True
        result.units.append(unit)

        def boom(*_a, **_k):
            raise UnicodeEncodeError("gbk", "\u26a0", 0, 1, "illegal multibyte sequence")

        monkeypatch.setattr(cli, "print_result", boom)
        monkeypatch.setattr(cli, "resolve_key", lambda *a, **k: "a" * 32)
        monkeypatch.setattr(cli, "AmapClient", lambda *a, **k: None)
        monkeypatch.setattr(
            cli,
            "Collector",
            lambda *a, **k: type("FakeCollector", (), {"run": lambda self, kw, rg: result})(),
        )

        out_dir = str(tmp_path / "out")
        code = cli.main(
            ["-k", "汽修厂", "-r", "海口市", "--yes", "--out-dir", out_dir]
        )
        assert code == report.EXIT_PARTIAL  # 触顶 → 2，而不是被编码异常改成 1
