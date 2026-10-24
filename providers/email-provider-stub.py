#!/usr/bin/env python3
"""SendGrid-compatible QA receiver. Does not forward mail or retain Authorization headers."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from threading import Lock

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=18991)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--status', type=int, default=202)
parser.add_argument('--drop-response', action='store_true', help='Record the request, then close without returning a provider receipt.')
parser.add_argument('--retry-scenario', action='store_true', help='The selected recipient receives 503/429/503/202; other recipients keep receiving 503.')
parser.add_argument('--retry-recipient', help='Recipient selected for the retry-success sequence.')
args = parser.parse_args()
if args.retry_scenario and not args.retry_recipient:
    parser.error('--retry-scenario requires --retry-recipient')
attempts = {}
attempt_lock = Lock()
args.output.parent.mkdir(parents=True, exist_ok=True)
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/v3/mail/send':
            self.send_error(404); return
        length = int(self.headers.get('Content-Length', '0'))
        if length < 1 or length > 1048576:
            self.send_error(413); return
        try:
            body = json.loads(self.rfile.read(length))
            assert body['from']['email'] and body['personalizations'][0]['to'][0]['email'] and body['content']
        except (ValueError, KeyError, IndexError, AssertionError):
            self.send_error(400); return
        message_id = 'qa-' + str(uuid4())
        recipient = body['personalizations'][0]['to'][0]['email']
        with attempt_lock:
            attempt = attempts.get(recipient, 0) + 1
            attempts[recipient] = attempt
        status = args.status
        if args.retry_scenario:
            sequence = [503, 429, 503, 202] if recipient == args.retry_recipient else [503]
            status = sequence[min(attempt - 1, len(sequence) - 1)]
        # No key, cookie or authorization header is written to evidence.
        with args.output.open('a', encoding='utf-8') as output:
            output.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'messageId': message_id, 'status': status, 'attempt': attempt, 'payload': body}) + '\n')
        args.output.chmod(0o600)
        if args.drop_response:
            self.close_connection = True
            return
        self.send_response(status); self.send_header('X-Message-Id', message_id); self.send_header('Content-Length', '0'); self.end_headers()
    def log_message(self, *_):
        pass
server = ThreadingHTTPServer((args.host, args.port), Handler)
print(json.dumps({'ready': True, 'host': args.host, 'port': server.server_address[1]}), flush=True)
server.serve_forever()
