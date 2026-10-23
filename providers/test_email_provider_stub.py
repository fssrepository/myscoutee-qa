"""Real local HTTP checks for the non-delivering QA stub; no dev stack required."""
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request


class EmailProviderStubTest(unittest.TestCase):
    def test_acceptance_and_failure_protocol(self):
        for status in (202, 401):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'mail.jsonl'
                process = subprocess.Popen([
                    sys.executable, str(Path(__file__).with_name('email-provider-stub.py')),
                    '--host', '127.0.0.1', '--port', '0', '--output', str(output), '--status', str(status)
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'No readiness within 5s')
                    ready = json.loads(process.stdout.readline())
                    self.assertTrue(ready['ready'])
                    port = ready['port']

                    def request(path, payload):
                        req = urllib.request.Request(
                            f'http://127.0.0.1:{port}{path}', json.dumps(payload).encode(),
                            {'Content-Type': 'application/json', 'Authorization': 'Bearer qa-not-a-real-key'})
                        try:
                            with urllib.request.urlopen(req, timeout=3) as response:
                                return response.status, response.headers
                        except urllib.error.HTTPError as error:
                            return error.code, error.headers

                    payload = {'from': {'email': 'sender@example.invalid'},
                               'personalizations': [{'to': [{'email': 'qa@example.invalid'}]}],
                               'content': [{'type': 'text/plain', 'value': 'QA stub check'}]}
                    code, headers = request('/v3/mail/send', payload)
                    self.assertEqual(code, status)
                    self.assertTrue(headers['X-Message-Id'].startswith('qa-'))
                    self.assertEqual(request('/v3/mail/send', {})[0], 400)
                    self.assertEqual(request('/wrong', payload)[0], 404)
                    text = output.read_text()
                    self.assertNotIn('Authorization', text)
                    self.assertNotIn('qa-not-a-real-key', text)
                    self.assertEqual(len(text.splitlines()), 1)
                    self.assertEqual(json.loads(text)['payload'], payload)
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                finally:
                    process.terminate()
                    process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
