import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import project_research
import weekly_pipeline


REPO = "deepseek-ai/dsh-superagent-prototype"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
ROOT = f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/"


def encoded_readme(text):
    return json.dumps({
        "encoding": "base64",
        "path": "README.md",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    })


class ProjectEvidenceTests(unittest.TestCase):
    def collect(self, readme, documents=None):
        documents = documents or {}
        api = f"https://api.github.com/repos/{REPO}"
        calls = []

        def fetch(url):
            calls.append(url)
            if url == api:
                return json.dumps({"default_branch": "develop"})
            if url == api + "/commits/develop":
                return json.dumps({"sha": COMMIT})
            if url == api + "/readme?ref=" + COMMIT:
                return encoded_readme(readme)
            if url in documents:
                value = documents[url]
                if isinstance(value, Exception):
                    raise value
                return value
            raise AssertionError(f"unexpected fetch: {url}")

        result = project_research.collect_project_evidence({"repo": REPO}, fetch)
        return result, calls

    def test_readme_and_internal_docs_are_pinned_to_resolved_commit(self):
        readme = ("# Project\n" + "Useful overview details. " * 10 +
                  "\n[Execution](docs/003-execution.md)\n[Panel](docs/005-panel.md)")
        docs = {
            ROOT + "docs/003-execution.md": "Execution details",
            ROOT + "docs/005-panel.md": "Panel details",
        }
        result, calls = self.collect(readme, docs)

        self.assertEqual(result["commit"], COMMIT)
        self.assertEqual({source["url"] for source in result["sources"]},
                         {ROOT + "README.md", ROOT + "docs/003-execution.md", ROOT + "docs/005-panel.md"})
        self.assertTrue(all(COMMIT in source["url"] for source in result["sources"]))
        self.assertNotIn("/commits/develop/", " ".join(source["url"] for source in result["sources"]))
        self.assertIn("https://api.github.com/repos/" + REPO + "/commits/develop", calls)

    def test_cross_repository_and_external_markdown_links_are_not_fetched(self):
        readme = ("# Project\n" + "Useful overview details. " * 10 +
                  "\n[Other repository](https://github.com/other/project/blob/main/docs/guide.md)" +
                  "\n[External guide](https://example.com/guide.md)")
        result, calls = self.collect(readme)

        self.assertEqual(len(result["sources"]), 1)
        self.assertFalse(any("other/project" in url or "example.com/guide.md" in url for url in calls))

    def test_changelog_is_not_selected(self):
        readme = ("# Project\n" + "Useful overview details. " * 10 +
                  "\n[Changelog](CHANGELOG.md)\n[Usage](docs/usage.md)")
        result, calls = self.collect(readme, {ROOT + "docs/usage.md": "Usage details"})

        self.assertNotIn(ROOT + "CHANGELOG.md", calls)
        self.assertNotIn(ROOT + "CHANGELOG.md", {source["url"] for source in result["sources"]})
        self.assertIn(ROOT + "docs/usage.md", {source["url"] for source in result["sources"]})

    def test_failed_document_does_not_drop_readme_or_other_documents(self):
        readme = ("# Project\n" + "Useful overview details. " * 10 +
                  "\n[Execution](docs/003-execution.md)\n[Panel](docs/005-panel.md)" +
                  "\n[Authorization](docs/002-authorization.md)")
        docs = {
            ROOT + "docs/003-execution.md": RuntimeError("temporarily unavailable"),
            ROOT + "docs/005-panel.md": "Panel details",
            ROOT + "docs/002-authorization.md": "Authorization details",
        }
        result, _ = self.collect(readme, docs)

        paths = {source["path"] for source in result["sources"]}
        self.assertEqual(paths, {"README.md", "docs/005-panel.md", "docs/002-authorization.md"})
        self.assertEqual(result["unavailable_sources"], [ROOT + "docs/003-execution.md"])

    def test_very_short_readme_is_rejected(self):
        api = f"https://api.github.com/repos/{REPO}"

        def fetch(url):
            if url == api:
                return json.dumps({"default_branch": "main"})
            if url == api + "/commits/main":
                return json.dumps({"sha": COMMIT})
            if url == api + "/readme?ref=" + COMMIT:
                return encoded_readme("# Tiny\nToo short.")
            raise AssertionError(f"unexpected fetch: {url}")

        with self.assertRaisesRegex(RuntimeError, "项目资料不足"):
            project_research.collect_project_evidence({"repo": REPO}, fetch)


class TitleBoundaryTests(unittest.TestCase):
    def validate_title(self, title):
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            image_dir = folder / "images"
            image_dir.mkdir()
            for index in range(3):
                (image_dir / f"{index + 1:02d}.png").write_bytes(b"image")
            (folder / "post.json").write_text(json.dumps({
                "editorial": {"title": title, "content": "项目介绍。"},
                "author": "MJZHOU",
            }, ensure_ascii=False), encoding="utf-8")
            weekly_pipeline.validate_package(folder)

    def test_recommended_title_format_passes(self):
        title = "每周 Github 项目推荐 - DSH"
        self.assertTrue(title.startswith(weekly_pipeline.TITLE_PREFIX))
        self.assertLessEqual(weekly_pipeline.title_units(title), 40)
        self.validate_title(title)

    def test_mixed_chinese_english_title_at_40_units_passes(self):
        prefix = weekly_pipeline.TITLE_PREFIX
        remaining = 40 - weekly_pipeline.title_units(prefix)
        title = prefix + "AI 开源工具 " + "x" * (remaining - weekly_pipeline.title_units("AI 开源工具 "))
        self.assertEqual(weekly_pipeline.title_units(title), 40)
        self.validate_title(title)

    def test_title_at_41_units_is_rejected(self):
        prefix = weekly_pipeline.TITLE_PREFIX
        title = prefix + "x" * (41 - weekly_pipeline.title_units(prefix))
        self.assertEqual(weekly_pipeline.title_units(title), 41)
        with self.assertRaisesRegex(ValueError, "贴图标题超过20字"):
            self.validate_title(title)


if __name__ == "__main__":
    unittest.main()
