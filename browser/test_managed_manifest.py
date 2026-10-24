"""Verify private launcher state without opening or attaching to a browser."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class BrowserManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'myscoutee-qa-browser-sessions'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / 'TEST-CHECKPOINT.json'
        self.path.write_text(json.dumps({'checkpointId': 'TEST-CHECKPOINT',
                                        'sessions': [{'devToolsPort': 12345}]}))
        self.path.chmod(0o600)

    def read(self):
        module = Path(__file__).with_name('managed-browser-manifest.mjs').as_uri()
        script = f'import {{readManagedBrowserManifest}} from {json.dumps(module)}; await readManagedBrowserManifest("TEST-CHECKPOINT");'
        return subprocess.run(['node', '--input-type=module', '--eval', script], capture_output=True,
                              text=True, env={**os.environ, 'TMPDIR': self.temporary.name}, timeout=10)

    def test_private_launcher_manifest_is_accepted(self):
        result = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_directory_or_manifest_is_rejected(self):
        for target in (self.directory, self.path):
            with self.subTest(target=target):
                previous = target.stat().st_mode & 0o777
                target.chmod(previous | 0o044)
                self.assertNotEqual(self.read().returncode, 0)
                target.chmod(previous)

    def test_symlink_manifest_is_rejected(self):
        retained = self.directory / 'retained.json'
        self.path.rename(retained)
        self.path.symlink_to(retained)
        self.assertNotEqual(self.read().returncode, 0)

    def test_wrong_checkpoint_and_invalid_port_are_rejected(self):
        for checkpoint, port in [('OTHER', 12345), ('TEST-CHECKPOINT', 80),
                                  ('TEST-CHECKPOINT', '12345'), ('TEST-CHECKPOINT', 65536)]:
            with self.subTest(checkpoint=checkpoint, port=port):
                self.path.write_text(json.dumps({'checkpointId': checkpoint, 'sessions': [{'devToolsPort': port}]}))
                self.assertNotEqual(self.read().returncode, 0)

    def test_oversized_file_and_nonregular_file_are_rejected(self):
        self.path.write_text(' ' * 65537)
        self.assertNotEqual(self.read().returncode, 0)
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        self.assertNotEqual(self.read().returncode, 0)


if __name__ == '__main__':
    unittest.main()
