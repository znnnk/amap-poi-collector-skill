# -*- coding: utf-8 -*-
"""命令行入口：参数解析、key 解析、预算确认、编排调用、报告与退出码。

设计约束（详见 README「参数与产物」与「数据为什么可能不完整」）：
  * 只有关键词和地区必填，其余都有默认值；
  * 请求数是乘法增长，因此执行前必须打印预估并要 ``--yes`` 确认；
  * 高德错误码决定「停 / 退避 / 跳过」，退出码决定 Skill 的话术。
"""

import argparse
import io
import os
import sys
from datetime import datetime

from . import __version__
from .client import AmapClient, AmapError
from .collect import Collector, estimate_requests
from .export import export
from .normalize import parse_region
from .regions import default_cache_path, get_index, resolve_all
from .report import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL, build_report, compute_exit_code, write_report

KEY_PLACEHOLDER_HINTS = ("在此填入", "your_key", "YOUR_KEY", "<", "请填入")

#: 技能目录（``SKILL.md`` 所在目录）= ``scripts/amap_poi/`` 往上两级。
#: 用于「从别的工作目录调用时，也能找到放在技能根目录的 config.json」。
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_CONFIG = "config.json"
DEFAULT_OUTPUT_DIR = "output"


# ---------------------------------------------------------------- 控制台编码


def _harden_console(stream):
    """让终端编码装不下的字符降级成 ``?``，而不是抛异常终止整次采集。

    Windows 中文控制台的默认编码是 GBK，装不下 ``⚠`` (U+26A0)、``✗`` (U+2717)，
    甚至装不下用户自己输入的 emoji 关键词。若不兜底，``print_result()`` 会在
    **数据已经写盘之后**抛 ``UnicodeEncodeError``，进程以 1 退出——调用方拿不到
    本该是 ``2``（不完整）或 ``3``（致命）的退出码，等于采集结果白跑。
    """
    if stream is None:
        return None
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:  # Python 3.7+
        try:
            reconfigure(errors="replace")
            return stream
        except (ValueError, OSError):
            pass
    buffer = getattr(stream, "buffer", None)  # Python 3.6
    if buffer is None:
        return stream
    try:
        return io.TextIOWrapper(
            buffer,
            encoding=getattr(stream, "encoding", None) or "utf-8",
            errors="replace",
            line_buffering=True,
        )
    except (ValueError, OSError):
        return stream


def harden_console():
    """给 stdout / stderr 装上「编码失败就替换」的兜底。可重复调用。"""
    global _CONSOLE_HARDENED
    if _CONSOLE_HARDENED:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        hardened = _harden_console(stream)
        if hardened is not None and hardened is not stream:
            setattr(sys, name, hardened)
    _CONSOLE_HARDENED = True


_CONSOLE_HARDENED = False


# ---------------------------------------------------------------- key 解析


def _looks_like_placeholder(key: str) -> bool:
    text = (key or "").strip()
    if not text:
        return True
    return any(hint in text for hint in KEY_PLACEHOLDER_HINTS)


def resolve_key(config_path: str = DEFAULT_CONFIG, allow_prompt: bool = False) -> str:
    """按 AMAP_KEY → config.json → 交互提示 的顺序取 key。

    ``config.json`` 会先按传入路径找（相对当前工作目录），找不到再按**技能目录**找一次。
    这样 Agent 在任意工作目录调用都不会因为 cwd 不同而丢 key。
    """
    key = (os.environ.get("AMAP_KEY") or "").strip()
    if key and not _looks_like_placeholder(key):
        return key

    candidates = [config_path]
    if config_path and not os.path.isabs(config_path):
        candidates.append(os.path.join(SKILL_ROOT, config_path))

    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            import json

            with open(candidate, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            key = str(payload.get("key") or "").strip()
            if key and not _looks_like_placeholder(key):
                return key
        except (ValueError, OSError):
            continue

    if allow_prompt and sys.stdin is not None and sys.stdin.isatty():
        try:
            pasted = input("请粘贴高德「Web服务」key（不会写入任何文件）：").strip()
        except (EOFError, KeyboardInterrupt):
            pasted = ""
        if pasted and not _looks_like_placeholder(pasted):
            return pasted

    raise AmapError(
        "未找到可用的高德 key。请设置环境变量 AMAP_KEY，或在技能目录的 config.json 里填写 key"
        "（已查找：%s）。key 必须是「Web服务」类型，不是 JS API。申请地址：https://console.amap.com"
        % os.pathsep.join(candidates),
        action="fatal",
    )


# ---------------------------------------------------------------- 参数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amap-poi-collector-skill",
        description="按关键词 + 地区批量采集高德 POI（含电话），导出 CSV / JSON / XLSX。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/collect.py -k 汽修厂 -r 海口市\n"
            "  python scripts/collect.py -k 汽修厂 -k 汽车维修 -r 海口市 -r 460106\n"
            "  python scripts/collect.py -k 汽修厂 -r 460106 --max-records 100 --format xlsx --yes\n"
            "\n"
            "地区参数：城市名 / 省名 / 6 位 adcode。区县名会被拒绝（高德会串到同名区县）。\n"
        ),
    )
    parser.add_argument("-k", "--keyword", action="append", required=False,
                        dest="keywords", metavar="关键词",
                        help="要搜的关键词，可重复多次（用 --list-regions 时可省略）")
    parser.add_argument("-r", "--region", action="append", required=False,
                        dest="regions", metavar="地区",
                        help="城市名 / 省名 / 区县名 / 6 位 adcode，可重复多次")
    parser.add_argument("--list-regions", default=None, metavar="城市名",
                        help="列出该城市（或省）下辖的区县与 adcode，然后退出；不采集")
    parser.add_argument("--refresh-regions", action="store_true",
                        help="强制重新拉取行政区划索引（默认缓存 30 天）")
    parser.add_argument("-n", "--max-records", type=int, default=0,
                        help="本次总条数上限（默认 0 = 不限；单查询本身约 200~225 条到顶）")
    parser.add_argument("-f", "--format", default="csv", choices=("csv", "json", "xlsx"),
                        help="输出格式（默认 csv）")
    parser.add_argument("-o", "--output", default=None, metavar="路径",
                        help="输出文件路径；默认 <out-dir>/amap_poi_<时间戳>.<格式>")
    parser.add_argument("--report-json", default=None, metavar="路径",
                        help="额外写出机器可读运行报告")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="本次请求数硬刹车（默认 0 = 不设，靠单单元页数保险丝防跑飞）")
    parser.add_argument("--interval", type=float, default=0.4,
                        help="两次请求的最小间隔秒数（默认 0.4）")
    parser.add_argument("--offset", type=int, default=25,
                        help="每页条数（默认 25，别改）")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="config.json 路径")
    parser.add_argument("--out-dir", default=DEFAULT_OUTPUT_DIR, help="默认输出目录")
    parser.add_argument("--yes", action="store_true", help="跳过执行前确认（脚本/Agent 调用用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划，不发请求（也就不验证 Key）")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def validate_regions(regions):
    """校验地区参数，返回 (kind, value) 列表；不合法时抛出 ValueError（含全部错误）。"""
    parsed = []
    errors = []
    for region in regions:
        try:
            parsed.append(parse_region(region))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("\n".join(errors))
    return parsed


def _region_cache(args) -> str:
    """行政区划缓存路径。放在技能目录，避免跟调用方 cwd 走。"""
    return default_cache_path(SKILL_ROOT)


def _load_region_index(args, key, quiet=False, offline=False):
    """取行政区划索引：缓存新鲜时零请求、零 key。

    ``offline=True``（dry-run 用）时**绝不联网**：忽略 ``--refresh-regions``，
    只读本地缓存，没有缓存就返回 ``None`` 让调用方降级。

    返回 ``None`` 表示「离线可用但拿不到 key」，调用方决定是降级还是报错。
    """
    path = _region_cache(args)
    refresh = getattr(args, "refresh_regions", False) and not offline
    try:
        return get_index(
            path,
            key=key or "",
            refresh=refresh,
        )
    except AmapError:
        if quiet:
            return None
        raise


def list_regions(index, query):
    """``--list-regions`` 的实现：打印某城市/省下辖的区县与 adcode。

    返回退出码：找到下级 → ``EXIT_OK``，找不到 → ``EXIT_FATAL``。
    """
    matches = index.find(query)
    if not matches:
        print("[x] 没找到 %r。可以试城市全名（用「市」结尾）或省名。" % query, file=sys.stderr)
        return EXIT_FATAL

    if len(matches) > 1:
        print("[!] %r 有 %d 个匹配，请用更完整的名称（如「海口市」）：" % (query, len(matches)))
        for node in matches:
            print("  %s  %s（%s）" % (node.adcode, index.path(node), node.level_label))
        return EXIT_FATAL

    parent = matches[0]
    children = index.children(parent)
    print("%s（%s，adcode=%s）" % (parent.name, parent.level_label, parent.adcode))
    if not children:
        print("  没有下级行政单位 —— 该区划只能整体检索，不能再往下拆。")
        print("  直接用 `-r %s` 即可；要更全只能加关键词变体（同义的多种说法）。" % parent.adcode)
        return EXIT_OK

    districts = [node for node in children if node.level == "district"]
    cities = [node for node in children if node.level == "city"]
    others = [node for node in children if node.level not in ("district", "city")]

    if districts:
        print()
        print("下辖区县 %d 个（把 adcode 传给 -r 就能按区县检索）：" % len(districts))
        for node in districts:
            print("  %s  %s" % (node.adcode, node.name))
    if cities:
        print()
        print("下辖地级单位 %d 个（按城市跑；每个城市内部还可以再按区县拆）：" % len(cities))
        for node in cities:
            print("  %s  %s" % (node.adcode, node.name))
    if others:
        print()
        print("下级行政单位 %d 个：" % len(others))
        for node in others:
            print("  %s  %s（%s）" % (node.adcode, node.name, node.level_label))
    if not districts and others:
        print()
        print("注意：这些不是区县级单位（该市没有区县层，如东莞、中山、济源）。"
              "它们与上级共用同一批 POI，按每个下级各跑一遍不会拿到更多数据；"
              "要更全只能加关键词变体。")
    print()
    if districts:
        print("提示：想一次把所有区县都跑一遍，把上面的 adcode 逐个加到 -r 上即可。")
    elif cities:
        print("提示：先用 `--list-regions <某个城市>` 看它下辖的区县，再逐个 -r 传入。")
    return EXIT_OK


# ---------------------------------------------------------------- 输出


def print_plan(keywords, regions, parsed_regions, max_records, estimate, offset):
    print("=" * 68)
    print("高德 POI 采集 · 执行计划")
    print("=" * 68)
    print("关键词 (%d)：%s" % (len(keywords), "、".join(keywords)))
    print("地区   (%d)：%s" % (len(regions), "、".join(
        "%s[%s]" % (value, kind) for kind, value in parsed_regions)))
    print("查询单元：%d × %d = %d 个" % (len(keywords), len(regions),
                                        len(keywords) * len(regions)))
    print("条数上限：%s" % (max_records if max_records else "不限（默认；单查询本身约 200~225 条到顶）"))
    print("请求预估：≤ %d 次（每单元约 %d 页 × %d 条 + 1 次空页确认）"
          % (estimate, 200 // max(1, offset) + 1, offset))
    print("单单元可检索量：约 200~225 条（实测值，服务端不保证；想更多需拆关键词或拆区县）")
    print("-" * 68)

def print_result(result, outputs):
    print()
    print("=" * 68)
    print("采集结果")
    print("=" * 68)
    print("去重后条数：%d（原始 %d 条）" % (result.unique, result.raw_total))
    print("请求次数  ：%d" % result.requests)
    print("电话空缺率：%.1f%%" % (result.tel_empty_rate * 100))
    print()
    for unit in result.units:
        from .report import unit_note

        print("  · %-12s @ %-10s %s" % (unit.keyword, unit.region, unit_note(unit)))
    if result.errors:
        print()
        print("错误记录：")
        for item in result.errors:
            print("  [x] %s -> %s" % (item.get("unit", ""), item.get("message", "")))
    if result.stopped_early:
        print()
        print("[!] 提前结束：%s" % (result.stop_reason or "未知原因"))
    if result.hit_cap_units:
        print()
        print("[!] %d 个查询单元疑似触顶（单查询约 200~225 条即到顶），本次数据可能不完整。"
              % len(result.hit_cap_units))
        print("  想拿更全：加关键词变体、或把城市拆成区县 adcode 再跑。")
    if outputs:
        print()
        for label, path in outputs.items():
            print("输出（%s）：%s" % (label, path))


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    harden_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    keywords = [item.strip() for item in (args.keywords or []) if item and item.strip()]
    regions = [item.strip() for item in (args.regions or []) if item and item.strip()]

    # --list-regions 是独立查询：不需要关键词，也不需要真的采集
    if args.list_regions:
        try:
            key = resolve_key(args.config, allow_prompt=False)
        except AmapError:
            key = ""  # 有缓存就不需要 key
        try:
            index = _load_region_index(args, key)
        except AmapError as exc:
            print("[x] %s" % exc, file=sys.stderr)
            return EXIT_FATAL
        if index is None:
            print("[x] 没有可用的行政区划索引，也没有可用的 key。"
                  "请设置 AMAP_KEY，或把 key 写进技能目录的 config.json。", file=sys.stderr)
            return EXIT_FATAL
        return list_regions(index, args.list_regions)

    if not keywords:
        print("[x] 关键词为空", file=sys.stderr)
        return EXIT_FATAL
    if not regions:
        print("[x] 地区为空", file=sys.stderr)
        return EXIT_FATAL

    try:
        parsed_regions = validate_regions(regions)
    except ValueError as exc:
        print("[x] 地区参数不合法：", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return EXIT_FATAL

    # 区县名 → adcode。这一步可能需要一次在线拉取（首次），之后走本地缓存。
    # 关键：真跑时**必须**解析成功，否则就会把「龙华区」原样传给 API，
    # 拿到深圳的数据而毫无提示——这正是本工具最想避免的失败模式。
    if any(kind == "district" for kind, _ in parsed_regions):
        if args.dry_run:
            # dry-run 一律不联网：区县名解析**只用本地缓存**，没有缓存就按原名显示计划。
            # 传空 key 让 get_index 走 cache-only 分支（缓存新鲜或过期都可用，但绝不发请求）。
            key = ""
        else:
            try:
                key = resolve_key(args.config, allow_prompt=False)
            except AmapError:
                key = ""
        index = None
        try:
            index = _load_region_index(args, key, quiet=True, offline=args.dry_run)
        except AmapError as exc:
            if not args.dry_run:
                print("[x] 解析区县名需要行政区划索引：%s" % exc, file=sys.stderr)
                print("    也可以直接给 6 位 adcode，或用 --list-regions <城市名> 先查。",
                      file=sys.stderr)
                return EXIT_FATAL

        if index is None:
            if not args.dry_run:
                print("[x] 解析区县名需要一次在线拉取行政区划（约 1 次请求），"
                      "但当前没有可用的 key。\n"
                      "    请设置 AMAP_KEY，或直接把区县换成 6 位 adcode。", file=sys.stderr)
                return EXIT_FATAL
            print("[!] 本地没有行政区划缓存，dry-run 不联网，所以只能按区县名显示计划。"
                  "真跑之前会先花 1 次请求把区县名换成 6 位 adcode。")
        else:
            parsed_regions, notes, errors = resolve_all(index, parsed_regions)
            if errors:
                print("[x] 地区参数无法确定：", file=sys.stderr)
                for item in errors:
                    print(item, file=sys.stderr)
                return EXIT_FATAL
            for note in notes:
                print("区县已解析：%s" % note)

    for index_, (kind, _value) in enumerate(parsed_regions):
        if kind == "province":
            print("提示：%s 是省级查询，结果会跨市且同样受 200 条上限；"
                  "要更全建议按城市跑。" % regions[index_])

    # 实际发起检索用的地区值：区县名已被换成 adcode，其余原样。
    # 必须用这份而不是用户原始输入——把「龙华区」直接传给 API 会静默查到深圳。
    run_regions = [value for _kind, value in parsed_regions]

    estimate = estimate_requests(len(keywords), len(run_regions), args.max_records, args.offset)
    # 注意：不把 estimate 直接当硬刹车。estimate 是「预估」，实测显示单单元
    # 结束页数会因城市而变（海口 9 页、成都 8 页），把预估值当上限会把
    # 正常收官的那一次空页请求砍掉，误报「已达请求上限」。
    # 真正的防跑飞靠 Collector 里的单单元页数保险丝；--max-requests 是额外可选刹车。
    max_requests = args.max_requests or 0
    print_plan(keywords, regions, parsed_regions, args.max_records, estimate, args.offset)

    if args.dry_run:
        print("（dry-run：未发起任何请求，也未验证 Key）")
        return EXIT_OK

    try:
        key = resolve_key(args.config, allow_prompt=True)
    except AmapError as exc:
        print("[x] %s" % exc, file=sys.stderr)
        return EXIT_FATAL

    if not args.yes:
        if not (sys.stdin and sys.stdin.isatty()):
            print("[x] 非交互环境请加 --yes 确认执行（避免 Agent 误触发消耗配额）", file=sys.stderr)
            return EXIT_FATAL
        answer = input("确认执行？将消耗最多 %d 次请求 [y/N]：" % estimate).strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            return EXIT_OK

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    client = AmapClient(key, min_interval=args.interval, timeout=20.0)
    collector = Collector(
        client,
        max_records=args.max_records,
        max_requests=max_requests,
        offset=args.offset,
    )

    print("\n开始采集…（每单元翻到返回 0 条为止）")
    try:
        result = collector.run(keywords, run_regions)
    except AmapError as exc:
        print("[x] 致命错误：%s" % exc, file=sys.stderr)
        return EXIT_FATAL
    except KeyboardInterrupt:
        print("\n[x] 已中断（未写出任何数据）", file=sys.stderr)
        return EXIT_FATAL

    outputs = {}
    if result.records:
        target = export(
            result.records,
            args.format,
            out_path=args.output,
            out_dir=args.out_dir,
            result=result,
            params={
                "keywords": keywords,
                "regions": regions,
                "regions_resolved": run_regions,
                "max_records": args.max_records,
                "format": args.format,
            },
            tool_version=__version__,
            started_at=started_at,
        )
        outputs[args.format] = target
    else:
        print("\n（本次没有采到任何记录，未写出数据文件）")

    exit_code = compute_exit_code(result)

    if args.report_json:
        report_outputs = dict(outputs)
        report_outputs["report"] = args.report_json
        report = build_report(
            result,
            params={
                "keywords": keywords,
                "regions": regions,
                "regions_resolved": run_regions,
                "max_records": args.max_records,
                "format": args.format,
                "max_requests": max_requests,
            },
            tool_version=__version__,
            started_at=started_at,
            outputs=report_outputs,
        )
        write_report(report, args.report_json)
        outputs["report"] = args.report_json

    # 收尾打印只是「给人看」的，不能让编码问题推翻已经写好的产物和退出码。
    try:
        print_result(result, outputs)
    except Exception:  # noqa: BLE001
        sys.stderr.write("[warn] 结果摘要打印失败（不影响已写出的文件与退出码）\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
