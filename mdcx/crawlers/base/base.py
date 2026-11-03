import asyncio
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never

from parsel import Selector

from mdcx.config.models import Website
from mdcx.models.crawl_status import (
    QUALITY_FIELDS,
    CrawlStatus,
    SiteReport,
    observations,
    record_parse_error,
    record_request_error,
    record_status,
    request_cache,
)
from mdcx.models.types import CrawlerInput, CrawlerResponse, CrawlerResult
from mdcx.web_async import CrawlRequestState, SiteCircuitOpen, crawl_request_state

from .types import Context, CralwerException, CrawlerData, CrawlerNotFound, is_valid

if TYPE_CHECKING:
    from mdcx.web_async import AsyncWebClient


@dataclass
class _NumberBudget:
    remaining: float
    deadline: float = 0
    active: int = 0
    wall_deadline: float = 0


class GenericBaseCrawler[T: Context = Context](ABC):
    """
    爬虫基类. 所有具体爬虫均应继承此类并实现其抽象方法.

    Crawler 实例的生命周期是一次刮削任务, 即刮削一批文件. 此次任务内对同一网站的请求将使用同一实例.
    所有与单次爬取相关的数据均应存储在 `Context` 中, 可在所有方法中通过 `ctx` 参数访问.

    `Context` 是默认类型, 必要时可实现子类并通过泛型参数 T 指定.

    由于爬取逻辑因网站而异, 在最极端情况下可以重写 `_run` 方法以完全自定义爬取流程.
    """

    def __init__(self, client: "AsyncWebClient", base_url: str = "", browser=None):
        """
        初始化爬虫实例.

        Args:
            client (AsyncWebClient): 异步 HTTP 客户端, 用于发送请求.
            base_url (str, optional): 基础 URL, 用于支持自定义 URL. 不提供则使用默认值.
            browser (_type_, optional): 保留的兼容参数, 当前主流程不再使用浏览器请求.
        """
        self.async_client = client
        self.base_url: str = base_url or self.base_url_()
        self.browser = browser
        self._site_failure = ""
        self._skip_logged = False
        self._number_budgets: dict[str, _NumberBudget] = {}

    async def close(self):
        """释放资源."""
        return None

    @classmethod
    @abstractmethod
    def site(cls) -> Website:
        """此爬虫对应的网站枚举值."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def base_url_(cls) -> str:
        """默认 URL, 结尾无斜杠. 可以通过 self.base_url 访问实际值."""
        raise NotImplementedError

    @classmethod
    def display_name(cls) -> str:
        """前端展示名称, 默认使用网站枚举值."""
        return cls.site().value

    @classmethod
    def hidden_in_ui(cls) -> bool:
        """是否在前端站点枚举中隐藏."""
        return False

    @classmethod
    def supports_custom_url(cls) -> bool:
        """是否支持在前端配置自定义网址."""
        return True

    @abstractmethod
    def new_context(self, input: CrawlerInput) -> T:
        raise NotImplementedError

    def is_stopped(self) -> bool:
        if self._site_failure:
            return True
        ban_states = getattr(self.async_client, "_site_ban_states", {})
        state = ban_states.get(self.site().value)
        return bool(state and state.circuit_open)

    def reset_wall_timeout(self, number: str) -> None:
        """Only the formal batch-end retry starts a new wall-clock round."""
        budget = self._number_budgets.get(number)
        if budget is not None:
            budget.wall_deadline = 0

    async def run(self, input: CrawlerInput) -> CrawlerResponse:
        issues = []
        token = observations.set(issues)
        cache_token = request_cache.set(getattr(input, "request_cache", None))
        try:
            response = await self._run_with_budget(input)
            if response.data is not None:
                for name in QUALITY_FIELDS:
                    value = getattr(response.data, name)
                    valid = (
                        isinstance(value, list) and all(isinstance(item, str) for item in value)
                        if name == "actors"
                        else isinstance(value, str)
                    )
                    if not valid:
                        record_parse_error(f"字段 {name} 类型错误")
                        setattr(response.data, name, [] if name == "actors" else "")
            error = response.debug_info.error
            if isinstance(error, SiteCircuitOpen):
                record_status(CrawlStatus.SKIPPED, error)
            elif error is not None:
                status = getattr(error, "status", None)
                if isinstance(error, TimeoutError):
                    status = CrawlStatus.REQUEST_ERROR
                # A terminal HTTP 404 is normal absence even if a legacy caller wrapped it.
                if status == CrawlStatus.REQUEST_ERROR:
                    if not any(i.status == CrawlStatus.PARSE_ERROR for i in issues):
                        record_request_error(error)
                elif status is not None:
                    record_status(status, error)
                elif not issues:
                    record_status(CrawlStatus.PARSE_ERROR, error)
            if response.data is None and not issues:
                record_status(CrawlStatus.NOT_FOUND, "无匹配结果")
            missing_status = next(
                (
                    i.status
                    for i in issues
                    if i.status in {CrawlStatus.REQUEST_ERROR, CrawlStatus.PARSE_ERROR, CrawlStatus.SKIPPED}
                ),
                CrawlStatus.NOT_FOUND
                if response.data is None
                or (not response.data.title and any(i.status == CrawlStatus.NOT_FOUND for i in issues))
                else CrawlStatus.MISSING,
            )
            response.report = SiteReport(
                self.site().value,
                issues,
                {
                    name: missing_status
                    for name in QUALITY_FIELDS
                    if not response.data or not getattr(response.data, name)
                },
            )
            return response
        finally:
            observations.reset(token)
            request_cache.reset(cache_token)

    async def _run_with_budget(self, input: CrawlerInput) -> CrawlerResponse:
        """
        执行爬虫任务.

        此方法负责初始化 `Context`, 处理异常, 记录调试信息等. 具体执行流程应在 `_run` 方法中实现.
        """
        start_time = time.time()
        ctx = self.new_context(input)
        ctx.debug(f"{input=}")

        if self._site_failure:
            if not self._skip_logged:
                self.async_client._log(f"⏭ [断路器] {self.site().value} 本次运行跳过后续番号：{self._site_failure}")
                self._skip_logged = True
            ctx.debug_info.error = SiteCircuitOpen(self._site_failure)
            return CrawlerResponse(debug_info=ctx.debug_info)

        # Keep lightweight HTTP clients used by parser callers compatible.
        total_timeout = getattr(self.async_client, "site_total_timeout", 120)
        wall_timeout = getattr(self.async_client, "site_wall_timeout", 180)
        loop = asyncio.get_running_loop()
        budget = self._number_budgets.setdefault(input.number, _NumberBudget(remaining=total_timeout))
        if not budget.wall_deadline:
            budget.wall_deadline = loop.time() + wall_timeout
        if budget.active == 0:
            budget.deadline = loop.time() + budget.remaining
        budget.active += 1
        state = CrawlRequestState(
            is_blocked=lambda: bool(self._site_failure),
            budget=budget,
            site_name=self.site().value if hasattr(self.site(), "value") else str(self.site()),
            open_circuit=self._open_circuit,
        )
        token = crawl_request_state.set(state)
        try:
            if loop.time() >= budget.wall_deadline:
                raise TimeoutError
            async with asyncio.timeout_at(budget.wall_deadline):
                async with asyncio.timeout_at(budget.deadline) as timeout_ctx:
                    state.timeout_ctx = timeout_ctx
                    data = await self._run(ctx)
            if data is None and state.request_failed and not state.is_ban_failure:
                self._open_circuit("允许的请求尝试已耗尽，未取得页面")
            return CrawlerResponse(data=data, debug_info=ctx.debug_info)
        except TimeoutError:
            reason = (
                f"单站单番号墙钟超限 ({wall_timeout:g}s)，番号={input.number}"
                if loop.time() >= budget.wall_deadline
                else f"单站单番号总等待超限 ({total_timeout:g}s)，番号={input.number}"
            )
            ctx.debug(reason)
            ctx.debug_info.error = TimeoutError(reason)
            return CrawlerResponse(debug_info=ctx.debug_info)
        except Exception as e:
            ctx.debug(traceback.format_exc())
            ctx.debug_info.error = e
            if state.request_failed and not state.is_ban_failure:
                self._open_circuit("允许的请求尝试已耗尽，未取得页面")
            return CrawlerResponse(debug_info=ctx.debug_info)
        finally:
            budget.active -= 1
            if budget.active == 0:
                budget.remaining = max(budget.deadline - loop.time(), 0)
            crawl_request_state.reset(token)
            ctx.debug_info.execution_time = time.time() - start_time

    def _open_circuit(self, reason: str) -> None:
        if not self._site_failure:
            self._site_failure = reason
            self.async_client._log(f"🚫 [断路器] {self.site().value} 本次运行停用：{reason}")

    async def _run(self, ctx: T):
        if not ctx.input.appoint_url:
            search_urls = await self._generate_search_url(ctx)
            if not search_urls:
                raise CralwerException("生成搜索页 URL 失败")
            if isinstance(search_urls, str):
                search_urls = [search_urls]
            ctx.debug(f"搜索页 URL: {search_urls}")
            ctx.debug_info.search_urls = search_urls

            detail_urls = await self._search(ctx, search_urls)
            if not detail_urls:
                raise CrawlerNotFound("搜索无匹配结果")
        else:
            detail_urls = [ctx.input.appoint_url]
            ctx.debug(f"使用指定详情页 URL: {ctx.input.appoint_url}")

        ctx.debug_info.detail_urls = detail_urls
        data = await self._detail(ctx, detail_urls)
        if not data:
            raise CralwerException("获取详情页数据失败")
        if not is_valid(data.title):
            normal_absence = any(i.status == CrawlStatus.NOT_FOUND for i in observations.get() or [])
            if not normal_absence:
                record_parse_error("详情页未取得有效标题")
        data.source = self.site().value  # todo use Enum directly
        data = data.to_result()
        return await self.post_process(ctx, data)

    async def _search(self, ctx: T, search_urls: list[str]) -> list[str] | None:
        for search_url in search_urls:
            html, error = await self._fetch_search(ctx, search_url)
            if html is None:
                record_request_error(error)
                ctx.debug(f"搜索页请求失败: {error=}")
                continue
            ctx.debug(f"搜索页请求成功: {search_url=}")
            selector = Selector(text=html)
            detail_urls = await self._parse_search_page(ctx, selector, search_url)
            if detail_urls:
                ctx.debug(f"详情页 URL: {detail_urls}")
                return detail_urls if isinstance(detail_urls, list) else [detail_urls]

    async def _detail(self, ctx: T, detail_urls: list[str]) -> CrawlerData | None:
        for detail_url in detail_urls:
            html, error = await self._fetch_detail(ctx, detail_url)
            if html is None:
                record_request_error(error)
                ctx.debug(f"详情页请求失败: {error=}")
                continue
            ctx.debug(f"详情页请求成功: {detail_url=}")
            selector = Selector(text=html)
            scraped_data = await self._parse_detail_page(ctx, selector, detail_url)
            if scraped_data and not scraped_data.external_id:
                scraped_data.external_id = detail_url
            return scraped_data
        raise CrawlerNotFound("详情页无可用响应")

    @abstractmethod
    async def _generate_search_url(self, ctx: T) -> list[str] | str | None:
        """
        生成搜索 URL. 如果重写 `_run` 则无须实现此方法.
        """
        raise NotImplementedError

    @abstractmethod
    async def _parse_search_page(self, ctx: T, html: Selector, search_url: str) -> list[str] | str | None:
        """
        解析搜索结果页, 获取详情页 URL. 如果重写 `_search` 则无须实现此方法.

        此方法应返回完整 URL, 当解析页面获取到相对 URL 时需进行处理.

        Args:
            html (Selector): 包含搜索结果页 HTML 的 parsel Selector 对象.
            search_url (str): 搜索页 URL.

        Returns:
            detail_urls: 一个或多个详情页的 URL, 如果找不到则返回 None.
        """
        raise NotImplementedError

    @abstractmethod
    async def _parse_detail_page(self, ctx: T, html: Selector, detail_url: str) -> CrawlerData | None:
        """
        解析详情页获取数据. 如果重写 `_detail` 则无须实现此方法.

        Args:
            html (Selector): 包含详情页 HTML 的 parsel Selector 对象.
            detail_url (str): 详情页 URL.

        Returns:
            爬取数据对象, 如果解析失败则返回 None.
        """
        raise NotImplementedError

    async def post_process(self, ctx: T, res: CrawlerResult) -> CrawlerResult:
        """
        爬取并解析完成后对结果进行后处理.

        Args:
            res (CrawlerResult): 爬取结果对象.
        """
        return res

    async def _fetch_search(self, ctx: T, url: str, use_browser: bool | None = False) -> tuple[str | None, str]:
        """
        获取搜索页. 此方法不应抛出异常.
        """
        return await self._fetch(ctx, url, use_browser)

    async def _fetch_detail(self, ctx: T, url: str, use_browser: bool | None = False) -> tuple[str | None, str]:
        """
        获取详情页. 此方法不应抛出异常.
        """
        return await self._fetch(ctx, url, use_browser)

    async def _fetch(self, ctx: T, url: str, use_browser: bool | None) -> tuple[str | None, str]:
        if use_browser is True:
            return None, "当前版本已移除浏览器请求模式"
        return await self.async_client.get_text(url, headers=self._get_headers(ctx), cookies=self._get_cookies(ctx))

    def _get_cookies(self, ctx: T) -> dict[str, str] | None:
        return None

    def _get_headers(self, ctx: T) -> dict[str, str] | None:
        return None


class BaseCrawler(GenericBaseCrawler[Context]):
    def new_context(self, input: CrawlerInput) -> Context:
        return Context(input=input)


crawler_registry: dict[Website, type[GenericBaseCrawler[Never]]] = {}


def register_crawler(crawler_cls: type[GenericBaseCrawler[Any]]):
    crawler_registry[crawler_cls.site()] = crawler_cls


def get_crawler(site: Website) -> type[GenericBaseCrawler[Never]] | None:
    """
    获取指定网站的爬虫类.

    注意: 出于类型安全的目的, 将返回类型标注为 `GenericBaseCrawler[Never]`.
    由于允许子类继承 `Context` 作为泛型, 因此实际上没有类型可以准确标注此方法的返回值.

    在应用内部, 只有 `run` 被视为公开 API 调用, `Context` 实际上是内部实现细节, 因此这种标注不会导致问题.
    在测试等情况下, 如果需要调用具有 `ctx` 参数的方法, 必须使用返回类的 `new_context` 类方法创建具体使用的泛型类并传入.
    """
    return crawler_registry.get(site)


def get_registered_crawler_sites(*, include_hidden: bool = False) -> list[Website]:
    """返回已注册刮削器的网站列表, 供前端和配置 schema 枚举使用."""
    sites: list[Website] = []
    for site, crawler_cls in crawler_registry.items():
        if include_hidden or not crawler_cls.hidden_in_ui():
            sites.append(site)
    return sites
