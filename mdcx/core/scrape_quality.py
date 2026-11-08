"""Quality decisions happen before translation, image downloads, output creation or moves."""

import re

from ..config.enums import FixedScrapingType, Website
from ..models.crawl_status import ERROR_STATUSES, CrawlStatus
from ..models.types import score_to_10
from .actor_quality import valid_actor_names

REQUIRED_FIELDS = {
    FixedScrapingType.YOUMA: ("score", "outline", "actors", "thumb"),
    FixedScrapingType.WUMA: ("actors", "thumb"),
    FixedScrapingType.SUREN: ("thumb",),
    FixedScrapingType.FC2: ("thumb",),
}
FIELD_LABELS = {"score": "评分", "outline": "简介", "actors": "演员", "thumb": "封面"}


class QualityDeferred(Exception):
    pass


class QualityHeld(Exception):
    pass


def missing_required(result, scraping_type):
    missing = []
    for name in REQUIRED_FIELDS.get(scraping_type, ()):
        value = getattr(result, name, None)
        if name == "actors" and value:
            value = valid_actor_names(value)
        elif name == "score" and result:
            value = score_to_10(result.score, result.score_max)
        elif isinstance(value, str):
            value = value.strip()
        if not value:
            missing.append(name)
    return missing


async def retryable_sites(scraper):
    sites = []
    for report in scraper.reports:
        if not report.has_error or all(i.status == CrawlStatus.SKIPPED for i in report.issues):
            continue
        crawler = await scraper.crawler_provider.get(Website(report.site))
        if not getattr(crawler, "is_stopped", lambda: False)():
            sites.append(report.site)
    return sites


def error_summary(reports):
    lines = []
    for report in reports:
        for issue in report.issues:
            if issue.status not in ERROR_STATUSES:
                continue
            # The public summary needs causes, not URL query values or response bodies.
            reason = re.sub(r"https?://\S+", "[URL]", issue.reason)
            reason = re.sub(r"(API Token[^:：]*[:：]).*", r"\1 [已隐藏]", reason)
            reason = reason.split("input_value=", 1)[0]
            reason = reason.split("response=", 1)[0].split("响应正文", 1)[0]
            lines.append(f"{report.site}: {issue.status} ({reason[:160]})")
    return "；".join(dict.fromkeys(lines))


def recovery_summary(number, before, after, reports, *, retried, held):
    recovered = []
    if after:
        for name in FIELD_LABELS:
            new_value = getattr(after, name)
            old_value = getattr(before, name, None)
            source = next((v for k, v in after.field_sources.items() if k.value == name), "")
            old_source = next((v for k, v in before.field_sources.items() if k.value == name), "") if before else ""
            if new_value and (not old_value or old_source != source):
                recovered.append(f"{FIELD_LABELS[name]} ← {source or '未知来源'}")
    state = "批末重刮一次" if retried else "出错站已停，不重刮"
    disposition = "留原地，缺 " + "、".join(FIELD_LABELS[n] for n in held) if held else "字段齐，入库"
    return f"{number}: {state}；补回 {'、'.join(recovered) or '无'}；{disposition}；{error_summary(reports)}"
