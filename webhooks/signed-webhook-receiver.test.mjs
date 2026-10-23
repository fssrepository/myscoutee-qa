import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { startEventsReceiver } from './signed-webhook-receiver.mjs';

test('Events receiver verifies exact bytes, echoes challenges and keeps evidence secret-free', async () => {
  const receiver = await startEventsReceiver({ port: 0 });
  try {
    const callback = receiver.register('smoke');
    const body = JSON.stringify({ type: 'verification', challenge: 'qa-random-challenge' });
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = createHmac('sha256', Buffer.from(callback.secrets[0].slice(6), 'base64'))
      .update(`qa-message.${timestamp}.${body}`).digest('base64');
    const headers = { 'webhook-id': 'qa-message', 'webhook-timestamp': timestamp,
      'webhook-signature': `v1,${signature}`, 'x-mcp-subscription-id': 'qa-subscription' };
    const url = `http://127.0.0.1:${receiver.port}${callback.path}`;
    const response = await fetch(url, { method: 'POST', headers, body });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { challenge: 'qa-random-challenge' });
    const tampered = await fetch(url, { method: 'POST', headers, body: body + ' ' });
    assert.equal(tampered.status, 401);
    await tampered.arrayBuffer();
    assert.equal(receiver.receipts.length, 1);
    const evidence = JSON.stringify(receiver.evidence());
    for (const privateValue of [callback.secrets[0], callback.path, 'qa-random-challenge', signature]) {
      assert.ok(!evidence.includes(privateValue));
    }
    callback.setBehavior('mismatch');
    const mismatch = await fetch(url, { method: 'POST', headers, body });
    assert.deepEqual(await mismatch.json(), { challenge: 'wrong' });
    callback.setBehavior(410);
    const gone = await fetch(url, { method: 'POST', headers, body });
    assert.equal(gone.status, 410);
    await gone.arrayBuffer();
    callback.setBehavior('hold');
    const beforeHold = receiver.receipts.length;
    let completed = false;
    const held = fetch(url, { method: 'POST', headers, body }).then(response => {
      completed = true; return response;
    });
    for (let tries = 0; receiver.receipts.length === beforeHold && tries < 100; tries++) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    assert.equal(receiver.receipts.length, beforeHold + 1);
    assert.equal(completed, false);
    callback.release();
    assert.deepEqual(await (await held).json(), { challenge: 'qa-random-challenge' });
    callback.setBehavior({status: 200, body: 'x'.repeat(4097)});
    assert.equal((await (await fetch(url, {method: 'POST', headers, body})).text()).length, 4097);
    callback.setBehavior({status: 200, body: 'x', chunks: 3, intervalMs: 10});
    assert.equal(await (await fetch(url, {method: 'POST', headers, body})).text(), 'xxx');
    callback.setBehavior({status: 302, headers: {Location: '/never-follow'}});
    const redirect = await fetch(url, {method: 'POST', headers, body, redirect: 'manual'});
    assert.equal(redirect.status, 302);
    assert.equal(redirect.headers.get('location'), '/never-follow');
    await redirect.arrayBuffer();
    assert.throws(() => callback.setBehavior({body: 'x'.repeat(16385)}));
    assert.throws(() => callback.setBehavior({chunks: 31}));
    assert.throws(() => receiver.register('duplicate', callback.secrets, callback.path));
    assert.throws(() => receiver.register('unsafe path', callback.secrets, '/callback/../'));
  } finally { await receiver.close(); }
});
