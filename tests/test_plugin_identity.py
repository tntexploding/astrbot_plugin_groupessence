from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest

from group_essence_extractor.plugin_identity import (
    LEGACY_PLUGIN_NAMES,
    PLUGIN_DATABASE_FILENAME,
    PLUGIN_NAME,
    resolve_plugin_data_dir,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PluginIdentityTests(unittest.TestCase):
    def test_canonical_identity_matches_distribution_name(self) -> None:
        self.assertEqual(PLUGIN_NAME, "astrbot_plugin_groupessence")
        self.assertEqual(PLUGIN_DATABASE_FILENAME, "group_essence.db")
        self.assertEqual(LEGACY_PLUGIN_NAMES, ("astrbot_plugin_group_essence",))

    def test_fresh_install_uses_canonical_path_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)

            result = resolve_plugin_data_dir(data_root)

            self.assertEqual(
                result,
                data_root / "plugin_data" / "astrbot_plugin_groupessence",
            )
            self.assertFalse((data_root / "plugin_data").exists())

    def test_existing_legacy_path_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            legacy = (
                data_root / "plugin_data" / "astrbot_plugin_group_essence"
            )
            legacy.mkdir(parents=True)

            self.assertEqual(resolve_plugin_data_dir(data_root), legacy)

    def test_canonical_path_wins_when_both_paths_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_root = Path(temp)
            plugin_data = data_root / "plugin_data"
            canonical = plugin_data / "astrbot_plugin_groupessence"
            legacy = plugin_data / "astrbot_plugin_group_essence"
            canonical.mkdir(parents=True)
            legacy.mkdir()

            self.assertEqual(resolve_plugin_data_dir(data_root), canonical)


class DistributionContractTests(unittest.TestCase):
    def test_required_astrbot_distribution_files_are_present(self) -> None:
        for relative_path in (
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
            "logo.png",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertTrue((PROJECT_ROOT / relative_path).is_file())

    def test_metadata_uses_stable_marketplace_identity(self) -> None:
        metadata = (PROJECT_ROOT / "metadata.yaml").read_text(encoding="utf-8")

        self.assertIn("name: astrbot_plugin_groupessence\n", metadata)
        self.assertIn("version: 0.5.2\n", metadata)
        self.assertIn("author: tntexploding\n", metadata)
        self.assertIn(
            "repo: https://github.com/tntexploding/astrbot_plugin_groupessence\n",
            metadata,
        )
        self.assertIn("astrbot_version: \">=4.0.0,<5\"\n", metadata)
        self.assertIn("support_platforms:\n  - aiocqhttp\n", metadata)
        self.assertNotIn("repo: https://github.com/tntexploding/GroupEssence", metadata)

    def test_configuration_schema_marks_identity_fields_prominently(self) -> None:
        schema = json.loads(
            (PROJECT_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )

        for field in ("admin_ids", "allowed_group_ids", "onebot_platform_id"):
            with self.subTest(field=field):
                self.assertTrue(schema[field]["obvious_hint"])

    def test_logo_is_recommended_square_rgba_size(self) -> None:
        logo = (PROJECT_ROOT / "logo.png").read_bytes()

        self.assertEqual(logo[:8], b"\x89PNG\r\n\x1a\n")
        width, height, bit_depth, color_type = struct.unpack(
            ">IIBB", logo[16:26]
        )
        self.assertEqual((width, height), (256, 256))
        self.assertEqual(bit_depth, 8)
        self.assertEqual(color_type, 6)


if __name__ == "__main__":
    unittest.main()
