import { spawn } from 'node:child_process';
import { PublicClientApplication } from '@azure/msal-node';

const RESOURCE_ID = '42c8f7bd-c7a8-4ba9-bc1c-91edfb716247';
const GUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;

export function configFromEnvironment(environment = process.env) {
  const clientId = environment.ENTRA_CLIENT_ID;
  const tenantId = environment.ENTRA_TENANT_ID;
  if (!GUID.test(clientId || '') || !GUID.test(tenantId || '')) {
    throw new Error('Set ENTRA_CLIENT_ID and ENTRA_TENANT_ID to your own registered public-client IDs.');
  }
  return { clientId, tenantId };
}

export async function openSignInBrowser(url) {
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' || parsed.hostname !== 'login.microsoftonline.com') {
    throw new Error('Unexpected sign-in authority.');
  }
  const command = process.platform === 'win32' ? 'rundll32.exe'
    : process.platform === 'darwin' ? 'open' : 'xdg-open';
  const args = process.platform === 'win32' ? ['url.dll,FileProtocolHandler', url] : [url];
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'ignore', windowsHide: true });
    child.once('error', () => reject(new Error('Could not open the sign-in browser.')));
    child.once('exit', (code) => code === 0 ? resolve()
      : reject(new Error('Could not open the sign-in browser.')));
  });
}

export class EntraSession {
  #application;
  #account;
  #expiresOn;
  #scopes = [`${RESOURCE_ID}/.default`];

  constructor(config, application) {
    if (!GUID.test(config.clientId || '') || !GUID.test(config.tenantId || '')) {
      throw new Error('Invalid public-client registration.');
    }
    this.#application = application || new PublicClientApplication({
      auth: {
        clientId: config.clientId,
        authority: `https://login.microsoftonline.com/${config.tenantId}`,
      },
      system: { loggerOptions: { loggerCallback() {}, piiLoggingEnabled: false } },
    });
  }

  async signIn(openBrowser = openSignInBrowser) {
    const result = await this.#application.acquireTokenInteractive({
      scopes: this.#scopes,
      openBrowser,
      successTemplate: '<!doctype html><title>Signed in</title><p>You can close this window.</p>',
      errorTemplate: '<!doctype html><title>Sign-in failed</title><p>Return to the gateway terminal.</p>',
    });
    if (!result?.account || !result.accessToken) {
      throw new Error('Sign-in did not return an account and access token.');
    }
    this.#account = result.account;
    this.#expiresOn = result.expiresOn;
    return this.status();
  }

  async getAccessToken() {
    if (!this.#account) throw new Error('Interactive sign-in is required.');
    const result = await this.#application.acquireTokenSilent({
      scopes: this.#scopes,
      account: this.#account,
    });
    if (!result?.accessToken) throw new Error('No upstream access token is available.');
    this.#expiresOn = result.expiresOn;
    return result.accessToken;
  }

  status() {
    return {
      signedIn: Boolean(this.#account),
      expiresOn: this.#expiresOn?.toISOString() || null,
      resource: RESOURCE_ID,
    };
  }
}

export function signInErrorSummary(error) {
  const aadsts = /AADSTS[0-9]+/.exec(String(error?.errorMessage || error?.message || ''))?.[0];
  const code = typeof error?.errorCode === 'string' && /^[a-z0-9_]{1,80}$/i.test(error.errorCode)
    ? error.errorCode : 'sign_in_failed';
  return aadsts ? `${code} (${aadsts})` : code;
}