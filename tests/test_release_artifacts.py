"""Exercise the workflow checksum commands against downloaded asset names."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/build-platforms.yml'


class ReleaseArtifactsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bash') and shutil.which('shasum'), 'requires workflow shell tools')
    def test_checksums_verify_after_assets_are_downloaded_to_another_directory(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        start = workflow.index('          shopt -s nullglob')
        end = workflow.index('          version=', start)
        script = 'set -Eeuo pipefail\n' + textwrap.dedent(workflow[start:end])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / 'release-assets'; assets.mkdir()
            names = ['TDLib.Media.Uploader-v1.9.4-windows-x64.zip',
                     'TDLib.Media.Uploader-v1.9.4-macos-arm64.dmg']
            for name in names:
                (assets / name).write_bytes(name.encode())
            subprocess.run(['bash', '-c', script], cwd=root, check=True, capture_output=True)
            sums = (assets / 'SHA256SUMS').read_text(encoding='utf-8')
            for name in names:
                self.assertIn(f'{hashlib.sha256(name.encode()).hexdigest()}  {name}', sums)
            self.assertNotIn('release-assets/', sums)
            downloaded = root / 'downloaded'
            assets.rename(downloaded)
            subprocess.run(['shasum', '-a', '256', '-c', 'SHA256SUMS'],
                           cwd=downloaded, check=True, capture_output=True)

    def test_existing_releases_and_tags_are_not_overwritten(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        release = workflow.split('  publish-release:', 1)[1]
        self.assertIn("if: startsWith(github.ref, 'refs/tags/v')", release)
        self.assertIn('gh release view "$tag"', release)
        self.assertIn('--verify-tag', release)
        self.assertNotIn('--clobber', release)
        self.assertNotIn('git push', release)
        self.assertNotIn('git tag', release)
        self.assertIn('cancel-in-progress: false', workflow)
