from types import SimpleNamespace

import pytest
from parsel import Selector

from mdcx.crawlers.javdb_new import Parser


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor_html, actors, all_actors",
    [
        (
            '<span class="value"><a class="actor-female" href="/actors/fiction-a">虚构演员甲</a>'
            '<a class="actor-female" href="/actors/fiction-b">虚构演员乙</a></span>',
            ["虚构演员甲", "虚构演员乙"],
            ["虚构演员甲", "虚构演员乙"],
        ),
        (
            '<span class="value"><a href="/actors/fiction-a">虚构演员甲</a><strong class="female">♀</strong>'
            '<a href="/actors/fiction-b">虚构演员乙</a><strong class="male">♂</strong></span>',
            ["虚构演员甲"],
            ["虚构演员甲", "虚构演员乙"],
        ),
        (
            '<span class="value"><a href="/actors/fiction-b">虚构演员乙</a><strong class="male">♂</strong></span>',
            [],
            ["虚构演员乙"],
        ),
        ('<span class="value"></span>', [], []),
        ('<span class="value"><a href="/makers/fiction">虚构片商</a></span>', [], []),
    ],
    ids=["new-female", "legacy-mixed", "legacy-male-only", "empty", "unrelated-links"],
)
async def test_actor_structures(actor_html, actors, all_actors):
    html = Selector(
        '<h2 class="title is-4"><strong class="current-title">虚构标题</strong></h2>'
        '<a class="button is-white copy-to-clipboard" data-clipboard-text="FICTION-000"></a>'
        f'<div class="panel-block"><strong>演員:</strong>{actor_html}</div>'
    )
    parser = Parser()
    ctx = SimpleNamespace(input=SimpleNamespace(number="FICTION-000"))

    assert await parser.number(ctx, html) == "FICTION-000"
    assert await parser.actors(ctx, html) == actors
    assert await parser.all_actors(ctx, html) == all_actors
