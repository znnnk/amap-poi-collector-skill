# -*- coding: utf-8 -*-
"""高德 Web 服务 v3/place/text 客户端：请求、分页、错误码分类、限速重试。

这一层是唯一知道 HTTP 与高德字段语义的地方。上层只看到
:class:`SearchPage` 与 :class:`AmapError`，不知道 URL 长什么样。

关键事实（2026-11-07 实测，证据与全部实验见 ``references/api-notes.md``）：
  * 高德错误也返回 **HTTP 200**，真正的结果在 ``status`` / ``infocode``。
  * ``count`` 不可信（实测 count=551 但只能取到 225 条）。
  * 翻页天花板约 200~225 条且随城市与 ``offset`` 漂移，唯一可靠的终止信号是
    「本页返回 0 条」。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

TEXT_SEARCH_URL = "https://restapi.amap.com/v3/place/text"

#: 单次请求最多返回条数（v3 文档上限 25，实测 offset=50 也接受，但不依赖该行为）
MAX_OFFSET = 25

#: 触顶判定阈值。实测天花板漂移在 200~225 之间，取 200 作为「很可能被截断」的下界。
#: 这是**启发式**，不是协议保证；真正的终止条件只有「本页返回 0 条」。
KNOWN_ITEM_CAP = 200

#: 只用于标识客户端身份，不含任何账号或应用标识（不伪造 appname）
USER_AGENT = "amap-poi-collector-skill/0.1"

# ---------------------------------------------------------------- 错误分类

# 三条分类的唯一依据是高德官方「错误码说明」页面（2026-11-07 取，最后更新 2022-10-12）：
# https://lbs.amap.com/api/webservice/guide/tools/info
#
# 注意：这些中文说明是**照官方表格翻译的**，不是本项目逐个实测出来的。只有 10000、
# 10009、10004 在本项目里真实见过；其余码的触发条件与处置方式属于「按文档推断」，
# 见 references/error-codes.md 的「未实测」标记。

#: 任一出现即中止整个运行：继续跑只会白烧配额或一直失败
FATAL_CODES = {
    "10001": "key 不正确或已过期，请重新申请「Web服务」类型的 key",
    "10002": "没有权限使用该服务，或接口路径拼写错误，请检查控制台的服务授权",
    "10003": "当日访问量已超限，次日 0:00 自动解封，请明天再试",
    "10005": "IP 白名单校验失败，请检查 key 是否绑定了服务器 IP",
    "10006": "绑定域名无效（Web服务 key 不应绑定域名）",
    "10007": "数字签名未通过，请检查 key 是否开启了数字签名",
    "10009": "key 与绑定平台不符：你用的是 JS API 的 key，需要「Web服务」类型的 key",
    "10010": "IP 访问超限且已被封停，无法自动恢复，需在高德控制台提交工单",
    "10011": "该服务不支持 HTTPS 请求，需提交工单申请",
    "10012": "权限不足，服务请求被拒绝，请检查该 key 是否开通了「搜索 POI」服务",
    "10013": "key 已被删除，请重新申请",
    "10026": "账号处于被封禁状态，需在高德控制台提交工单申诉",
    "10041": "请求的接口权限已过期，请联系高德续期",
    "10044": "账号维度日调用量超限，超出部分会被拒绝，请明天再试或换账号",
    "10045": "账号维度海外服务日调用量超限，请明天再试",
    "40000": "所购买服务的余额已耗尽，无法继续使用",
    "40002": "所购买的服务已到期",
    "40003": "所购买的海外服务余额已耗尽",
}

#: 可退避重试：频率类限制，等一下大概率能过
RETRY_CODES = {
    "10004": "单位时间内访问过于频繁（按分钟限流，下一分钟自动解封）",
    "10014": "云图服务 QPS 超限",
    "10015": "受单机 QPS 限流（网关超时），请降低请求频率",
    "10016": "服务器负载过高，请稍后再试",
    "10017": "所请求的资源不可用",
    "10019": "某个服务的总 QPS 超限",
    "10020": "某个 key 对某个服务接口的 QPS 超限",
    "10021": "账号对某个服务接口的 QPS 超限",
    "10029": "某个 key 的 QPS/调用量超出限制",
    "300xx": "高德服务端响应失败（3 开头的引擎错误），建议核对参数后重试",
}

#: 跳过当前查询单元，继续下一个：参数或数据范围问题，重试也不会变
SKIP_CODES = {
    "10008": "MD5 安全码未通过验证（仅 SDK 场景出现，Web 服务 key 不会触发）",
    "20000": "请求参数非法，值未按规范填写",
    "20001": "缺少必填参数",
    "20002": "请求协议非法（例如该接口只支持 GET，却用了 POST）",
    "20003": "其他未知错误",
    "20011": "查询点位于海外，但该 key 没有海外地图权限",
    "20012": "查询内容存在非法字符",
    "20800": "规划点不在中国大陆陆地范围内（高德 POI 仅覆盖中国大陆）",
}


class AmapError(Exception):
    """一次请求的失败。

    :param infocode: 高德 ``infocode``，网络层失败时为 ``None``
    :param action: ``"fatal"`` / ``"retry"`` / ``"skip"`` —— 调用方据此决定停还是继续
    """

    def __init__(self, message: str, infocode: Optional[str] = None, action: str = "skip"):
        super().__init__(message)
        self.infocode = infocode
        self.action = action

    def __str__(self) -> str:
        if self.infocode:
            return "[%s] %s" % (self.infocode, self.args[0])
        return self.args[0]


def classify(infocode: Optional[str]) -> str:
    """把 infocode 归到 ``fatal`` / ``retry`` / ``skip``。未知码按 ``skip`` 处理。

    ``300xx`` 是官方文档里的一类（3 开头的引擎错误），按前缀匹配。

    维护约束：**同一个 infocode 不能同时出现在两张表里**——否则分类结果取决于
    查表顺序（本函数按 fatal → retry → skip 的顺序短路），改动表的时候很容易
    察觉不到。新增码时请只放一张表。
    """
    code = str(infocode or "")
    if code in FATAL_CODES:
        return "fatal"
    if code in RETRY_CODES:
        return "retry"
    if code.startswith("3") and len(code) == 5 and code.isdigit():
        return "retry"
    return "skip"


def describe(infocode: Optional[str]) -> str:
    """取 infocode 的中文说明；未知码返回空串。"""
    code = str(infocode or "")
    text = FATAL_CODES.get(code) or RETRY_CODES.get(code) or SKIP_CODES.get(code)
    if text:
        return text
    if code.startswith("3") and len(code) == 5 and code.isdigit():
        return RETRY_CODES["300xx"]
    return ""


# ---------------------------------------------------------------- 传输层


def http_get_json(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    """发起 GET 并解析 JSON。网络层失败抛 :class:`AmapError`，``action="retry"``。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 5xx 值得重试，4xx 基本是配置问题
        action = "retry" if exc.code >= 500 else "skip"
        raise AmapError("HTTP %s: %s" % (exc.code, exc.reason), action=action)
    except urllib.error.URLError as exc:
        raise AmapError("网络错误：%s" % (exc.reason,), action="retry")
    except OSError as exc:
        raise AmapError("网络错误：%s" % (exc,), action="retry")

    try:
        return json.loads(body)
    except ValueError:
        raise AmapError("响应不是合法 JSON（可能被网关拦截）", action="retry")


# ---------------------------------------------------------------- 分页结果


class SearchPage(object):
    """一页搜索结果。``pois`` 为归一化前的原始 dict 列表。"""

    __slots__ = ("keyword", "region", "page", "pois", "count", "infocode")

    def __init__(
        self,
        keyword: str,
        region: str,
        page: int,
        pois: List[Dict[str, Any]],
        count: int,
        infocode: str,
    ):
        self.keyword = keyword
        self.region = region
        self.page = page
        self.pois = pois
        self.count = count
        self.infocode = infocode

    @property
    def is_empty(self) -> bool:
        return not self.pois

    def __repr__(self) -> str:
        return "SearchPage(%s/%s p%s -> %d)" % (
            self.region,
            self.keyword,
            self.page,
            len(self.pois),
        )


# ---------------------------------------------------------------- 客户端


class AmapClient(object):
    """v3/place/text 客户端。线程不安全，单进程串行使用。

    :param key: Web 服务 key
    :param min_interval: 两次请求之间的最小间隔（秒）
    :param max_retries: 单个请求的最大重试次数
    :param timeout: 单次请求超时（秒）
    :param transport: 注入点，测试时替换为假函数
    :param sleep: 注入点，测试时替换为 no-op，避免真等
    """

    def __init__(
        self,
        key: str,
        min_interval: float = 0.4,
        max_retries: int = 3,
        timeout: float = 20.0,
        transport: Optional[Callable[[str], Dict[str, Any]]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        if not key or not key.strip():
            raise AmapError("缺少 API key", action="fatal")
        self.key = key.strip()
        self.min_interval = max(0.0, float(min_interval))
        self.max_retries = max(0, int(max_retries))
        self.timeout = timeout
        self._transport = transport or (lambda url: http_get_json(url, timeout=self.timeout))
        self._sleep = sleep or time.sleep
        self._last_request_at = 0.0
        self.request_count = 0

    # -- 内部 ------------------------------------------------------

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            self._sleep(wait)

    def _build_url(self, keyword: str, region: str, page: int, offset: int) -> str:
        params = {
            "key": self.key,
            "keywords": keyword,
            "city": region,
            "citylimit": "true",
            "offset": str(offset),
            "page": str(page),
            "extensions": "all",
        }
        return TEXT_SEARCH_URL + "?" + urllib.parse.urlencode(params)

    def _request(self, keyword: str, region: str, page: int, offset: int) -> Dict[str, Any]:
        """带限速与重试的单次请求，返回原始 JSON（已确认 status=1）。"""
        url = self._build_url(keyword, region, page, offset)
        last_error: Optional[AmapError] = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                # 指数退避：1, 2, 4, 8 秒
                self._sleep(min(2.0 ** (attempt - 1), 8.0))
            self._throttle()
            try:
                payload = self._transport(url)
                self._last_request_at = time.monotonic()
                self.request_count += 1
            except AmapError as exc:
                self._last_request_at = time.monotonic()
                self.request_count += 1
                if exc.action == "fatal":
                    raise
                last_error = exc
                if exc.action != "retry":
                    raise
                continue

            if not isinstance(payload, dict):
                last_error = AmapError("响应结构异常（不是 JSON 对象）", action="retry")
                continue

            status = str(payload.get("status", ""))
            infocode = str(payload.get("infocode", ""))

            if status == "1" and infocode in ("", "10000"):
                return payload

            action = classify(infocode)
            message = describe(infocode) or str(payload.get("info") or "未知错误")
            error = AmapError(message, infocode=infocode or None, action=action)
            if action == "retry":
                last_error = error
                continue
            raise error

        raise last_error or AmapError("请求失败（无详细信息）", action="retry")

    # -- 对外 ------------------------------------------------------

    def search_page(
        self, keyword: str, region: str, page: int = 1, offset: int = MAX_OFFSET
    ) -> SearchPage:
        """取一页。空结果返回 ``pois=[]`` 的 SearchPage，不抛异常。"""
        offset = max(1, min(int(offset), MAX_OFFSET))
        payload = self._request(keyword, region, page, offset)

        pois = payload.get("pois") or []
        if isinstance(pois, dict) and not pois:
            pois = []
        if not isinstance(pois, list):
            raise AmapError("pois 字段结构异常：%r" % (type(pois).__name__,), action="skip")

        try:
            count = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            count = 0

        return SearchPage(
            keyword=keyword,
            region=region,
            page=page,
            pois=pois,
            count=count,
            infocode=str(payload.get("infocode", "")),
        )

    def iter_all(
        self, keyword: str, region: str, offset: int = MAX_OFFSET, max_pages: int = 50
    ):
        """一页页翻到返回 0 条为止。产出 :class:`SearchPage`。

        不用 ``count`` 决定页数（不可信）；唯一终止条件是「本页 0 条」。
        ``max_pages`` 只是防死循环的保险丝，正常在 9 页内结束。
        """
        page = 1
        while page <= max_pages:
            result = self.search_page(keyword, region, page, offset)
            yield result
            if result.is_empty:
                return
            page += 1
