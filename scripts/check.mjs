import { readdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
for (const folder of ['browser', 'providers', 'webhooks', 'history', 'matrix', 'mongo']) {
  if (!existsSync(path.join(root, folder))) continue;
  for (const name of await readdir(path.join(root, folder))) {
    const file = path.join(root, folder, name);
    const command = name.endsWith('.mjs') ? [process.execPath, ['--check', file]]
      : name.endsWith('.py') ? ['python3', ['-c', 'import ast,pathlib,sys; ast.parse(pathlib.Path(sys.argv[1]).read_text())', file]] : null;
    if (command && spawnSync(command[0], command[1], { stdio: 'inherit' }).status !== 0) process.exit(1);
  }
}
console.log('Shared QA source syntax is valid.');
