from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from obsidian_clipper_pipeline import (
    apply_summary_payload,
    build_paths,
    build_summary_path,
    discover_pending,
    insert_links_into_insights,
    migrate_legacy_summaries,
    parse_frontmatter,
    resolve_vault_root,
)


class ObsidianClipperPipelineTests(unittest.TestCase):
    def test_parse_frontmatter_extracts_body_and_tags(self) -> None:
        text = (
            "---\n"
            'title: "My clip"\n'
            'source: "https://example.com"\n'
            "tags:\n"
            "  - \"clippings\"\n"
            "---\n\n"
            "Body text\n"
        )
        frontmatter, body = parse_frontmatter(text)
        self.assertEqual(frontmatter["title"], "My clip")
        self.assertEqual(frontmatter["source"], "https://example.com")
        self.assertEqual(frontmatter["tags"], ["clippings"])
        self.assertEqual(body, "Body text")

    def test_insert_links_into_existing_insights_block(self) -> None:
        daily = (
            "## **Daily note : 2026-06-08**\n\n"
            "> [!NOTE] Insights\n\n"
            "> [!faq] Sonho\n"
        )
        updated = insert_links_into_insights(daily, ["[[01 - Main Notes/Insights/note.md|Note]]"])
        self.assertIn("> - [[01 - Main Notes/Insights/note.md|Note]]", updated)
        self.assertIn("> [!faq] Sonho", updated)

    def test_apply_summary_payload_writes_summary_to_insights_updates_daily_and_moves_clip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_root = Path(tmp_dir)
            paths = build_paths(vault_root)
            paths.clippings_dir.mkdir(parents=True)
            paths.daily_dir.mkdir(parents=True)
            paths.templates_dir.mkdir(parents=True)
            paths.indexes_dir.mkdir(parents=True)
            (paths.indexes_dir / "Insights.md").write_text("", encoding="utf-8")
            (paths.indexes_dir / "Trading.md").write_text("", encoding="utf-8")
            (paths.indexes_dir / "Quant.md").write_text("", encoding="utf-8")

            (paths.templates_dir / "Full Note.md").write_text(
                "{{date}} {{time}}\n\nStatus:\n\nTags:\n\n---\n\n# References\n",
                encoding="utf-8",
            )
            (paths.templates_dir / "{{date}}.md").write_text(
                "## **Daily note : {{date}}**\n\n> [!NOTE] Insights\n",
                encoding="utf-8",
            )

            clip_path = paths.clippings_dir / "clip.md"
            clip_path.write_text(
                "---\n"
                'title: "Quant trading clip"\n'
                'source: "https://example.com"\n'
                "---\n\n"
                "Trading setup with quant signal\n",
                encoding="utf-8",
            )

            payload = {
                "target_date": "2026-06-08",
                "items": [
                    {
                        "source_path": str(clip_path),
                        "source_url": "https://example.com",
                        "summary_title": "Quant trading clip",
                        "summary_markdown": "## Summary\n\nQuant and Trading idea\n\n## Key Points\n\n- Main point",
                    }
                ],
            }

            result = apply_summary_payload(vault_root, payload, send_email=False)

            summary_path = build_summary_path(paths, "Quant trading clip")
            summary_candidates = list(paths.insights_dir.glob("quant-trading-clip*.md"))
            self.assertEqual(len(summary_candidates), 1)
            summary_path = summary_candidates[0]
            self.assertTrue(summary_path.exists())
            summary_text = summary_path.read_text(encoding="utf-8")
            self.assertIn("Status: [[done]]", summary_text)
            self.assertIn("Tags: [[Insights]]", summary_text)
            self.assertIn("[[Clippings/.processed/clip.md]]", summary_text)
            self.assertIn("## Properties", summary_text)
            self.assertIn('title: Quant trading clip', summary_text)
            self.assertIn('source: https://example.com', summary_text)

            daily_path = paths.daily_dir / "20260608.md"
            self.assertTrue(daily_path.exists())
            self.assertIn(f"[[01 - Main Notes/Insights/{summary_path.name}|Quant trading clip]]", daily_path.read_text(encoding="utf-8"))

            processed_path = paths.processed_dir / "clip.md"
            self.assertTrue(processed_path.exists())
            self.assertFalse(clip_path.exists())
            self.assertEqual(result["items"][0]["processed_path"], str(processed_path))

    def test_discover_pending_ignores_processed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_root = Path(tmp_dir)
            paths = build_paths(vault_root)
            paths.clippings_dir.mkdir(parents=True)
            paths.processed_dir.mkdir(parents=True)
            (paths.clippings_dir / "keep.md").write_text("body", encoding="utf-8")
            (paths.processed_dir / "skip.md").write_text("body", encoding="utf-8")

            pending = discover_pending(vault_root)
            self.assertEqual(len(pending), 1)
            self.assertTrue(pending[0]["path"].endswith("keep.md"))

    def test_resolve_vault_root_prefers_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch.dict(os.environ, {"OBSIDIAN_VAULT_ROOT": str(root)}, clear=False):
                resolved = resolve_vault_root(None)
            self.assertEqual(resolved, root)

    def test_lock_path_stays_under_clippings_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = build_paths(Path(tmp_dir))
            expected = Path(tmp_dir) / "Clippings" / "_pipeline_state" / "clipping_summary.lock.json"
            self.assertEqual(paths.lock_path, expected)

    def test_migrate_legacy_summaries_moves_file_and_updates_daily_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_root = Path(tmp_dir)
            paths = build_paths(vault_root)
            paths.templates_dir.mkdir(parents=True)
            paths.daily_dir.mkdir(parents=True)
            paths.indexes_dir.mkdir(parents=True)
            (paths.indexes_dir / "Insights.md").write_text("", encoding="utf-8")
            (paths.indexes_dir / "Trading.md").write_text("", encoding="utf-8")
            legacy_dir = paths.storage_dir / "2026" / "Summaries"
            legacy_dir.mkdir(parents=True)

            (paths.templates_dir / "Full Note.md").write_text(
                "{{date}} {{time}}\n\nStatus:\n\nTags:\n\n---\n\n# References\n",
                encoding="utf-8",
            )

            old_summary = legacy_dir / "20260608-old.md"
            old_summary.write_text(
                "2026-06-08 23:23\n\nStatus:\n\nTags:\n\n---\n\n# FX note\n\nSource: https://example.com\nOriginal clipping: [[Clippings/.processed/clip.md]]\n\n## Summary\n\nTrading note.\n\n# References\n- [[Clippings/.processed/clip.md]]\n",
                encoding="utf-8",
            )
            daily = paths.daily_dir / "20260608.md"
            daily.write_text(
                "> [!NOTE] Insights\n> - [[05 - Storage/2026/Summaries/20260608-old.md|FX note]]\n",
                encoding="utf-8",
            )

            result = migrate_legacy_summaries(vault_root)

            self.assertEqual(len(result["migrated"]), 1)
            migrated_path = Path(result["migrated"][0]["new_path"])
            self.assertTrue(migrated_path.exists())
            self.assertFalse(old_summary.exists())
            self.assertIn(f"[[01 - Main Notes/Insights/{migrated_path.name}|FX note]]", daily.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
