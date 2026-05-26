from io import BytesIO
from zipfile import ZipFile

import pytest

from common.skill_bundle import SkillBundleError, inspect_skill_bundle, parse_skill_markdown


def make_zip(files):
    buf = BytesIO()
    with ZipFile(buf, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buf.getvalue()


def test_inspect_skill_bundle_extracts_frontmatter_and_hash():
    bundle_bytes = make_zip({
        "docs/SKILL.md": """---
name: kcs-korean-formal-tone
description: 한국어 공문체로 답변합니다.
version: 1.0.0
allowed_tools:
  - read
---

# Usage

항상 정중하고 간결하게 답변합니다.
""",
    })

    bundle = inspect_skill_bundle(bundle_bytes)

    assert bundle.slug == "kcs-korean-formal-tone"
    assert bundle.version == "1.0.0"
    assert bundle.frontmatter["allowed_tools"] == ["read"]
    assert bundle.skill_md_path == "docs/SKILL.md"
    assert len(bundle.sha256) == 64
    assert "정중하고 간결" in bundle.body_excerpt


def test_parse_skill_markdown_requires_semver_and_kebab_name():
    frontmatter, _ = parse_skill_markdown("""---
name: valid-skill
description: ok
version: 1.2.3
---
body
""")
    assert frontmatter["name"] == "valid-skill"

    bad_bundle = make_zip({
        "SKILL.md": """---
name: Invalid Skill
description: ok
version: one
---
body
""",
    })
    with pytest.raises(SkillBundleError):
        inspect_skill_bundle(bad_bundle)


def test_inspect_skill_bundle_rejects_path_traversal():
    bad_bundle = make_zip({
        "../SKILL.md": """---
name: bad-skill
description: ok
version: 1.0.0
---
body
""",
    })
    with pytest.raises(SkillBundleError):
        inspect_skill_bundle(bad_bundle)
