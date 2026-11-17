import http from 'node:http';
import https from 'node:https';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import path from 'node:path';
import { pipeline } from 'node:stream';
import { pathToFileURL } from 'node:url';
import { configFromEnvironment, EntraSession, signInErrorSummary } from './auth.mjs';
import { loadProtectedText, saveProtectedText } from './dpapi.mjs';

const DEFAULT_UPSTREAM = 'https://loki.delve.office.com/api/v1/llmapi/';
const ROUTES = new Map([
  ['/v1/messages', { method: 'POST', upstreamPath: 'v1/messages' }],
  ['/v1/messages/count_tokens', { method: 'POST', upstreamPath: 'v1/messages/count_tokens' }],
  ['/v1/models', { method: 'GET', upstreamPath: 'models' }],
]);
const RESPONSE_HEADERS = [
  'content-type', 'content-encoding', 'retry-after', 'request-id', 'x-request-id',
  'www-authenticate', 'anthropic-ratelimit-requests-limit',
  'anthropic-ratelimit-requests-remaining', 'anthropic-ratelimit-requests-reset',
  'anthropic-ratelimit-tokens-limit', 'anthropic-ratelimit-tokens-remaining',
  'anthropic-ratelimit-tokens-reset',
];
const TOKEN_PATTERN = /^[^\s,\x00-\x1f\x7f]{1,32768}$/;

function decodeJwtExp(token) {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    if (typeof payload?.exp === 'number' && Number.isFinite(payload.exp)) {
      return new Date(payload.exp * 1000).toISOString();
    }
  } catch {
    return null;
  }
  return null;
}

export class RelayTokenStore {
  #token = null;
  #expiresOn = null;
  #tokenFile;

  constructor({ tokenFile = null } = {}) {
    this.#tokenFile = tokenFile;
  }

  async loadCached() {
    if (!this.#tokenFile) return false;
    try {
      const token = (await loadProtectedText(this.#tokenFile)).trim();
      if (!TOKEN_PATTERN.test(token)) return false;
      this.#token = token;
      this.#expiresOn = decodeJwtExp(token);
      return true;
    } catch {
      return false;
    }
  }

  async setToken(token) {
    if (typeof token !== 'string' || !TOKEN_PATTERN.test(token)) {
      throw new RequestError(400, 'invalid_request_error', 'Invalid token format.');
    }
    this.#token = token;
    this.#expiresOn = decodeJwtExp(token);
    if (this.#tokenFile) {
      await saveProtectedText(this.#tokenFile, token);
    }
    return { expiresOn: this.#expiresOn };
  }

  getToken() {
    if (!this.#token) {
      throw new RequestError(401, 'authentication_error',
        'No Copilot upstream token captured yet. Trigger a request in Copilot App.');
    }
    if (this.#expiresOn && Date.parse(this.#expiresOn) <= Date.now()) {
      throw new RequestError(401, 'authentication_error',
        'Captured Copilot token has expired. Trigger a new request in Copilot App to refresh it.');
    }
    return this.#token;
  }

  status() {
    const hasToken = Boolean(this.#token);
    const expired = Boolean(this.#expiresOn && Date.parse(this.#expiresOn) <= Date.now());
    return {
      hasToken: hasToken && !expired,
      tokenExpiresOn: this.#expiresOn,
    };
  }
}

class RequestError extends Error {
  constructor(status, type, message) {
    super(message);
    this.status = status;
    this.type = type;
  }
}

function sendJson(response, status, value) {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
    connection: 'close',
  });
  response.end(JSON.stringify(value));
}

function requireSecret(value, expected, message) {
  if (typeof value === 'string') {
    const candidate = Buffer.from(value);
    const reference = Buffer.from(expected);
    if (candidate.length === reference.length && timingSafeEqual(candidate, reference)) return;
  }
  throw new RequestError(401, 'authentication_error', message);
}

function callerToken(request) {
  const credentials = request.rawHeaders.filter((header, index) => (
    index % 2 === 0 && ['authorization', 'x-api-key'].includes(header.toLowerCase())
  ));
  if (credentials.length !== 1) {
    throw new RequestError(401, 'authentication_error',
      'Provide exactly one Authorization: Bearer token or x-api-key containing a valid upstream access token.');
  }
  const authorization = request.headers.authorization;
  const token = authorization === undefined
    ? request.headers['x-api-key']
    : /^Bearer ([^\s,]+)$/i.exec(authorization)?.[1];
  if (typeof token !== 'string' || !TOKEN_PATTERN.test(token)) {
    throw new RequestError(401, 'authentication_error', 'Invalid access token format.');
  }
  return token;
}

function validateLocalRequest(request) {
  const host = request.headers.host;
  if (typeof host !== 'string') {
    throw new RequestError(403, 'permission_error', 'A loopback Host header is required.');
  }
  const authority = new URL(`http://${host}`);
  const port = authority.port || '80';
  if (!['localhost', '127.0.0.1'].includes(authority.hostname)
      || authority.username || authority.password || authority.pathname !== '/'
      || authority.search || authority.hash || Number(port) !== request.socket.localPort) {
    throw new RequestError(403, 'permission_error', 'Only this loopback endpoint is allowed.');
  }
  if (request.headers.origin !== undefined) {
    throw new RequestError(403, 'permission_error', 'Browser-origin requests are not allowed.');
  }
  if (!request.url?.startsWith('/') || request.url.startsWith('//')) {
    throw new RequestError(400, 'invalid_request_error', 'Only relative API paths are allowed.');
  }
}

function readJsonBody(request, limit, timeoutMs) {
  return new Promise((resolve, reject) => {
    const declaredLength = Number(request.headers['content-length']);
    if (Number.isFinite(declaredLength) && declaredLength > limit) {
      reject(new RequestError(413, 'invalid_request_error', 'Request body exceeds the configured limit.'));
      return;
    }
    let length = 0;
    const chunks = [];
    const timer = setTimeout(() => {
      finish(new RequestError(408, 'timeout_error', 'Request body timed out.'));
      request.pause();
    }, timeoutMs);
    const finish = (error, body) => {
      clearTimeout(timer);
      request.removeListener('data', onData);
      request.removeListener('end', onEnd);
      request.removeListener('error', onError);
      request.removeListener('aborted', onAborted);
      if (error) reject(error);
      else resolve(body);
    };
    const onData = (chunk) => {
      length += chunk.length;
      if (length > limit) {
        request.pause();
        finish(new RequestError(413, 'invalid_request_error', 'Request body exceeds the configured limit.'));
      } else {
        chunks.push(chunk);
      }
    };
    const onEnd = () => {
      const body = Buffer.concat(chunks);
      try {
        const parsed = JSON.parse(body.toString('utf8'));
        if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
          throw new Error('Not a JSON object');
        }
        finish(undefined, body);
      } catch {
        finish(new RequestError(400, 'invalid_request_error', 'Request body must be a JSON object.'));
      }
    };
    const onError = () => finish(new RequestError(400, 'invalid_request_error', 'Request body could not be read.'));
    const onAborted = () => finish(new RequestError(400, 'invalid_request_error', 'Request was aborted.'));
    request.on('data', onData);
    request.once('end', onEnd);
    request.once('error', onError);
    request.once('aborted', onAborted);
  });
}

function forward(request, response, target, token, body, timeoutMs) {
  const transport = target.protocol === 'https:' ? https : http;
  const headers = {
    authorization: `Bearer ${token}`,
    accept: 'application/json, text/event-stream',
    'accept-encoding': 'identity',
    'user-agent': 'local-messages-gateway/0.1',
    'x-clienttype': 'Copilot',
    'x-clientscenario': 'Code',
    'x-clientcallid': randomUUID(),
    'x-conversationid': request.headers['x-conversationid'] || randomUUID(),
    'x-llm-api-version': request.headers['x-llm-api-version'] || '2026-10-21',
    'anthropic-version': request.headers['anthropic-version'] || '2023-06-01',
  };
  if (request.headers['anthropic-beta']) headers['anthropic-beta'] = request.headers['anthropic-beta'];
  if (body !== undefined) {
    headers['content-type'] = 'application/json';
    headers['content-length'] = String(body.length);
  }
  const upstreamRequest = transport.request(target, {
    method: request.method,
    headers,
  }, (upstreamResponse) => {
    if (response.destroyed) {
      upstreamResponse.destroy();
      return;
    }
    response.statusCode = upstreamResponse.statusCode || 502;
    response.setHeader('cache-control', 'no-store');
    response.setHeader('x-content-type-options', 'nosniff');
    response.setHeader('x-accel-buffering', 'no');
    for (const name of RESPONSE_HEADERS) {
      const value = upstreamResponse.headers[name];
      if (value !== undefined) response.setHeader(name, value);
    }
    response.flushHeaders();
    pipeline(upstreamResponse, response, () => {
      if (!upstreamRequest.destroyed) upstreamRequest.destroy();
    });
  });
  upstreamRequest.setTimeout(timeoutMs, () => {
    const error = new Error('Upstream timeout');
    error.code = 'ETIMEDOUT';
    upstreamRequest.destroy(error);
  });
  upstreamRequest.on('error', (error) => {
    if (response.headersSent) response.destroy();
    else sendJson(response, error.code === 'ETIMEDOUT' ? 504 : 502, {
      type: 'error',
      error: { type: 'api_error', message: error.code === 'ETIMEDOUT'
        ? 'Upstream timed out.' : 'Upstream connection failed.' },
    });
  });
  response.once('close', () => {
    if (!upstreamRequest.destroyed) upstreamRequest.destroy();
  });
  upstreamRequest.end(body);
}

export function createGateway({
  upstreamBase = DEFAULT_UPSTREAM,
  bodyLimit = 8 * 1024 * 1024,
  timeoutMs = 120_000,
  tokenProvider,
  localApiKey,
  relayStore,
  relayIngestSecret,
  authMode,
} = {}) {
  if (tokenProvider !== undefined && (typeof tokenProvider !== 'function'
      || typeof localApiKey !== 'string' || localApiKey.length < 32)) {
    throw new Error('Managed login requires a token provider and a local API key of at least 32 characters.');
  }
  if (relayStore !== undefined && (typeof relayIngestSecret !== 'string' || relayIngestSecret.length < 32)) {
    throw new Error('Relay mode requires a relayIngestSecret of at least 32 characters.');
  }
  const base = new URL(upstreamBase);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password
      || !base.pathname.endsWith('/') || base.search || base.hash) {
    throw new Error('Invalid upstream base URL.');
  }
  const server = http.createServer({ maxHeaderSize: 64 * 1024 }, async (request, response) => {
    try {
      validateLocalRequest(request);
      const url = new URL(request.url, 'http://localhost');
      if (url.search) {
        throw new RequestError(400, 'invalid_request_error', 'Query parameters are not accepted.');
      }
      if (url.pathname === '/healthz' && request.method === 'GET') {
        const relayStatus = relayStore ? relayStore.status() : null;
        sendJson(response, 200, {
          status: 'ok',
          authentication: authMode || (tokenProvider ? 'entra-msal' : 'caller-provided-upstream-token'),
          upstreamAuthenticated: relayStatus ? relayStatus.hasToken : false,
          ...(relayStatus ? { tokenExpiresOn: relayStatus.tokenExpiresOn } : {}),
        });
        return;
      }
      if (url.pathname === '/_internal/token' && relayStore) {
        if (request.method !== 'POST') {
          response.setHeader('allow', 'POST');
          throw new RequestError(405, 'invalid_request_error', 'Method not allowed.');
        }
        const secret = request.headers['x-relay-secret'];
        if (typeof secret !== 'string') {
          throw new RequestError(401, 'authentication_error', 'Missing relay ingest secret.');
        }
        requireSecret(secret, relayIngestSecret, 'Invalid relay ingest secret.');
        const rawBody = await readJsonBody(request, 64 * 1024, timeoutMs);
        const parsed = JSON.parse(rawBody.toString('utf8'));
        const info = await relayStore.setToken(parsed.token);
        sendJson(response, 200, { status: 'updated', tokenExpiresOn: info.expiresOn });
        return;
      }
      const route = ROUTES.get(url.pathname);
      if (!route) throw new RequestError(404, 'not_found_error', 'Unknown API route.');
      if (request.method !== route.method) {
        response.setHeader('allow', route.method);
        throw new RequestError(405, 'invalid_request_error', 'Method not allowed.');
      }
      let token = callerToken(request);
      if (tokenProvider) {
        requireSecret(token, localApiKey, 'Invalid local gateway key.');
        try {
          token = await tokenProvider();
        } catch (error) {
          if (error instanceof RequestError) throw error;
          throw new RequestError(401, 'authentication_error',
            'Upstream sign-in or refresh failed. Restart the gateway with --login and complete supported sign-in.');
        }
        if (typeof token !== 'string' || !TOKEN_PATTERN.test(token)) {
          throw new RequestError(401, 'authentication_error', 'No valid upstream access token is available.');
        }
      }
      const body = route.method === 'POST'
        ? await readJsonBody(request, bodyLimit, timeoutMs) : undefined;
      if (!response.destroyed) {
        forward(request, response, new URL(route.upstreamPath, base), token, body, timeoutMs);
      }
    } catch (error) {
      sendJson(response, error instanceof RequestError ? error.status : 400, {
        type: 'error',
        error: {
          type: error instanceof RequestError ? error.type : 'invalid_request_error',
          message: error instanceof RequestError ? error.message : 'Invalid request.',
        },
      });
    }
  });
  server.headersTimeout = 15_000;
  server.requestTimeout = timeoutMs;
  return server;
}

async function main() {
  const port = Number(process.env.PORT || 19324);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    console.error('PORT must be an integer between 1 and 65535.');
    process.exitCode = 1;
    return;
  }
  const mode = process.argv.includes('--relay') ? 'relay'
    : process.argv.includes('--login') ? 'login' : 'passthrough';
  const options = {};
  if (mode !== 'passthrough') {
    const localApiKey = process.env.LOCAL_GATEWAY_KEY;
    if (typeof localApiKey !== 'string' || localApiKey.length < 32) {
      console.error('Set LOCAL_GATEWAY_KEY to a random local-only key of at least 32 characters.');
      process.exitCode = 1;
      return;
    }
    options.localApiKey = localApiKey;
  }
  if (mode === 'relay') {
    const relayIngestSecret = process.env.RELAY_INGEST_SECRET;
    if (typeof relayIngestSecret !== 'string' || relayIngestSecret.length < 32) {
      console.error('Set RELAY_INGEST_SECRET to a random local-only secret of at least 32 characters.');
      process.exitCode = 1;
      return;
    }
    const tokenFile = process.env.RELAY_TOKEN_FILE || path.resolve('.local', 'token.dpapi');
    const relayStore = new RelayTokenStore({ tokenFile });
    const loaded = await relayStore.loadCached();
    if (loaded) {
      const status = relayStore.status();
      console.log(`Loaded DPAPI-protected cached token (valid=${status.hasToken}, exp=${status.tokenExpiresOn || 'unknown'}).`);
    }
    options.relayStore = relayStore;
    options.relayIngestSecret = relayIngestSecret;
    options.authMode = 'copilot-token-relay';
    options.tokenProvider = () => relayStore.getToken();
  } else if (mode === 'login') {
    let config;
    try {
      config = configFromEnvironment();
    } catch {
      console.error('Set ENTRA_CLIENT_ID and ENTRA_TENANT_ID to your own registered public-client IDs.');
      process.exitCode = 1;
      return;
    }
    const session = new EntraSession(config);
    console.log('Opening Microsoft sign-in. Complete authentication in your browser; do not paste tokens into this terminal.');
    const status = await session.signIn();
    console.log(`Microsoft sign-in completed. Token expiry: ${status.expiresOn || 'unknown'}. Upstream API access still requires verification.`);
    options.tokenProvider = () => session.getAccessToken();
  }
  {
    const server = createGateway(options);
    server.on('error', (error) => {
      console.error(error.code === 'EADDRINUSE' ? 'Loopback port is already in use.' : 'Gateway failed to start.');
      process.exitCode = 1;
    });
    server.listen(port, '127.0.0.1', () => {
      console.log(`Messages endpoint: http://127.0.0.1:${port}/v1/messages`);
      const instructions = {
        relay: 'Use the local gateway key in VS Code. Copilot App requests update the upstream token.',
        login: 'Use the local gateway key in VS Code. MSAL handles upstream refresh in memory.',
        passthrough: 'Caller must supply a valid upstream Entra access token; no tokens are acquired or stored.',
      };
      console.log(instructions[mode]);
    });
    for (const signal of ['SIGINT', 'SIGTERM']) {
      process.once(signal, () => {
        server.close();
        server.closeAllConnections();
      });
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(`Sign-in/startup failed: ${signInErrorSummary(error)}`);
    process.exitCode = 1;
  });
}