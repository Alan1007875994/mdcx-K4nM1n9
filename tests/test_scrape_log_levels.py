from unittest.mock import AsyncMock

import pytest

from mdcx.config.enums import FixedScrapingType
from mdcx.core import translate
from mdcx.models.log_buffer import LogBuffer
from mdcx.models.types import CrawlersResult


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,marker", [("No Result!", "⚪"), ("Error: HTTP 503", "🔴"), ("Error: timeout", "🔴")])
async def test_avwiki_normal_absence_is_plain_but_request_errors_remain_red(monkeypatch, reason, marker):
    monkeypatch.setattr(translate.manager.config, "actor_realname", True)
    monkeypatch.setattr(translate, "get_actorname", AsyncMock(return_value=(False, reason)))
    result = CrawlersResult.empty()
    result.scraping_type = FixedScrapingType.FC2
    LogBuffer.clear_task()
    try:
        await translate.translate_actor(result)
        assert f"{marker} Av-wiki failed! {reason}" in LogBuffer.log().get()
    finally:
        LogBuffer.clear_task()
