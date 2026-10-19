"""Complete transferable Skill package factories."""

import base64

from team_skills.transfer.packages import make_zip, package


def bundle(name="demo", body="one"):
    return package(
        name,
        {
            "SKILL.md": f"---\nname: {name}\ndescription: Demo\n---\n{body}\n".encode(),
            "scripts/run.py": b"print('hello')\n",
            "assets/data.bin": bytes(range(256)),
        },
    )


def encoded(*items):
    return {"zip_b64": base64.b64encode(make_zip(items)).decode()}
