#!/usr/bin/env node

import { writeFile } from 'node:fs/promises';

const args = process.argv.slice(2);
const option = name => {
  const index = args.indexOf(name);
  return index >= 0 ? `${args[index + 1] ?? ''}`.trim() : '';
};
const port = Number(option('--port'));
const latitude = Number(option('--latitude'));
const longitude = Number(option('--longitude'));
const accuracy = Number(option('--accuracy') || 10);
const targetUrl = option('--url');
const readyFile = option('--ready-file');
const permission = option('--permission') || 'granted';
const noReload = args.includes('--no-reload');

if (!Number.isInteger(port) || port <= 0
  || !Number.isFinite(latitude) || !Number.isFinite(longitude)
  || !Number.isFinite(accuracy) || accuracy < 0
  || !targetUrl || !readyFile
  || !['granted', 'denied', 'prompt'].includes(permission)) {
  throw new Error(
    'Usage: hold-chrome-geolocation.mjs --port PORT --latitude LAT '
    + '--longitude LON [--accuracy METERS] --permission granted|denied|prompt '
    + '--url URL --ready-file FILE'
  );
}

try {
  const page = await findPageTarget();
  const browser = await jsonRequest(`http://127.0.0.1:${port}/json/version`);
  const browserConnection = await connectCdp(browser.webSocketDebuggerUrl);
  const pageConnection = await connectCdp(page.webSocketDebuggerUrl);
  const geolocation = { latitude, longitude, accuracy };
  const origin = new URL(targetUrl).origin;

  pageConnection.onEvent(message => {
    if (message.method === 'Page.frameNavigated' && !message.params?.frame?.parentId) {
      void pageConnection.send('Emulation.setGeolocationOverride', geolocation).catch(() => undefined);
    }
  });

  if (permission === 'prompt') {
    // A synthetic `setPermission: prompt` can report the requested state while
    // still short-circuiting the browser's native permission UI. Reset the
    // isolated QA profile instead so the next user-initiated Geolocation API
    // call exercises Chrome's real prompt and persists the user's choice.
    await browserConnection.send('Browser.resetPermissions');
  } else {
    await browserConnection.send('Browser.setPermission', {
      origin,
      permission: { name: 'geolocation' },
      setting: permission
    });
  }
  await pageConnection.send('Page.enable');
  await pageConnection.send('Emulation.setGeolocationOverride', geolocation);
  if (!noReload) await pageConnection.send('Page.navigate', { url: targetUrl });
  await writeFile(readyFile, `${JSON.stringify({
    status: 'ready',
    pid: process.pid,
    port,
    targetUrl,
    permission,
    permissionMode: permission === 'prompt' ? 'browser-reset' : 'cdp-override',
    geolocation,
    readyAtIso: new Date().toISOString()
  }, null, 2)}\n`);

  // Node's built-in WebSocket alone is not guaranteed to retain an active
  // event-loop reference. Keep this deliberately tiny holder alive until the
  // launcher closes the managed Chrome or sends SIGTERM.
  const lifecycleGuard = setInterval(() => undefined, 60000);
  process.on('SIGTERM', close);
  process.on('SIGINT', close);

  function close() {
    clearInterval(lifecycleGuard);
    pageConnection.close();
    browserConnection.close();
  }
} catch (error) {
  await writeFile(readyFile, `${JSON.stringify({
    status: 'error',
    pid: process.pid,
    message: error instanceof Error ? error.message : `${error}`
  }, null, 2)}\n`).catch(() => undefined);
  process.exit(1);
}

async function connectCdp(webSocketUrl) {
  if (!webSocketUrl) {
    throw new Error('Chrome did not expose a CDP WebSocket URL.');
  }
  const socket = new WebSocket(webSocketUrl);
  let requestId = 0;
  const pending = new Map();
  const eventListeners = new Set();

  socket.addEventListener('message', event => {
    const message = JSON.parse(`${event.data}`);
    const request = pending.get(message.id);
    if (request) {
      pending.delete(message.id);
      if (message.error) {
        request.reject(new Error(message.error.message || 'Chrome CDP command failed.'));
      } else {
        request.resolve(message);
      }
      return;
    }
    for (const listener of eventListeners) {
      listener(message);
    }
  });
  socket.addEventListener('close', () => process.exit(0));
  socket.addEventListener('error', () => process.exit(1));
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('Unable to connect to Chrome DevTools.')), { once: true });
  });

  return {
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++requestId;
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    onEvent(listener) {
      eventListeners.add(listener);
    },
    close() {
      socket.close();
    }
  };
}

async function findPageTarget() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`);
    if (response.ok) {
      const targets = await response.json();
      const page = targets.find(target => target.type === 'page' && target.webSocketDebuggerUrl);
      if (page) {
        return page;
      }
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`No Chrome page target found on DevTools port ${port}.`);
}

async function jsonRequest(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${url} returned HTTP ${response.status}`);
  }
  return await response.json();
}
