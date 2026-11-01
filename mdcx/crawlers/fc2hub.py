#!/usr/bin/env python3
import json
import re
from typing import override

from lxml import etree

from mdcx.models.crawl_status import record_parse_error

from ..config.enums import FieldRule, Website
from ..config.manager import manager
from ..config.resource_policy import is_trailer_output_used
from ..signals import signal
from .base import BaseCrawler, Context, CralwerException, CrawlerData
from .base.types import CrawlerNotFound, CrawlerRequestError


def getTitle(html):  # 获取标题
    result = html.xpath("//h1/text()")
    result = result[1] if result else ""
    return result


def getNum(html):  # 获取番号
    result = html.xpath("//h1/text()")
    result = result[0] if result else ""
    return result


def getCover(html):  # 获取封面
    result = html.xpath('//a[@data-fancybox="gallery"]/@href')
    result = result[0] if result else ""
    result = "https:" + result if result.startswith("//") else result
    return result


def getExtraFanart(html):  # 获取剧照
    result = html.xpath('//div[@style="padding: 0"]/a/@href')
    result = ["https:" + u if u.startswith("//") else u for u in result]
    return result


def getStudio(html):  # 使用卖家作为厂家
    result = html.xpath('//div[@class="col-8"]/text()')
    if result:
        result = result[0].strip()
    return result


def getTag(html):  # 获取标签
    result = html.xpath('//p[@class="card-text"]/a[contains(@href, "/tag/")]/text()')
    result = str(result).strip(" []").replace(", ", ",").replace("'", "").strip() if result else ""
    return result


_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\b|;|$)|height\s*:\s*0(?:\b|px|;|$)",
    re.IGNORECASE,
)
_WATERMARK_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
_INLINE_SCRIPT_RE = re.compile(
    r"\(function\s*\(\)\s*\{.*?window\.__CF\$cv\$params.*?\}\)\(\)\s*;?",
    re.DOTALL,
)
_DIVIDER_LINE_RE = re.compile(r"^(?:[-ー━─═=*+＋]{4,}|[〇○●◇◆□■△▲▽▼☆※]{4,})$")
_PROMO_TRUNCATE_RE = re.compile(
    r"^(?:"
    r"☆今回の購入者レビュープレゼントは|"
    r"レビュー特典は|"
    r"特典では|"
    r"\[動画情報\]|"
    r"\[注意事項\]|"
    r"※注意事項※|"
    r"※この作品は.*フィクション|"
    r"・この作品は.*フィクション|"
    r"・登場モデル.*18歳以上|"
    r"・本作品.*著作権|"
    r"本作品に登場する人物は18歳以上|"
    r"本作品に登場する人物は同意の上|"
    r"画像、動画等の無断転載|"
    r"日本の法律.*遵守|"
    r"出演モデルの同意の上|"
    r"第三者に提供.*禁止|"
    r"こちらの作品もオススメです"
    r")"
)
_TOP_NOISE_LINE_RE = re.compile(
    r"^(?:"
    r"※サンプル(?:画像|動画)は.*(?:モザイク|ボカシ).*|"
    r"(?:\d+\s*)?[⇨⇒→]\s*\d+.*|"
    r"◎(?:本作品|海外法人|作品に登場|映像は18歳|2次転売|RE-UPLOAD|未經同意|諸事情|価格は予告).*|"
    r"・本作品のモデル.*18歳.*|"
    r"・二次使用.*禁止.*|"
    r"■購入特典.*|"
    r"ご購入後、?レビューを投稿.*|"
    r"特典として高画質版.*|"
    r"※(?:高画質ZIP|.*配信対策|無断の転載対策|特典動画はファイルサイズ).*|"
    r"お早めのご購入.*Zipダウンロード.*|"
    r"早期購入者全員に.*|"
    r"まだレビュー特典の.*|"
    r"購入後、レビューを記載.*|"
    r"プレゼントさせていただき.*|"
    r"※無断転載.*厳禁.*|"
    r"★+本編完全無修正★+.*|"
    r"◇収録時間[:：].*|"
    r"(?:ファイル形式|フレーム|再生時間|ビットレート|解像度|音声)[:：].*|"
    r"Windws\s*Media\s*Player\s*推奨"
    r")$"
)
_SERIES_INTRO_HEADER_RE = re.compile(r"ご存じない方のために.*馴れ初め|モデル募集掲示板で知り合いました")
_SERIES_CURRENT_RESUME_RE = re.compile(
    r"^(?:"
    r"処女喪失から.*久しぶりに会ってくれた|"
    r"(?:…|\.\.\.)?実は[０-９0-9]+回目の撮影のあと|"
    r"そんな.+との[０-９0-9]+回目のホテルデートは|"
    r"今回お届けする作品は撮影時期でいうと|"
    r"最近は.+で忙しく.*"
    r")"
)


def _is_related_work_href(href: str) -> bool:
    href = href.strip()
    return bool(re.search(r"(?:^|/)(?:a/\d+|s/[\w-]+|id\d+)", href) or "contents.fc2.com/article" in href)


def _strip_series_catalog_paragraphs(paragraphs: list[str], last_prequel_link_line_idx: int | None) -> list[str]:
    intro_idx = None
    for idx, line in enumerate(paragraphs):
        if _SERIES_INTRO_HEADER_RE.search(line):
            intro_idx = idx
            break
    if intro_idx is None:
        return paragraphs

    search_start = (
        intro_idx + 1 if last_prequel_link_line_idx is None else max(intro_idx + 1, last_prequel_link_line_idx)
    )
    resume_idx = None
    for idx in range(len(paragraphs) - 1, search_start - 1, -1):
        if _SERIES_CURRENT_RESUME_RE.search(paragraphs[idx]):
            resume_idx = idx
            break
    if resume_idx is None:
        return paragraphs

    return paragraphs[:intro_idx] + paragraphs[resume_idx:]


def _extract_paragraph_lines(node: etree._Element) -> tuple[list[str], int | None]:
    lines: list[str] = []
    buf: list[str] = []
    truncated = False
    seen_gallery_image = False
    last_prequel_link_line_idx: int | None = None

    def flush():
        nonlocal buf
        text = "".join(buf)
        buf = []
        text = _INLINE_SCRIPT_RE.sub("", text)
        for raw_part in text.splitlines():
            part = re.sub(r"^・+", "", raw_part.strip()).strip()
            if not part or _WATERMARK_TOKEN_RE.match(part):
                continue
            lines.append(part)

    def append_text(val: str | None):
        if not val:
            return
        parts = val.splitlines()
        for i, part in enumerate(parts):
            if i > 0:
                flush()
            buf.append(part)

    def visit(el: etree._Element):
        nonlocal truncated, seen_gallery_image, last_prequel_link_line_idx
        if truncated:
            return
        if not isinstance(el.tag, str):
            return
        tag = el.tag.lower()
        if tag in ("script", "style", "noscript", "iframe", "strike", "del"):
            return
        if tag == "hr":
            flush()
            truncated = True
            return

        style = el.get("style") or ""
        if _HIDDEN_STYLE_RE.search(style):
            return
        if el.get("data-id") and tag == "div":
            return

        if tag == "a":
            href = el.get("href") or ""
            if _is_related_work_href(href):
                flush()
                if not seen_gallery_image:
                    last_prequel_link_line_idx = len(lines)
                if lines and re.search(r"(?:おすすめ|オススメ|関連作品|前作|過去作)", lines[-1]):
                    lines.pop()
                return
            if el.get("data-fancybox") == "des" or "storage" in href:
                seen_gallery_image = True

        if tag in ("b", "strong"):
            bold_text = "".join(el.itertext()).strip()
            if re.search(r"おすすめVR|オススメ作品|関連作品", bold_text):
                return

        is_block = tag in (
            "p",
            "div",
            "section",
            "article",
            "blockquote",
            "li",
            "ul",
            "ol",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        )
        if is_block:
            flush()
        elif tag == "br":
            flush()

        append_text(el.text)
        for child in el:
            if truncated:
                break
            visit(child)
            append_text(child.tail)

        if is_block:
            flush()

    visit(node)
    flush()
    return lines, last_prequel_link_line_idx


def getOutline(html):  # 获取简介
    des_nodes = html.xpath('//div[contains(@class, "col") and contains(@class, "des")]')
    if not des_nodes:
        return ""

    raw_lines, last_prequel_link_line_idx = _extract_paragraph_lines(des_nodes[0])
    raw_lines = _strip_series_catalog_paragraphs(raw_lines, last_prequel_link_line_idx)

    cleaned_lines: list[str] = []
    for line in raw_lines:
        if _DIVIDER_LINE_RE.match(line):
            if cleaned_lines:
                break
            continue
        if _PROMO_TRUNCATE_RE.search(line):
            break
        if _TOP_NOISE_LINE_RE.match(line):
            continue
        cleaned_lines.append(line)

    while cleaned_lines and re.search(r"こちらの作品もオススメです[↓⬇]*$", cleaned_lines[-1]):
        cleaned_lines.pop()

    return "\n".join(cleaned_lines).strip()


def getMosaic(tag, title):  # 获取马赛克
    result = "无码" if "無修正" in tag or "無修正" in title else "有码"
    return result


def getTrailerVideoId(html, number):  # 获取 FC2 视频 ID
    result = html.xpath(
        '//div[contains(@class, "player-api")]/@data-id'
        ' | //iframe[contains(@data-src, "/embed/")]/@data-src'
        ' | //iframe[contains(@src, "/embed/")]/@src'
    )
    for item in result:
        item = str(item).strip()
        if not item:
            continue
        if item.isdigit():
            return item
        matched = re.search(r"/embed/(\d+)", item)
        if matched:
            return matched.group(1)
    return number


async def getTrailer(client, html, number):  # 获取预告片
    if not is_trailer_output_used():
        return ""
    fc2_video_id = getTrailerVideoId(html, number)
    # FC2 sample 接口返回的是带 mid 参数的临时直链，适合立即下载，不适合长期固化。
    # 注意 path 上的 mid 参数不能丢，否则直链会返回 403。
    req_url = f"https://adult.contents.fc2.com/api/v2/videos/{fc2_video_id}/sample"
    response, error = await client.get_text(req_url)
    if response is None:
        return ""
    try:
        data = json.loads(response)
    except Exception as parse_error:
        record_parse_error(parse_error)
        return ""

    trailer_url = data.get("path")
    if isinstance(trailer_url, str) and trailer_url.startswith("http"):
        return trailer_url
    return ""


def normalize_fc2_number(number: str) -> str:
    return number.upper().replace("FC2PPV", "").replace("FC2-PPV-", "").replace("FC2-", "").replace("-", "").strip()


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Fc2hubCrawler(BaseCrawler):
    @classmethod
    @override
    def site(cls) -> Website:
        return Website.FC2HUB

    @classmethod
    @override
    def base_url_(cls) -> str:
        return manager.config.get_site_url(Website.FC2HUB, "https://javten.com")

    @override
    async def _run(self, ctx: Context):
        number = normalize_fc2_number(ctx.input.number)
        real_url = ctx.input.appoint_url
        if not real_url:
            search_url = self.base_url + "/search?kw=" + number
            ctx.debug(f"搜索地址: {search_url}")
            ctx.debug_info.search_urls = [search_url]
            html_search, error = await self.async_client.get_text(search_url)
            if html_search is None:
                raise CrawlerRequestError(f"网络请求错误: {error}")
            html = etree.fromstring(html_search, etree.HTMLParser())
            real_urls = html.xpath("//link[contains(@href, $number)]/@href", number="id" + number)
            if not real_urls:
                raise CrawlerNotFound("搜索结果: 未匹配到番号！")
            language_not_jp = ["/tw/", "/ko/", "/en/"]
            for url in real_urls:
                if all(la not in url for la in language_not_jp):
                    real_url = url
                    break
            if not real_url:
                raise CrawlerNotFound("搜索结果: 未匹配到日文详情页！")

        ctx.debug(f"番号地址: {real_url}")
        ctx.debug_info.detail_urls = [real_url]
        html_content, error = await self.async_client.get_text(real_url)
        if html_content is None:
            raise CrawlerRequestError(f"网络请求错误: {error}")
        html_info = etree.fromstring(html_content, etree.HTMLParser())

        title = getTitle(html_info)
        if not title:
            raise CralwerException("数据获取失败: 未获取到title！")
        tag = getTag(html_info)
        studio = getStudio(html_info)
        trailer = await getTrailer(self.async_client, html_info, number)
        ctx.debug("预告片: 已获取到带时效参数的临时链接" if trailer else "预告片: 未获取到临时下载链接")
        if trailer:
            signal.add_log("🟡 FC2Hub 预告片链接带时效参数，仅适合立即下载，不建议长期复用远程链接。")
        actor = studio if FieldRule.FC2_SELLER in manager.config.fields_rule else ""
        outline = getOutline(html_info)

        data = CrawlerData(
            number="FC2-" + str(number),
            title=title,
            originaltitle=title,
            actors=split_csv(actor),
            outline=outline,
            originalplot=outline,
            tags=split_csv(tag),
            release="",
            year="",
            runtime="",
            score="",
            series="FC2系列",
            directors=[],
            studio=studio,
            publisher=studio,
            thumb=str(getCover(html_info)),
            poster="",
            extrafanart=getExtraFanart(html_info),
            trailer=trailer,
            image_download=False,
            mosaic=getMosaic(tag, title),
            external_id=str(real_url).strip("[]"),
            wanted="",
        )
        result = data.to_result()
        result.source = self.site().value
        ctx.debug("数据获取成功！")
        return result

    @override
    async def _generate_search_url(self, ctx: Context) -> list[str] | str | None:
        return None

    @override
    async def _parse_search_page(self, ctx: Context, html, search_url: str) -> list[str] | str | None:
        return None

    @override
    async def _parse_detail_page(self, ctx: Context, html, detail_url: str) -> CrawlerData | None:
        return None
