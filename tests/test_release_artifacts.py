"""Exercise the workflow checksum commands against downloaded asset names."""
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/build-platforms.yml'


class ReleaseArtifactsTest(unittest.TestCase):
    @unittest.skipUnless(os.name != 'nt' and shutil.which('bash'),
                         'executes the macOS validation shell script')
    def test_dmg_mount_recovers_from_transient_failure_and_still_fails_if_unavailable(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        start = workflow.index('          MOUNT_POINT="$(mktemp -d)"')
        end = workflow.index('\n      - name: Compute macOS artifact label', start)
        script = '''
set -Eeuo pipefail
mount_attempts=0
DMG=package.dmg
hdiutil() {
  if [[ "$1" == attach ]]; then
    mount_attempts=$((mount_attempts + 1))
    printf '%s' "$mount_attempts" > "$ATTEMPT_LOG"
    if (( mount_attempts <= FAILURES_BEFORE_SUCCESS )); then
      printf 'hdiutil: attach failed - Resource temporarily unavailable\\n' >&2
      return 1
    fi
    mkdir -p "${!#}/TDLib Media Uploader.app"
    ln -s /Applications "${!#}/Applications"
  fi
}
sleep() { :; }
''' + textwrap.dedent(workflow[start:end])
        for failures, expected_attempts, succeeds in ((0, 1, True), (1, 2, True), (10, 3, False)):
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                attempts = root / 'attempts'
                result = subprocess.run(
                    ['bash', '-c', script], cwd=root, capture_output=True, text=True,
                    env=dict(os.environ, TMPDIR=str(root), ATTEMPT_LOG=str(attempts),
                             FAILURES_BEFORE_SUCCESS=str(failures)),
                )
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                self.assertEqual(int(attempts.read_text()), expected_attempts)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('bash') and shutil.which('shasum'),
                         'executes the Unix-only publish job; Windows bash may be a WSL launcher')
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
            subprocess.run(['bash', '-c', script], cwd=root, check=True)
            sums = (assets / 'SHA256SUMS').read_text(encoding='utf-8')
            for name in names:
                self.assertIn(f'{hashlib.sha256(name.encode()).hexdigest()}  {name}', sums)
            self.assertNotIn('release-assets/', sums)
            downloaded = root / 'downloaded'
            assets.rename(downloaded)
            subprocess.run(['shasum', '-a', '256', '-c', 'SHA256SUMS'],
                           cwd=downloaded, check=True)

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
