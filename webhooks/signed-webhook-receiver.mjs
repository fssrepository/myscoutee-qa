import http from 'node:http';
import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

// Events uses signed verification and fault responses, which the legacy /watch
// receiver below does not provide. Controls and raw payloads stay in memory.
export async function startEventsReceiver({ port = 18090 } = {}) {
  const routes = new Map(), receipts = [], sockets = new Set();
  const server = http.createServer(async (request, response) => {
    const route = routes.get(request.url);
    if (request.method !== 'POST' || !route) return response.writeHead(404).end();
    try {
      const chunks = []; let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 262144) { response.writeHead(413).end(); request.destroy(); return; }
        chunks.push(chunk);
      }
      const raw = Buffer.concat(chunks);
      const id = String(request.headers['webhook-id'] ?? '');
      const timestamp = String(request.headers['webhook-timestamp'] ?? '');
      const signatures = String(request.headers['webhook-signature'] ?? '').split(/\s+/);
      const verifiedKeys = route.secrets.map(secret => {
        const expected = createHmac('sha256', Buffer.from(secret.slice(6), 'base64'))
          .update(id + '.' + timestamp + '.').update(raw).digest();
        return signatures.some(signature => {
          if (!signature.startsWith('v1,')) return false;
          const actual = Buffer.from(signature.slice(3), 'base64');
          return actual.length === expected.length && timingSafeEqual(actual, expected);
        });
      });
      if (!id || !/^\d+$/.test(timestamp) || Math.abs(Date.now() / 1000 - Number(timestamp)) > 300
        || !verifiedKeys.some(Boolean)) return response.writeHead(401).end();
      const body = JSON.parse(raw.toString('utf8'));
      const receipt = { at: Date.now(), label: route.label, id, timestamp,
        subscriptionId: request.headers['x-mcp-subscription-id'] ?? null,
        type: body.type ?? body.name ?? null, bodyHash: createHash('sha256').update(raw).digest('hex'),
        bytes: raw.length, verifiedKeys, body };
      receipts.push(receipt);
      const behavior = route.behavior;
      if (behavior === 'hold') await new Promise(resolve => route.pending.add(resolve));
      if (behavior === 'stall') return; // server timeout bounds retained sockets.
      if (behavior === 'drop') { request.socket.destroy(); return; }
      if (typeof behavior === 'number') return response.writeHead(behavior).end();
      if (behavior && typeof behavior === 'object') {
        const {status = 200, headers = {}, body = '', chunks = 1, intervalMs = 0} = behavior;
        response.writeHead(status, headers);
        for (let i = 0; i < chunks && !response.destroyed; i++) {
          response.write(body);
          if (intervalMs) await new Promise(resolve => setTimeout(resolve, intervalMs));
        }
        response.end();
        return;
      }
      if (body.type === 'verification') {
        response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        response.end(JSON.stringify({ challenge: behavior === 'mismatch' ? 'wrong' : body.challenge }));
      } else response.writeHead(204).end();
    } catch { if (!response.headersSent) response.writeHead(400); response.end(); }
  });
  server.setTimeout(20000, socket => socket.destroy());
  server.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  return {
    port: server.address().port,
    register(label, secrets = ['whsec_' + randomBytes(32).toString('base64')], retainedPath) {
      const path = retainedPath ?? '/callback/' + randomBytes(24).toString('hex');
      if (!/^\/callback\/[a-f0-9]{48}$/.test(path) || routes.has(path)) throw Error('Invalid or duplicate callback path');
      const route = { label, secrets: [...secrets], behavior: 'accept', pending: new Set() };
      routes.set(path, route);
      return { path, secrets: route.secrets, setBehavior(value) {
        if (value && typeof value === 'object' &&
          (Buffer.byteLength(value.body ?? '') * (value.chunks ?? 1) > 16384
            || !Number.isInteger(value.chunks ?? 1) || (value.chunks ?? 1) < 1 || (value.chunks ?? 1) > 30
            || (value.intervalMs ?? 0) < 0 || (value.intervalMs ?? 0) > 1000))
          throw Error('Response fault exceeds fixture bounds');
        route.behavior = value;
      },
        release() { for (const resolve of route.pending) resolve(); route.pending.clear(); },
        setSecrets(values) { route.secrets = [...values]; } };
    },
    receipts,
    evidence() { return receipts.map(({ body, ...safe }) => safe); },
    async close() {
      for (const route of routes.values()) for (const resolve of route.pending) resolve();
      for (const socket of sockets) socket.destroy();
      await new Promise(resolve => server.close(resolve));
      routes.clear(); receipts.length = 0;
    }
  };
}

