import { execFile } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';

function powershellScript(action) {
  return [
    'Add-Type -AssemblyName System.Security',
    '$inputText = [Console]::In.ReadToEnd().Trim()',
    'if (-not $inputText) { exit 1 }',
    action === 'protect'
      ? '$bytes = [Text.Encoding]::UTF8.GetBytes($inputText); $out = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser); [Console]::Out.Write([Convert]::ToBase64String($out))'
      : '$bytes = [Convert]::FromBase64String($inputText); $out = [Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser); [Console]::Out.Write([Text.Encoding]::UTF8.GetString($out))',
  ].join('; ');
}

async function runDpapi(action, stdinText) {
  if (process.platform !== 'win32') {
    throw new Error('DPAPI is only supported on Windows.');
  }
  return new Promise((resolve, reject) => {
    const child = execFile(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command', powershellScript(action)],
      { windowsHide: true, maxBuffer: 1024 * 1024 },
      (error, stdout) => {
        if (error) reject(new Error(`DPAPI ${action} failed.`));
        else resolve(stdout);
      },
    );
    child.stdin.end(stdinText, 'utf8');
  });
}

export async function saveProtectedText(filePath, plainText) {
  const encrypted = await runDpapi('protect', plainText);
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, encrypted, 'utf8');
}

export async function loadProtectedText(filePath) {
  const encrypted = await fs.readFile(filePath, 'utf8');
  return runDpapi('unprotect', encrypted);
}
