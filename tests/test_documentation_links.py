"""Check local references in the canonical Markdown entrypoints."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = (
    "README.md",
    "design/CURRENT_DESIGN.md",
    "design/RESEARCH_ARCHITECTURE_V1_1.md",
    "design/RESEARCH_SCOPE.md",
    "docs/README.md",
    "docs/OPERATIONS.md",
    "docs/RELEASE_ACCEPTANCE.md",
)
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)")


class CurrentDocumentationLinks(unittest.TestCase):
    def test_local_links_resolve(self):
        for name in ACTIVE:
            source = ROOT / name
            self.assertTrue(source.is_file(), name)
            for href in LINK.findall(source.read_text(encoding="utf-8")):
                if href.startswith(("#", "/", "mailto:", "data:")) or "://" in href:
                    continue
                target = href.split("#", 1)[0].split("?", 1)[0]
                if target:
                    with self.subTest(source=name, href=href):
                        resolved = (source.parent / target).resolve()
                        self.assertTrue(
                            resolved.is_relative_to(ROOT) and resolved.is_file(),
                            f"broken documentation link: {name} -> {href}",
                        )


if __name__ == "__main__":
    unittest.main()
