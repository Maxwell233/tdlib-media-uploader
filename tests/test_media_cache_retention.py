from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from tdlib_media_uploader.config import paths
from tdlib_media_uploader.core.filesystem import stable_path
from tdlib_media_uploader.core.models import UploadRunResult
from tdlib_media_uploader.core.upload_journal import InflightJournal
from tdlib_media_uploader.core.uploader_database import (
    database_for,
    load_media_metadata,
    prune_media_metadata,
    store_media_metadata,
)
from tdlib_media_uploader.gui.integration import run_v2_upload
from tdlib_media_uploader.media import artifact_cache, video_probe
from tdlib_media_uploader.media.image_probe import probe_image
from tdlib_media_uploader.media.runtime import media_scope
from tdlib_media_uploader.upload.staging import stage_file


class MediaCacheRetentionTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.staging_dir = self.root / "custom-staging-root"
        self.config = SimpleNamespace(STAGING_DIR=self.staging_dir, MEDIA_METADATA_CACHE_MAX_ENTRIES=3)
        self.enterContext(patch.multiple(paths, DATA_DIR=self.root, CACHE_DIR=self.cache))

    def test_staged_copy_never_enters_either_probe_cache(self):
        original = self.root / "original.jpg"
        Image.new("RGB", (4, 5)).save(original)
        staged = stage_file(original, staging_dir=self.staging_dir, namespace="album")
        ffprobe = self.root / "ffprobe"
        ffprobe.touch()
        video = video_probe.VideoMediaInfo("mp4", "h264", None, 4, 5, 1.0, 25, True, "native")

        with media_scope(self.config):
            self.assertEqual(probe_image(original).width, 4)
            self.assertEqual(probe_image(staged).width, 4)
            with patch.object(video_probe, "_find_ffprobe", return_value=ffprobe):
                with patch.object(video_probe, "_probe_with_ffprobe", return_value=video):
                    video_probe.probe_video(original)
                    video_probe.probe_video(staged)

        with database_for(self.root / "uploader.db").read() as connection:
            rows = connection.execute("SELECT stable_path, probe FROM media_metadata_cache").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({name for name, _probe in rows}, {stable_path(original)})

    def test_caps_metadata_rows_and_cleans_preexisting_staging_entries(self):
        records = [(self.root / f"file-{index}.jpg", 1, 2, {"index": index}) for index in range(5)]
        store_media_metadata("probe", records, config=self.config)
        found = load_media_metadata("probe", [record[:3] for record in records], config=self.config)
        self.assertEqual({record["index"] for record in found.values()}, {2, 3, 4})

        database = database_for(self.root / "uploader.db")
        legacy_staging = self.root / "old-custom" / ".tdlib-media-uploader-staging" / "old.jpg"
        with database.write() as connection:
            for path in (self.cache / "staging" / "old.jpg", self.staging_dir / "old.jpg", legacy_staging):
                connection.execute(
                    "INSERT INTO media_metadata_cache VALUES (?, 1, 2, 'probe', '{}')",
                    (stable_path(path),),
                )
        prune_media_metadata(self.config)
        with database.read() as connection:
            remaining = {row[0] for row in connection.execute("SELECT stable_path FROM media_metadata_cache")}
        self.assertEqual(remaining, {stable_path(record[0]) for record in records[2:]})

    def test_completed_job_trims_artifacts_and_unknown_job_keeps_them(self):
        thumbnails = self.cache / "thumbnails"
        compressed = self.cache / "compressed"
        thumbnails.mkdir(parents=True)
        compressed.mkdir()
        thumbnail = thumbnails / ("a" * 40 + ".jpg")
        temp_file = thumbnails / ("b" * 40 + ".tmp.jpg")
        compressed_image = compressed / ("c" * 64 + ".jpg")
        for path in (thumbnail, temp_file, compressed_image):
            path.write_bytes(b"artifact")
        config = SimpleNamespace(THUMBNAIL_CACHE_MAX_BYTES=0, IMAGE_COMPRESSION_CACHE_MAX_BYTES=0)
        runtime_paths = SimpleNamespace(UPLOAD_INFLIGHT_DIR=self.root / "inflight")
        journal = InflightJournal(runtime_paths.UPLOAD_INFLIGHT_DIR)

        with patch.multiple(artifact_cache, THUMBNAIL_CACHE_DIR=thumbnails, IMAGE_COMPRESSION_CACHE_DIR=compressed):
            with patch("tdlib_media_uploader.gui.integration._run_v2_upload") as run:
                journal.prepare("image", "prior-album")
                for status, artifacts_left in (("UNKNOWN", True), ("CANCELLED", True), ("COMPLETED", True),
                                               ("COMPLETED", False)):
                    if not artifacts_left:
                        journal.finalize("image", "prior-album")
                    run.return_value = UploadRunResult(status)
                    result = run_v2_upload("image", source_root=Path("/source"), config=config,
                                           runtime_paths=runtime_paths)
                    self.assertEqual(result.status, status)
                    self.assertEqual(thumbnail.exists(), artifacts_left)
                    self.assertEqual(compressed_image.exists(), artifacts_left)
                    self.assertTrue(temp_file.exists())

    def test_unsupported_atime_update_leaves_artifact_usable(self):
        path = self.root / "artifact.jpg"
        path.touch()
        with patch.object(artifact_cache.os, "utime", side_effect=NotImplementedError):
            artifact_cache.record_cache_access(path, path.stat())
        self.assertTrue(path.is_file())
