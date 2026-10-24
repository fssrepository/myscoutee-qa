// Consume the existing QA launcher's private state; never create a browser here.
import { constants } from 'node:fs';
import { lstat, open } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

export async function readManagedBrowserManifest(checkpoint) {
  if (!/^[A-Z0-9][A-Z0-9_-]{0,100}$/.test(checkpoint)) throw new Error('Invalid checkpoint ID');
  const directory = path.join(os.tmpdir(), 'myscoutee-qa-browser-sessions'); // NOSONAR - javascript:S5443: owner/mode checks below and O_NOFOLLOW protect the existing launcher state.
  const state = await lstat(directory);
  if (!state.isDirectory() || state.uid !== process.getuid() || (state.mode & 0o077) !== 0)
    throw new Error('Managed browser state must be a private, owned directory');
  const file = await open(path.join(directory, checkpoint + '.json'), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await file.stat();
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) !== 0 || info.size > 65536)
      throw new Error('Managed browser manifest must be a private, owned regular file of at most 64 KiB');
    const manifest = JSON.parse(await file.readFile('utf8'));
    if (manifest.checkpointId !== checkpoint || !Array.isArray(manifest.sessions) || manifest.sessions.length !== 1)
      throw new Error('Wrong managed browser checkpoint');
    const port = manifest.sessions[0]?.devToolsPort;
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid managed browser port');
    return manifest;
  } finally {
    await file.close();
  }
}
