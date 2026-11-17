import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createGateway, RelayTokenStore } from './server.mjs';
import { configFromEnvironment, EntraSession, signInErrorSummary } from './auth.mjs';

const SYNTHETIC_TOKEN = 'synthetic-test-token-not-a-real-credential';
const AUTH = { authorization: `Bearer ${SYNTHETIC_TOKEN}` };

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return `http://127.0.0.1:${server.address().port}`;
}

async function close(server) {
  const closed = new Promise((resolve) => server.close(resolve));
  server.closeAllConnections();
  await closed;
}

async function fixture(testContext, handler, options = {}) {
  const upstream = http.createServer(handler);
  const upstreamUrl = await listen(upstream);
  const gateway = createGateway({ upstreamBase: `${upstreamUrl}/api/v1/llmapi/`, ...options });
  const url = await listen(gateway);
  testContext.after(async () => {
    await close(gateway);
    await close(upstream);
  });
  return { url, upstream };
}

test('health does not imply upstream authentication; missing token never reaches upstream', async (testContext) => {
  let upstreamCalls = 0;
  const { url } = await fixture(testContext, (request, response) => {
    upstreamCalls += 1;
    response.end();
  });
  const health = await fetch(`${url}/healthz`);
  assert.equal(health.status, 200);
  assert.equal((await health.json()).upstreamAuthenticated, false);
  const response = await fetch(`${url}/v1/messages`, { method: 'POST', body: '{}' });
  assert.equal(response.status, 401);
  assert.equal(upstreamCalls, 0);
});

test('Messages JSON and tool definitions are forwarded unchanged with scoped headers', async (testContext) => {
  let captured;
  const { url } = await fixture(testContext, async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    captured = { path: request.url, headers: request.headers, body: Buffer.concat(chunks).toString() };
    response.writeHead(200, { 'content-type': 'application/json', 'set-cookie': 'must-not-forward=1' });
    response.end(JSON.stringify({ type: 'message', content: [{ type: 'text', text: 'OK' }] }));
  });
  const body = JSON.stringify({
    model: 'claude-opus-5.5', max_tokens: 32,
    messages: [{ role: 'user', content: 'Only say OK.' }],
    tools: [{ name: 'read_file', input_schema: { type: 'object', properties: {} } }],
  });
  const response = await fetch(`${url}/v1/messages`, {
    method: 'POST', body,
    headers: { ...AUTH, cookie: 'must-not-forward=1', 'x-unrelated-secret': 'must-not-forward' },
  });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).content[0].text, 'OK');
  assert.equal(captured.path, '/api/v1/llmapi/v1/messages');
  assert.equal(captured.body, body);
  assert.equal(captured.headers.authorization, AUTH.authorization);
  assert.equal(captured.headers['x-clienttype'], 'Copilot');
  assert.equal(captured.headers['x-clientscenario'], 'Code');
  assert.equal(captured.headers['anthropic-version'], '2023-06-01');
  assert.equal(captured.headers['x-llm-api-version'], '2026-10-21');
  assert.match(captured.headers['x-conversationid'], /^[0-9a-f-]{36}$/);
  assert.equal(captured.headers.cookie, undefined);
  assert.equal(captured.headers['x-unrelated-secret'], undefined);
  assert.equal(response.headers.get('set-cookie'), null);
});

test('x-api-key is accepted as an upstream bearer token and model-list path is mapped', async (testContext) => {
  let captured;
  const { url } = await fixture(testContext, (request, response) => {
    captured = { path: request.url, authorization: request.headers.authorization };
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ data: [{ id: 'claude-opus-5.5' }] }));
  });
  const response = await fetch(`${url}/v1/models`, { headers: { 'x-api-key': SYNTHETIC_TOKEN } });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).data[0].id, 'claude-opus-5.5');
  assert.deepEqual(captured, { path: '/api/v1/llmapi/models', authorization: AUTH.authorization });
});

test('SSE first chunk arrives before the upstream finishes', { timeout: 5000 }, async (testContext) => {
  let releaseEnd;
  let upstreamEnded = false;
  const canEnd = new Promise((resolve) => { releaseEnd = resolve; });
  testContext.after(() => releaseEnd());
  const { url } = await fixture(testContext, async (request, response) => {
    request.resume();
    response.writeHead(200, { 'content-type': 'text/event-stream' });
    response.write('event: message_start\ndata: {"type":"message_start"}\n\n');
    await canEnd;
    upstreamEnded = true;
    response.end('event: message_stop\ndata: {"type":"message_stop"}\n\n');
  });
  const response = await fetch(`${url}/v1/messages`, { method: 'POST', headers: AUTH, body: '{}' });
  assert.equal(response.headers.get('content-type'), 'text/event-stream');
  const reader = response.body.getReader();
  const first = await reader.read();
  assert.match(new TextDecoder().decode(first.value), /message_start/);
  assert.equal(upstreamEnded, false);
  releaseEnd();
  let rest = '';
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    rest += new TextDecoder().decode(chunk.value);
  }
  assert.match(rest, /message_stop/);
});

test('upstream authentication failures are preserved rather than replaced with fake success', async (testContext) => {
  const { url } = await fixture(testContext, (request, response) => {
    request.resume();
    response.writeHead(401, { 'content-type': 'application/json', 'www-authenticate': 'Bearer' });
    response.end('{"error":{"code":"AuthTokenMissing"}}');
  });
  const response = await fetch(`${url}/v1/messages`, { method: 'POST', headers: AUTH, body: '{}' });
  assert.equal(response.status, 401);
  assert.equal(response.headers.get('www-authenticate'), 'Bearer');
  assert.equal((await response.json()).error.code, 'AuthTokenMissing');
});

test('bad JSON, excessive bodies, ambiguous credentials, browser origins and unknown routes are rejected', async (testContext) => {
  let upstreamCalls = 0;
  const { url } = await fixture(testContext, (request, response) => {
    upstreamCalls += 1;
    response.end();
  }, { bodyLimit: 256 });
  const cases = [
    { path: '/v1/messages', method: 'POST', headers: AUTH, body: 'not-json', status: 400 },
    { path: '/v1/messages', method: 'POST', headers: AUTH, body: JSON.stringify({ text: 'x'.repeat(300) }), status: 413 },
    { path: '/v1/messages', method: 'POST', headers: { ...AUTH, 'x-api-key': SYNTHETIC_TOKEN }, body: '{}', status: 401 },
    { path: '/healthz', headers: { origin: 'https://untrusted.invalid' }, status: 403 },
    { path: '/v1/messages?access_token=must-not-leak', method: 'POST', headers: AUTH, body: '{}', status: 400 },
    { path: '/proxy/https://untrusted.invalid', headers: AUTH, status: 404 },
    { path: '/v1/messages', headers: AUTH, status: 405 },
  ];
  for (const item of cases) {
    const response = await fetch(`${url}${item.path}`, {
      method: item.method, headers: item.headers, body: item.body,
    });
    assert.equal(response.status, item.status);
    const text = await response.text();
    assert.equal(text.includes(SYNTHETIC_TOKEN), false);
    assert.equal(text.includes('must-not-leak'), false);
  }
  assert.equal(upstreamCalls, 0);
});

test('non-loopback Host headers are rejected', async (testContext) => {
  const { url } = await fixture(testContext, (request, response) => response.end());
  const result = await new Promise((resolve, reject) => {
    const request = http.get(`${url}/healthz`, { headers: { host: 'untrusted.invalid' } }, (response) => {
      response.resume();
      response.once('end', () => resolve(response.statusCode));
    });
    request.on('error', reject);
  });
  assert.equal(result, 403);
});

test('an unresponsive upstream produces a bounded timeout without leaking credentials', { timeout: 5000 }, async (testContext) => {
  const { url } = await fixture(testContext, (request) => request.resume(), { timeoutMs: 100 });
  const response = await fetch(`${url}/v1/messages`, { method: 'POST', headers: AUTH, body: '{}' });
  assert.equal(response.status, 504);
  const text = await response.text();
  assert.match(text, /timed out/);
  assert.equal(text.includes(SYNTHETIC_TOKEN), false);
});

test('Entra login uses an independently registered client and keeps tokens out of status', async () => {
  const config = configFromEnvironment({
    ENTRA_CLIENT_ID: '11111111-1111-4111-8111-111111111111',
    ENTRA_TENANT_ID: '22222222-2222-4222-8222-222222222222',
  });
  const account = { homeAccountId: 'synthetic-account' };
  let interactiveRequest;
  let silentRequest;
  const session = new EntraSession(config, {
    async acquireTokenInteractive(request) {
      interactiveRequest = request;
      return { account, accessToken: SYNTHETIC_TOKEN, expiresOn: new Date('2030-01-01T00:00:00Z') };
    },
    async acquireTokenSilent(request) {
      silentRequest = request;
      return { account, accessToken: `${SYNTHETIC_TOKEN}-refreshed`, expiresOn: new Date('2030-01-01T01:00:00Z') };
    },
  });
  await assert.rejects(session.getAccessToken(), /sign-in is required/);
  const openBrowser = async () => {};
  const status = await session.signIn(openBrowser);
  assert.equal(status.signedIn, true);
  assert.equal(JSON.stringify(status).includes(SYNTHETIC_TOKEN), false);
  assert.deepEqual(interactiveRequest.scopes, ['42c8f7bd-c7a8-4ba9-bc1c-91edfb716247/.default']);
  assert.equal(interactiveRequest.openBrowser, openBrowser);
  assert.equal(await session.getAccessToken(), `${SYNTHETIC_TOKEN}-refreshed`);
  assert.equal(silentRequest.account, account);
  assert.deepEqual(silentRequest.scopes, interactiveRequest.scopes);
  assert.equal(session.status().expiresOn, '2030-01-01T01:00:00.000Z');
  assert.equal(JSON.stringify(session.status()).includes('synthetic-account'), false);
});

test('invalid Entra configuration fails closed and sign-in errors do not reveal credentials', () => {
  assert.throws(() => configFromEnvironment({}), /own registered public-client/);
  const summary = signInErrorSummary({
    errorCode: 'consent_required',
    errorMessage: `AADSTS65001: token=${SYNTHETIC_TOKEN}; account=private@example.test`,
  });
  assert.equal(summary, 'consent_required (AADSTS65001)');
  assert.equal(summary.includes(SYNTHETIC_TOKEN), false);
  assert.equal(summary.includes('private@example.test'), false);
});

test('real MSAL initializes with an empty in-memory cache without claiming sign-in', () => {
  const session = new EntraSession({
    clientId: '11111111-1111-4111-8111-111111111111',
    tenantId: '22222222-2222-4222-8222-222222222222',
  });
  assert.equal(session.status().signedIn, false);
  assert.equal(session.status().expiresOn, null);
});

test('login command stops before browser sign-in when required local configuration is absent', () => {
  const result = spawnSync(process.execPath, ['server.mjs', '--login'], {
    cwd: new URL('.', import.meta.url),
    env: { ...process.env, LOCAL_GATEWAY_KEY: '', ENTRA_CLIENT_ID: '', ENTRA_TENANT_ID: '' },
    encoding: 'utf8',
    timeout: 5000,
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /LOCAL_GATEWAY_KEY/);
  assert.equal(result.stdout.includes('Opening Microsoft sign-in'), false);
  assert.equal(result.stdout.includes('Messages endpoint:'), false);
});

test('managed login validates a separate local key and injects only the provider token upstream', async (testContext) => {
  const localApiKey = 'synthetic-local-gateway-key-at-least-thirty-two-characters';
  let providerCalls = 0;
  let upstreamAuthorization;
  const { url } = await fixture(testContext, (request, response) => {
    upstreamAuthorization = request.headers.authorization;
    request.resume();
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('{"type":"message","content":[]}');
  }, {
    localApiKey,
    async tokenProvider() {
      providerCalls += 1;
      return SYNTHETIC_TOKEN;
    },
  });
  const denied = await fetch(`${url}/v1/messages`, { method: 'POST', headers: AUTH, body: '{}' });
  assert.equal(denied.status, 401);
  await denied.text();
  assert.equal(providerCalls, 0);
  const accepted = await fetch(`${url}/v1/messages`, {
    method: 'POST', headers: { 'x-api-key': localApiKey }, body: '{}',
  });
  assert.equal(accepted.status, 200);
  await accepted.text();
  assert.equal(providerCalls, 1);
  assert.equal(upstreamAuthorization, `Bearer ${SYNTHETIC_TOKEN}`);
  assert.equal(upstreamAuthorization.includes(localApiKey), false);
  const health = await fetch(`${url}/healthz`);
  assert.equal((await health.json()).authentication, 'entra-msal');
});

test('managed login fails closed on refresh errors without exposing raw auth failures', async (testContext) => {
  const localApiKey = 'synthetic-local-gateway-key-at-least-thirty-two-characters';
  let upstreamCalls = 0;
  const { url } = await fixture(testContext, (request, response) => {
    upstreamCalls += 1;
    response.end();
  }, {
    localApiKey,
    async tokenProvider() { throw new Error(`private upstream failure: ${SYNTHETIC_TOKEN}`); },
  });
  const response = await fetch(`${url}/v1/messages`, {
    method: 'POST', headers: { 'x-api-key': localApiKey }, body: '{}',
  });
  assert.equal(response.status, 401);
  const text = await response.text();
  assert.equal(text.includes(SYNTHETIC_TOKEN), false);
  assert.equal(text.includes(localApiKey), false);
  assert.equal(upstreamCalls, 0);
  assert.throws(() => createGateway({ tokenProvider: async () => SYNTHETIC_TOKEN }), /local API key/);
});

test('relay mode ingests token over internal loopback route, stores DPAPI-encrypted cache, and forwards requests', async (testContext) => {
  const localApiKey = 'synthetic-local-gateway-key-at-least-thirty-two-characters';
  const relayIngestSecret = 'synthetic-relay-ingest-secret-at-least-thirty-two-chars';
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'relay-test-'));
  const tokenFile = path.join(tempDir, 'token.dpapi');
  testContext.after(async () => {
    await fs.rm(tempDir, { recursive: true, force: true });
  });

  const relayStore = new RelayTokenStore({ tokenFile });
  let upstreamAuth = null;
  const { url } = await fixture(testContext, (request, response) => {
    upstreamAuth = request.headers.authorization;
    request.resume();
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('{"type":"message","content":[{"type":"text","text":"OK"}]}');
  }, {
    localApiKey,
    relayStore,
    relayIngestSecret,
    authMode: 'copilot-token-relay',
    tokenProvider: () => relayStore.getToken(),
  });

  const beforeIngest = await fetch(`${url}/v1/messages`, {
    method: 'POST',
    headers: { 'x-api-key': localApiKey },
    body: '{}',
  });
  assert.equal(beforeIngest.status, 401);

  const badSecret = await fetch(`${url}/_internal/token`, {
    method: 'POST',
    headers: { 'x-relay-secret': 'wrong-secret', 'content-type': 'application/json' },
    body: JSON.stringify({ token: SYNTHETIC_TOKEN }),
  });
  assert.equal(badSecret.status, 401);

  const futureExp = Math.floor(Date.now() / 1000) + 3600;
  const payloadB64 = Buffer.from(JSON.stringify({ exp: futureExp })).toString('base64url');
  const jwtToken = `e30.${payloadB64}.sig`;

  const ingest = await fetch(`${url}/_internal/token`, {
    method: 'POST',
    headers: { 'x-relay-secret': relayIngestSecret, 'content-type': 'application/json' },
    body: JSON.stringify({ token: jwtToken }),
  });
  assert.equal(ingest.status, 200);
  const ingestBody = await ingest.json();
  assert.equal(ingestBody.status, 'updated');
  assert.equal(JSON.stringify(ingestBody).includes(jwtToken), false);

  const rawCipher = await fs.readFile(tokenFile, 'utf8');
  assert.equal(rawCipher.includes(jwtToken), false);

  const reloaded = new RelayTokenStore({ tokenFile });
  assert.equal(await reloaded.loadCached(), true);
  assert.equal(reloaded.getToken(), jwtToken);

  const health = await fetch(`${url}/healthz`);
  const healthJson = await health.json();
  assert.equal(healthJson.authentication, 'copilot-token-relay');
  assert.equal(healthJson.upstreamAuthenticated, true);

  const okResp = await fetch(`${url}/v1/messages`, {
    method: 'POST',
    headers: { 'x-api-key': localApiKey },
    body: JSON.stringify({ model: 'claude-opus-5.5', messages: [{ role: 'user', content: 'hi' }] }),
  });
  assert.equal(okResp.status, 200);
  assert.equal(upstreamAuth, `Bearer ${jwtToken}`);
});

test('relay missing and expired token errors preserve actionable Copilot instructions', async (testContext) => {
  const localApiKey = 'synthetic-local-gateway-key-at-least-thirty-two-characters';
  const relayStore = new RelayTokenStore();
  let upstreamCalls = 0;
  const { url } = await fixture(testContext, (request, response) => {
    upstreamCalls += 1;
    response.end();
  }, { localApiKey, tokenProvider: () => relayStore.getToken() });
  for (const expired of [false, true]) {
    if (expired) {
      const payload = Buffer.from(JSON.stringify({ exp: 1 })).toString('base64url');
      await relayStore.setToken(`e30.${payload}.sig`);
    }
    const response = await fetch(`${url}/v1/messages`, {
      method: 'POST', headers: { 'x-api-key': localApiKey }, body: '{}',
    });
    assert.equal(response.status, 401);
    const result = await response.json();
    assert.match(result.error.message, expired ? /has expired/ : /No Copilot upstream token/);
    assert.match(result.error.message, /Copilot App/);
    assert.equal(result.error.message.includes('--login'), false);
    assert.equal(relayStore.status().hasToken, false);
  }
  assert.equal(upstreamCalls, 0);
});
