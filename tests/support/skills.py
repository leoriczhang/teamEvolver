"""Skill bundle factories for tests."""


def _skill_md(name: str, body: str = "Do the thing.", description: str = "Use when testing.") -> bytes:
    return f"---\nname: {name}\ndescription: {description}\ncategory: coding\n---\n\n{body}\n".encode()
