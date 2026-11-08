"""Actor validity shared by source post-processing and the quality gate."""

# Existing actor-search exclusions in core/amazon.py; keep exact-name matching.
ACTOR_PLACEHOLDERS = frozenset({"未知演员", "未知演員", "女优不明", "女優不明", "人物不明"})


def valid_actor_names(actors):
    return [
        name
        for actor in actors or []
        if isinstance(actor, str)
        and (name := actor.strip())
        and name not in ACTOR_PLACEHOLDERS
        and any(char.isalnum() for char in name)
    ]
