#!/usr/bin/env python3
"""Index historical QA messages without exporting transcripts or assigning PASS.

Uses the retained-session enumeration and line locators of the existing production
audit scanner. The output is private discovery material, requiring human review.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history-root', type=Path, required=True)
    parser.add_argument('--project-name', action='append', required=True)
    parser.add_argument('--before', required=True, help='Exclusive ISO UTC cutoff')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cutoff = datetime.fromisoformat(args.before.replace('Z', '+00:00'))
    if cutoff.tzinfo is None:
        parser.error('--before must include a timezone')
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    pattern = re.compile(r'\bQA\b|\be2e\b|checklist|ellen[oő]rz|teszt|\bPASS\b|\bFAIL\b|bizony[ií]t', re.I)
    case_pattern = re.compile(r'\b(?:[A-Z][A-Z0-9]+-)+(?:\d{3}|[A-Z]+\b)|\bMSC[- ]?\d{3}\b')
    files = sorted(set(args.history_root.glob('sessions/**/*.jsonl'))
                   | set(args.history_root.glob('archived_sessions/**/*.jsonl')))
    inventory = []
    candidates = []
    for number, source in enumerate(files, 1):
        row = {'file': str(source.relative_to(args.history_root)), 'bytes': source.stat().st_size,
               'malformed_records': 0, 'candidates': 0}
        seen = set()
        relevant = False
        with source.open('rb') as stream:
            for line_number, line in enumerate(stream, 1):
                # Skip image/tool payloads before JSON decoding, as in the account
                # reconstruction extractor. Only messages and metadata are needed.
                head = line[:220].replace(b' ', b'')
                if not any(token in head for token in (b'"type":"session_meta"',
                           b'"type":"event_msg"', b'"type":"response_item"')):
                    continue
                if b'"type":"response_item"' in head and b'"type":"message"' not in line[:450].replace(b' ', b''):
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    row['malformed_records'] += 1
                    continue
                payload = event.get('payload', {})
                kind = event.get('type')
                if kind == 'session_meta':
                    cwd = payload.get('cwd', '')
                    source_kind = payload.get('source')
                    guardian = isinstance(source_kind, dict) and source_kind.get('subagent', {}).get('other') == 'guardian'
                    relevant = any(name in cwd for name in args.project_name) and not guardian
                    row.update(session_id=payload.get('id') or payload.get('session_id'),
                               cwd=cwd, source=payload.get('source'))
                    continue
                if not relevant:
                    continue
                timestamp = event.get('timestamp', '')
                if not timestamp or datetime.fromisoformat(timestamp.replace('Z', '+00:00')) >= cutoff:
                    continue
                text = ''
                role = None
                if kind == 'event_msg':
                    if payload.get('type') == 'user_message':
                        role, text = 'user', payload.get('message', '')
                    elif payload.get('type') == 'agent_message' and payload.get('phase') in ('final', 'final_answer'):
                        role, text = 'assistant-final', payload.get('message', '')
                elif payload.get('type') == 'message':
                    if payload.get('role') == 'user':
                        role = 'user'
                    elif payload.get('role') == 'assistant' and (payload.get('phase') in ('final', 'final_answer') or payload.get('channel') == 'final'):
                        role = 'assistant-final'
                    if role:
                        text = '\n'.join(block.get('text', '') for block in payload.get('content', [])
                                         if block.get('type') in ('input_text', 'output_text', 'text'))
                if not isinstance(text, str) or not text or not role:
                    continue
                if text.startswith(('# AGENTS.md', '<environment_context>', '<external_codex_apps_open_page>')):
                    continue
                digest = hashlib.sha256(text.encode()).hexdigest()
                identity = (role, digest)
                if identity in seen or not pattern.search(text):
                    continue
                seen.add(identity)
                candidates.append({'file': row['file'], 'session_id': row.get('session_id'),
                                   'line': line_number, 'timestamp': timestamp, 'kind': role,
                                   'message_sha256': digest, 'case_ids': sorted(set(case_pattern.findall(text))),
                                   'text': text})
                row['candidates'] += 1
        inventory.append(row)
        if number % 50 == 0:
            print(json.dumps({'files_scanned': number, 'total': len(files)}), flush=True)
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(), 'before': args.before,
               'files': len(files), 'bytes': sum(row['bytes'] for row in inventory),
               'candidate_messages': len(candidates),
               'message_kinds': dict(Counter(row['kind'] for row in candidates)),
               'limits': 'Discovery only. A message is not a verified execution; no PASS is inferred.'}
    for name, value in [('inventory.json', inventory), ('candidates.json', candidates), ('summary.json', summary)]:
        target = args.output / name
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        target.chmod(0o600)
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
