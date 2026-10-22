"""Settings edits must stay inside the selected TOML table."""

from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
import unittest

from tdlib_media_uploader.gui.config_service import update_toml_value, write_config_values


class ConfigEditingBoundariesTest(unittest.TestCase):
    def test_valid_header_comments_and_indentation_remain_editable(self):
        for header in ("[paths] # media directories", "  [paths]", "\t[paths]\t# media directories"):
            with self.subTest(header=header):
                original = f'{header}\nvideo_dir = "old"\n[proxy]\nenabled = true\n'
                updated = update_toml_value(original, "paths", "video_dir", "new")
                self.assertEqual(updated, original.replace('"old"', '"new"'))
                parsed = tomllib.loads(updated)
                self.assertEqual(parsed["paths"]["video_dir"], "new")
                self.assertTrue(parsed["proxy"]["enabled"])

    def test_missing_key_does_not_overwrite_an_indented_next_table(self):
        original = "[scan]\n# scan settings\n  [proxy]\nenabled = true\n"
        updated = update_toml_value(original, "scan", "enabled", False)
        parsed = tomllib.loads(updated)
        self.assertFalse(parsed["scan"]["enabled"])
        self.assertTrue(parsed["proxy"]["enabled"])
        self.assertIn("  [proxy]\nenabled = true\n", updated)

    def test_indented_array_table_also_ends_the_selected_table(self):
        original = "[scan]\n# scan settings\n\t[[jobs]]\nenabled = true\n"
        updated = update_toml_value(original, "scan", "enabled", False)
        parsed = tomllib.loads(updated)
        self.assertFalse(parsed["scan"]["enabled"])
        self.assertTrue(parsed["jobs"][0]["enabled"])
        self.assertIn("\t[[jobs]]\nenabled = true\n", updated)

    def test_replacing_crlf_value_preserves_header_comment_and_line_endings(self):
        original = "[scan] # tuning\r\nenabled = true\r\n  [proxy]\r\nenabled = true\r\n"
        updated = update_toml_value(original, "scan", "enabled", False)
        self.assertEqual(updated, original.replace("enabled = true", "enabled = false", 1))
        self.assertFalse(tomllib.loads(updated)["scan"]["enabled"])

    def test_inserting_key_or_table_uses_existing_crlf_line_endings(self):
        original = "[scan]\r\n# scan settings\r\n  [proxy]\r\nenabled = true\r\n"
        updated = update_toml_value(original, "scan", "readiness_attempts", 4)
        self.assertEqual(updated, original.replace("  [proxy]", "readiness_attempts = 4\r\n  [proxy]"))
        updated = update_toml_value(updated, "paths", "video_dir", "new")
        self.assertTrue(updated.endswith('[paths]\r\nvideo_dir = "new"\r\n'))
        self.assertNotIn("\n", updated.replace("\r\n", ""))
        self.assertEqual(tomllib.loads(updated)["scan"]["readiness_attempts"], 4)

    def test_empty_final_table_without_newline_accepts_a_new_key(self):
        updated = update_toml_value("[scan] # tuning", "scan", "readiness_attempts", 4)
        self.assertEqual(updated, "[scan] # tuning\nreadiness_attempts = 4\n")
        self.assertEqual(tomllib.loads(updated)["scan"]["readiness_attempts"], 4)

    def test_persistence_preserves_crlf_and_does_not_append_duplicate_table(self):
        original = b'[paths] # directories\r\nvideo_dir = "old"\r\n[proxy]\r\nenabled = true\r\n'
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(original)
            error = write_config_values(
                {("paths", "video_dir"): "new"}, config_path=path, reloader=lambda: ""
            )
            self.assertEqual(error, "")
            self.assertEqual(path.read_bytes(), original.replace(b'"old"', b'"new"'))

    def test_reload_error_restores_original_crlf_bytes(self):
        original = b'[paths] # directories\r\nvideo_dir = "old"\r\n'
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_bytes(original)
            errors = iter(("invalid value", ""))
            error = write_config_values(
                {("paths", "video_dir"): "new"}, config_path=path,
                reloader=lambda: next(errors),
            )
            self.assertEqual(error, "invalid value")
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
