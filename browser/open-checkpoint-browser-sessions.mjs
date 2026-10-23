#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { chmod, readFile, writeFile, mkdir, mkdtemp, rm, readdir } from 'node:fs/promises';
import { existsSync, readFileSync, lstatSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const sharedDirectory = path.dirname(fileURLToPath(import.meta.url));
const scriptDirectory = process.env.QA_PROJECT_TOOLS;
const geolocationHolderPath = path.join(sharedDirectory, 'hold-chrome-geolocation.mjs');
const browserSessionCheckpointHarnessPath = process.env.QA_BROWSER_FIXTURE_HARNESS;
const configurationPath = process.env.QA_BROWSER_SESSION_CONFIG;
if (!scriptDirectory || !browserSessionCheckpointHarnessPath || !configurationPath) {
  throw new Error('Use the project launcher, or supply QA_PROJECT_TOOLS, QA_BROWSER_FIXTURE_HARNESS and QA_BROWSER_SESSION_CONFIG.');
}
const chromeBinary = process.env.CHROME_BIN || '/opt/google/chrome/chrome';
const argumentsList = process.argv.slice(2);
const headless = argumentsList.includes('--headless');
const closeOnly = argumentsList.includes('--close-only');
const dismissGuidesOnly = argumentsList.includes('--dismiss-guides');
const verifyExistingOnly = argumentsList.includes('--verify-existing');
const reopenActor = argumentsList.find(argument => argument.startsWith('--reopen-actor='))?.split('=')[1];
const checkpointId = `${argumentsList.find(argument => !argument.startsWith('--')) || process.env.QA_CHECKPOINT || ''}`.trim();
const runtimeRoot = path.join(os.tmpdir(), process.env.QA_BROWSER_RUNTIME_NAMESPACE || 'qa-browser-sessions');

if (!checkpointId) {
  fail('Usage: open-checkpoint-browser-sessions.mjs CHECKPOINT_ID');
}
if (!existsSync(chromeBinary)) {
  fail(`Chrome is not executable at ${chromeBinary}`);
}

const configuration = JSON.parse(await readFile(configurationPath, 'utf8'));
const checkpoint = configuration.checkpoints?.[checkpointId];
if (!checkpoint || !Array.isArray(checkpoint.sessions) || checkpoint.sessions.length === 0) {
  fail(`No browser-session configuration exists for ${checkpointId}`);
}

// Fixture readiness is verified before any existing managed session is replaced.
if (checkpoint.fixtureScenario && checkpoint.device !== 'android') {
  const baseline = await runAttachedCommand('bash', [browserSessionCheckpointHarnessPath,
    'ready', checkpoint.fixtureScenario, checkpoint.fixtureCheckpointId || checkpointId], process.env);
  const result = JSON.parse(baseline.stdout);
  if (result.checkpointId !== (checkpoint.fixtureCheckpointId || checkpointId)
      || result.phase !== checkpoint.fixtureReadyPhase) fail('The configured browser fixture is not ready.');
}

const defaults = configuration.defaults ?? {};
const defaultTargetUrl = `${checkpoint.targetUrl || defaults.targetUrl || 'http://localhost/game'}`;
const defaultEntryUrl = `${checkpoint.entryUrl || defaults.entryUrl || 'http://localhost/entry?redirect=%2Fgame'}`;
const configuredAppOrigins = new Set([
  defaultEntryUrl, defaultTargetUrl,
  ...checkpoint.sessions.flatMap(session => [
    session.entryUrl || defaultEntryUrl, session.targetUrl || defaultTargetUrl
  ])
].flatMap(url => {
  const origin = new URL(url).origin;
  return [origin, origin.replace('http:', 'https:')];
}));
const storageScope = `${checkpoint.storageScope || defaults.storageScope || 'http'}`.trim();
const bootstrapEntryConsent = checkpoint.bootstrapEntryConsent
  ?? defaults.bootstrapEntryConsent
  ?? true;
const defaultGeolocation = checkpoint.geolocation ?? defaults.geolocation ?? {
  latitude: 30.2672,
  longitude: -97.7431,
  accuracy: 10
};
const screenWidth = positiveInteger(process.env.QA_SCREEN_WIDTH, defaults.screenWidth ?? 1920);
const screenHeight = positiveInteger(process.env.QA_SCREEN_HEIGHT, defaults.screenHeight ?? 1000);
const columns = Math.min(
  checkpoint.sessions.length,
  positiveInteger(process.env.QA_BROWSER_COLUMNS, checkpoint.layout?.columns ?? checkpoint.sessions.length)
);
const rows = Math.ceil(checkpoint.sessions.length / columns);
const windowWidth = Math.floor(screenWidth / columns);
const windowHeight = Math.floor(screenHeight / rows);
const safeCheckpointId = checkpointId.replace(/[^A-Za-z0-9_.-]+/g, '-');
const manifestPath = path.join(runtimeRoot, `${safeCheckpointId}.json`);

await mkdir(runtimeRoot, { recursive: true, mode: 0o700 });
await chmod(runtimeRoot, 0o700);
if (checkpoint.device === 'android') {
  if (checkpoint.sessions.length !== 1 || checkpoint.sessions[0].sessionKind !== 'anonymous') {
    fail('Physical-provider startup requires one anonymous session; login must happen through the UI.');
  }
  let baseline;
  if (checkpoint.fixtureScenario) {
    if (!checkpoint.fixtureCheckpointId || !checkpoint.fixtureReadyPhase) fail('Configure the fixture checkpoint and ready phase.');
    const result = await runAttachedCommand('bash', [browserSessionCheckpointHarnessPath,
      'ready', checkpoint.fixtureScenario, checkpoint.fixtureCheckpointId], process.env);
    baseline = JSON.parse(result.stdout);
    if (baseline.checkpointId !== checkpoint.fixtureCheckpointId || baseline.phase !== checkpoint.fixtureReadyPhase) {
      fail('The configured phone fixture is not ready.');
    }
    baseline.checkedAt = new Date().toISOString();
  } else {
    const statusPath = process.env.QA_DEPLOY_FIXTURE_STATUS;
    if (!statusPath) fail('Verify the configured deployment fixture and provide QA_DEPLOY_FIXTURE_STATUS.');
    baseline = JSON.parse(await readFile(statusPath, 'utf8'));
    const authenticationReady = process.env.QA_DEPLOY_ALLOW_DEMO === 'true' && checkpoint.authenticationMode === 'demo'
      ? baseline.demoActive === true : baseline.firebaseActive === true;
    if (baseline.checkpointId !== checkpointId || baseline.phase !== 'baseline-ready'
      || !authenticationReady || Date.now() - Date.parse(baseline.checkedAt) > 300000
      || !Number.isFinite(Date.parse(baseline.checkedAt))) {
      fail('A current verified configured-deployment baseline is required.');
    }
  }
  if (closeOnly || reopenActor !== undefined || dismissGuidesOnly) {
    fail('Android session cleanup/reopen uses the physical-device helper and its recorded state.');
  }
  if (verifyExistingOnly) {
    const retained = JSON.parse(await readFile(manifestPath, 'utf8'));
    if (retained.device !== 'android' || retained.checkpointId !== checkpointId) fail('Wrong retained device checkpoint.');
    process.stdout.write('Verified Android checkpoint manifest; device acceptance remains separate.\n');
    process.exit(0);
  }
  const url = new URL(defaultEntryUrl);
  await runAttachedCommand(process.execPath, [path.join(scriptDirectory, 'mobile-webview-device.mjs'),
    'launch-browser', url.pathname + url.search], {...process.env, QA_PHONE_ORIGIN: url.origin});
  await writeFile(manifestPath, JSON.stringify({checkpointId, device: 'android', createdAt: new Date().toISOString(),
    origin: url.origin, fixtureCheckedAt: baseline.checkedAt,
    sessions: [{actor: checkpoint.sessions[0].actor, sessionKind: 'anonymous'}]}, null, 2), {mode: 0o600});
  process.stdout.write('Configured Android entry opened; no login, consent, location override or checklist action injected.\n');
  process.exit(0);
}
if (reopenActor !== undefined) {
  await reopenManagedActor(reopenActor);
  process.exit(0);
}
if (closeOnly) {
  await closePreviousCheckpointSessions(manifestPath);
  process.exit(0);
}
if (verifyExistingOnly) {
  await verifyExistingManagedSessions(manifestPath);
  process.exit(0);
}
if (dismissGuidesOnly) {
  const existing = JSON.parse(await readFile(manifestPath, 'utf8'));
  for (const session of existing.sessions ?? []) {
    await dismissExplanationGuide(Number(session.devToolsPort));
    process.stdout.write(`${session.actor}: explanation guide dismissed.\n`);
  }
  process.exit(0);
}
if (!checkpoint.preserveOtherSessions) await closeOtherManagedCheckpointSessions(manifestPath);
await closePreviousCheckpointSessions(manifestPath);
await prepareConfiguredDemoSessions();

const manifest = {
  checkpointId,
  headless,
  createdAt: new Date().toISOString(),
  sessions: []
};

try {
  for (const [index, session] of checkpoint.sessions.entries()) {
    validateSession(session);
    const sessionKind = `${session.sessionKind ?? 'demo'}`.trim().toLowerCase();
    const targetUrl = `${session.targetUrl || defaultTargetUrl}`;
    const entryUrl = `${session.entryUrl || defaultEntryUrl}`;
    const actorSlug = `${session.actor}`.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    const userDataDirectory = await mkdtemp(
      path.join(runtimeRoot, `${safeCheckpointId}-${index + 1}-${actorSlug}-`)
    );
    const geolocation = session.geolocation ?? defaultGeolocation;
    const geolocationPermission = normalizeGeolocationPermission(
      session.geolocationPermission ?? checkpoint.geolocationPermission ?? 'granted'
    );
    const microphonePermission = normalizeMicrophonePermission(
      session.microphonePermission ?? checkpoint.microphonePermission ?? 'prompt'
    );
    const cameraPermission = normalizeMicrophonePermission(session.cameraPermission ?? checkpoint.cameraPermission ?? 'prompt');
    await prepareChromeProfilePreferences(userDataDirectory, geolocationPermission, microphonePermission, cameraPermission);
    const column = index % columns;
    const row = Math.floor(index / columns);
    const position = {
      x: column * windowWidth,
      y: row * windowHeight,
      width: windowWidth,
      height: windowHeight,
      ...(session.window ?? {})
    };
    if (!['x', 'y', 'width', 'height'].every(key => Number.isInteger(position[key]))
      || position.width <= 0 || position.height <= 0) {
      fail(`Invalid configured window bounds for ${session.actor}`);
    }

    const browserArguments = [
      ...(headless ? ['--headless=new'] : []),
      `--user-data-dir=${userDataDirectory}`,
      '--remote-debugging-port=0',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-session-crashed-bubble',
      '--hide-crash-restore-bubble',
      ...((checkpoint.ignoreCertificateErrors ?? defaults.ignoreCertificateErrors ?? true)
        ? ['--ignore-certificate-errors'] : []),
      ...(checkpoint.hostResolverRules
        ? [`--host-resolver-rules=${checkpoint.hostResolverRules}`] : []),
      '--ozone-platform=x11',
      `--window-position=${position.x},${position.y}`,
      `--window-size=${position.width},${position.height}`,
      ...(session.fakeMediaStream === true ? ['--use-fake-device-for-media-stream'] : []),
      ...(checkpoint.layout?.appWindows === true ? [`--app=${session.entryUrl || defaultEntryUrl}`] : ['about:blank'])
    ];
    const browser = spawn(chromeBinary, browserArguments, {
      detached: true,
      stdio: 'ignore'
    });
    browser.unref();

    const runtimeSession = {
      actor: session.actor,
      expectedName: session.expectedName,
      sessionKind,
      userId: session.userId,
      geolocation,
      geolocationPermission,
      microphonePermission,
      cameraPermission,
      fakeMediaStream: session.fakeMediaStream === true,
      pid: browser.pid,
      userDataDirectory,
      position
    };
    manifest.sessions.push(runtimeSession);
    await persistManifest();

    const port = await waitForDevToolsPort(userDataDirectory);
    runtimeSession.devToolsPort = port;
    runtimeSession.geolocationHolderPid = await prepareBrowserLocation(
      port,
      entryUrl,
      geolocation,
      geolocationPermission,
      userDataDirectory
    );
    await prepareBrowserMicrophone(port, entryUrl, microphonePermission);
    await prepareBrowserMicrophone(port, entryUrl, cameraPermission, 'camera');
    if (sessionKind === 'anonymous') {
      await clearManagedSession(port, storageScope, session.localStorage);
      if (bootstrapEntryConsent) {
        await acceptEntryConsentOnCurrentEntry(port, storageScope);
      }
      await waitForAnonymousEntry(port, storageScope, entryUrl);
    } else if (sessionKind === 'operator-bootstrap') {
      await installOperatorSession(port, storageScope, session, targetUrl);
      runtimeSession.authenticatedAt = new Date().toISOString();
    } else {
      runtimeSession.sessionId = await registerTrackedDemoSession(port, session.userId);
      runtimeSession.sessionOrigin = new URL(targetUrl).origin;
      await persistManifest();
      await installDemoSession(
        port,
        storageScope,
        session.userId,
        runtimeSession.sessionId,
        targetUrl,
        session.localStorage
      );
      if (bootstrapEntryConsent) {
        await acceptEntryConsent(port, storageScope, targetUrl);
      }
      await waitForConfiguredSession(
        port,
        storageScope,
        session.userId,
        runtimeSession.sessionId,
        targetUrl
      );
      runtimeSession.authenticatedAt = new Date().toISOString();
      const autoDismissExplanationGuide = session.autoDismissExplanationGuide
        ?? checkpoint.autoDismissExplanationGuide
        ?? true;
      if (autoDismissExplanationGuide) {
        await dismissExplanationGuide(port);
      }
    }
    await persistManifest();
    process.stdout.write(`${session.actor}: ${session.expectedName} ready.\n`);
  }
  if (checkpoint.startupOracle === 'ui-poll') {
    await runAttachedCommand(process.execPath,
      [path.join(scriptDirectory, 'verify-ui-poll-sessions.mjs'), 'preflight', manifestPath], process.env);
    process.stdout.write('UI-POLL-JANK-001: three identities, Group Chat visibility and exactly two Casey sessions verified.\n');
  }
} catch (error) {
  try {
    await closePreviousCheckpointSessions(manifestPath);
  } catch (cleanupError) {
    const failure = error instanceof Error ? error.message : `${error}`;
    const cleanupFailure = cleanupError instanceof Error ? cleanupError.message : `${cleanupError}`;
    fail(`${failure} Cleanup also failed: ${cleanupFailure}`);
  }
  fail(error instanceof Error ? error.message : `${error}`);
}

process.stdout.write(`${checkpointId}: ${manifest.sessions.length} isolated browser sessions ready.\n`);

async function persistManifest() {
  await writeFile(
    manifestPath,
    `${JSON.stringify(manifest, null, 2)}\n`,
    { mode: 0o600 }
  );
  await chmod(manifestPath, 0o600);
}

async function prepareConfiguredDemoSessions() {
  const userIds = [...new Set([
    ...(checkpoint.additionalDemoUserIds || []),
    ...checkpoint.sessions
      .filter(session => `${session.sessionKind ?? 'demo'}`.trim().toLowerCase() === 'demo')
      .map(session => `${session.userId ?? ''}`.trim())
      .filter(Boolean)
  ])];
  if (userIds.length === 0) return;
  if (!existsSync(browserSessionCheckpointHarnessPath)) {
    throw new Error(`Browser-session checkpoint harness is missing at ${browserSessionCheckpointHarnessPath}.`);
  }
  const result = await runAttachedCommand(
    'bash',
    // A companion actor uses the same fixture snapshot without replacing the
    // ongoing participant windows (for example app-admin moderation).
    [browserSessionCheckpointHarnessPath, 'prepare', 'browser-sessions', checkpoint.fixtureCheckpointId || checkpointId],
    {
      ...process.env,
      MANUAL_QA_BROWSER_USER_IDS: userIds.join(',')
    }
  );
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
}

async function runAttachedCommand(executable, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, {
      env,
      stdio: ['ignore', 'pipe', 'pipe']
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) {
        reject(new Error(
          `Browser-session preparation failed with exit code ${code}.${stderr ? ` ${stderr.trim()}` : ''}`
        ));
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

async function reopenManagedActor(actor) {
  const existing = JSON.parse(await readFile(manifestPath, 'utf8'));
  const index = /^\d+$/.test(actor) ? Number(actor) : checkpoint.sessions.findIndex(item => item.actor === actor);
  const configured = checkpoint.sessions[index];
  const retained = existing.sessions?.[index];
  if (existing.checkpointId !== checkpointId || !configured || !retained
      || retained.userId !== configured.userId || retained.sessionKind !== 'demo' || !retained.sessionId
      || !existsSync(retained.userDataDirectory)) throw new Error('No retained demo actor matches the reopen request.');
  if (ownsRuntimeDirectory(retained.pid, retained.userDataDirectory)) throw new Error('This managed browser is still running.');
  if (ownsGeolocationHolder(retained.geolocationHolderPid)) {
    process.kill(retained.geolocationHolderPid, 'SIGTERM');
  }
  // Reuse this profile and its tracked login; do not reset actors or register another session.
  await rm(path.join(retained.userDataDirectory, 'DevToolsActivePort'), { force: true });
  const entryUrl = `${configured.entryUrl || defaultEntryUrl}`;
  const targetUrl = `${configured.targetUrl || defaultTargetUrl}`;
  const position = retained.position;
  const browser = spawn(chromeBinary, [
    ...(existing.headless === true ? ['--headless=new'] : []),
    `--user-data-dir=${retained.userDataDirectory}`, '--remote-debugging-port=0', '--no-first-run',
    '--no-default-browser-check', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
    ...((checkpoint.ignoreCertificateErrors ?? defaults.ignoreCertificateErrors ?? true) ? ['--ignore-certificate-errors'] : []),
    ...(checkpoint.hostResolverRules ? [`--host-resolver-rules=${checkpoint.hostResolverRules}`] : []),
    '--ozone-platform=x11', `--window-position=${position.x},${position.y}`, `--window-size=${position.width},${position.height}`,
    ...(configured.fakeMediaStream === true ? ['--use-fake-device-for-media-stream'] : []),
    ...(checkpoint.layout?.appWindows === true ? [`--app=${entryUrl}`] : ['about:blank'])
  ], { detached: true, stdio: 'ignore' });
  browser.unref();
  retained.pid = browser.pid;
  const save = async () => writeFile(manifestPath, `${JSON.stringify(existing, null, 2)}\n`, { mode: 0o600 });
  await save();
  const port = await waitForDevToolsPort(retained.userDataDirectory);
  retained.devToolsPort = port;
  await save();
  retained.geolocationHolderPid = await prepareBrowserLocation(port, entryUrl, retained.geolocation,
    retained.geolocationPermission, retained.userDataDirectory);
  await save();
  await prepareBrowserMicrophone(port, entryUrl, retained.microphonePermission);
  await prepareBrowserMicrophone(port, entryUrl, retained.cameraPermission ?? 'prompt', 'camera');
  await installDemoSession(port, storageScope, retained.userId, retained.sessionId, targetUrl, configured.localStorage);
  if (bootstrapEntryConsent) await acceptEntryConsent(port, storageScope, targetUrl);
  await waitForConfiguredSession(port, storageScope, retained.userId, retained.sessionId, targetUrl);
  if (configured.autoDismissExplanationGuide ?? checkpoint.autoDismissExplanationGuide ?? true) {
    await dismissExplanationGuide(port);
  }
  retained.readinessVerifiedAt = new Date().toISOString();
  await save();
  process.stdout.write(`${retained.actor}: retained session reopened at its configured window position.\n`);
}

async function verifyExistingManagedSessions(filePath) {
  if (!existsSync(filePath)) {
    throw new Error(`No managed browser-session manifest exists for ${checkpointId}.`);
  }
  const existing = JSON.parse(await readFile(filePath, 'utf8'));
  if (existing.checkpointId !== checkpointId || !Array.isArray(existing.sessions)
      || existing.sessions.length !== checkpoint.sessions.length) {
    throw new Error(`Managed browser-session manifest for ${checkpointId} is incomplete.`);
  }
  for (const session of existing.sessions) {
    if (`${session.sessionKind ?? ''}` !== 'demo') continue;
    const port = Number(session.devToolsPort);
    const userId = `${session.userId ?? ''}`.trim();
    const sessionId = `${session.sessionId ?? ''}`.trim();
    const configuredSession = checkpoint.sessions.find(item => item.userId === userId);
    const targetUrl = `${configuredSession?.targetUrl || checkpoint.targetUrl || defaults.targetUrl || 'http://localhost/game'}`;
    if (!Number.isInteger(port) || port <= 0 || !userId || !sessionId) {
      throw new Error(`Managed browser-session state for ${userId || 'unknown user'} is incomplete.`);
    }
    await waitForConfiguredSession(port, storageScope, userId, sessionId, targetUrl);
    const holderPath = path.join(session.userDataDirectory, 'GeolocationHolderReady.json');
    if (existsSync(holderPath)) {
      const holder = JSON.parse(await readFile(holderPath, 'utf8'));
      if (holder.status === 'ready' && Number(holder.port) === port && ownsGeolocationHolder(holder.pid)) {
        session.geolocationHolderPid = holder.pid;
      }
    }
    session.readinessVerifiedAt = new Date().toISOString();
    process.stdout.write(`${session.actor}: /auth/me and realtime ready.\n`);
  }
  existing.readinessVerifiedAt = new Date().toISOString();
  await writeFile(filePath, `${JSON.stringify(existing, null, 2)}\n`, { mode: 0o600 });
  await chmod(filePath, 0o600);
  process.stdout.write(`${checkpointId}: all managed sessions passed the final readiness gate.\n`);
}

function positiveInteger(raw, fallback) {
  const value = Number.parseInt(`${raw ?? ''}`, 10);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function normalizeGeolocationPermission(raw) {
  const value = `${raw ?? ''}`.trim().toLowerCase();
  if (value === 'granted' || value === 'denied' || value === 'prompt') {
    return value;
  }
  throw new Error(`Invalid geolocation permission '${value}' in ${checkpointId}`);
}

function normalizeMicrophonePermission(raw) {
  const value = `${raw ?? ''}`.trim().toLowerCase();
  if (value === 'granted' || value === 'denied' || value === 'prompt') {
    return value;
  }
  throw new Error(`Invalid microphone permission '${value}' in ${checkpointId}`);
}

function validateSession(session) {
  const sessionKind = `${session?.sessionKind ?? 'demo'}`.trim().toLowerCase();
  const validKind = ['demo', 'anonymous', 'operator-bootstrap'].includes(sessionKind);
  const hasRequiredIdentity = sessionKind === 'anonymous' || (sessionKind === 'operator-bootstrap' && !!process.env[session.credentialFileEnvironment])
    || Boolean(`${session?.userId ?? ''}`.trim());
  if (!session || !`${session.actor ?? ''}`.trim() || !validKind || !hasRequiredIdentity) {
    throw new Error(`Invalid browser-session entry in ${checkpointId}`);
  }
  normalizeLocalStorageEntries(session.localStorage);
}

function normalizeLocalStorageEntries(value) {
  if (value == null) {
    return [];
  }
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Invalid localStorage configuration in ${checkpointId}`);
  }
  return Object.entries(value).map(([key, entryValue]) => {
    const normalizedKey = `${key ?? ''}`.trim();
    if (!normalizedKey) {
      throw new Error(`Empty localStorage key in ${checkpointId}`);
    }
    const serializedValue = typeof entryValue === 'string'
      ? entryValue
      : JSON.stringify(entryValue);
    if (serializedValue === undefined) {
      throw new Error(`Invalid localStorage value for '${normalizedKey}' in ${checkpointId}`);
    }
    return [normalizedKey, serializedValue];
  });
}

async function prepareChromeProfilePreferences(userDataDirectory, geolocationPermission, microphonePermission, cameraPermission = 'prompt') {
  const defaultProfileDirectory = path.join(userDataDirectory, 'Default');
  await mkdir(defaultProfileDirectory, { recursive: true });
  const defaultContentSettings = {
    ...(geolocationPermission === 'prompt' ? {} : { geolocation: geolocationPermission === 'granted' ? 1 : 2 }),
    ...(microphonePermission === 'prompt' ? {} : { media_stream_mic: microphonePermission === 'granted' ? 1 : 2 }),
    ...(cameraPermission === 'prompt' ? {} : { media_stream_camera: cameraPermission === 'granted' ? 1 : 2 })
  };
  await writeFile(path.join(defaultProfileDirectory, 'Preferences'), `${JSON.stringify({
    profile: {
      exit_type: 'Normal',
      exited_cleanly: true,
      default_content_setting_values: defaultContentSettings
    }
  })}\n`);
}

async function prepareBrowserMicrophone(port, entryPageUrl, permission, name = 'microphone') {
  if (permission === 'prompt') {
    return;
  }
  const origin = new URL(entryPageUrl).origin;
  const browser = await jsonRequest(`http://127.0.0.1:${port}/json/version`);
  if (!browser.webSocketDebuggerUrl) {
    throw new Error(`Chrome microphone permission target is unavailable on port ${port}.`);
  }
  for (const allowedOrigin of new Set([origin, origin.replace('http:', 'https:')])) {
    await cdpCommand(browser.webSocketDebuggerUrl, 'Browser.setPermission', {
      origin: allowedOrigin,
      permission: { name },
      setting: permission
    });
  }
}

async function closePreviousCheckpointSessions(filePath) {
  if (!existsSync(filePath)) {
    return;
  }
  let previous;
  try {
    previous = JSON.parse(await readFile(filePath, 'utf8'));
  } catch {
    return;
  }
  let cleanupError = null;
  for (const session of previous.sessions ?? []) {
    try {
      await revokeTrackedDemoSession(session);
    } catch (error) {
      cleanupError ??= error;
    }
    const holderPid = Number(session.geolocationHolderPid);
    if (Number.isInteger(holderPid) && holderPid > 1 && ownsGeolocationHolder(holderPid)) {
      try {
        process.kill(holderPid, 'SIGTERM');
      } catch (error) {
        if (error?.code !== 'ESRCH') {
          throw error;
        }
      }
    }
    const pid = Number(session.pid);
    const userDataDirectory = `${session.userDataDirectory ?? ''}`;
    if (Number.isInteger(pid) && pid > 1 && ownsRuntimeDirectory(pid, userDataDirectory)) {
      try {
        process.kill(pid, 'SIGTERM');
      } catch (error) {
        if (error?.code !== 'ESRCH') {
          throw error;
        }
      }
    }
  }
  await waitUntil(() => (previous.sessions ?? []).every(session => !processExists(Number(session.pid))), 5000);
  for (const session of previous.sessions ?? []) {
    const directory = `${session.userDataDirectory ?? ''}`;
    if (directory.startsWith(`${runtimeRoot}${path.sep}`)) {
      await rm(directory, { recursive: true, force: true });
    }
  }
  await rm(filePath, { force: true });
  if (cleanupError) {
    throw cleanupError;
  }
}

async function closeOtherManagedCheckpointSessions(currentManifestPath) {
  const entries = await readdir(runtimeRoot, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) {
      continue;
    }
    const filePath = path.join(runtimeRoot, entry.name);
    if (filePath !== currentManifestPath) {
      await closePreviousCheckpointSessions(filePath);
    }
  }
}

function ownsRuntimeDirectory(pid, directory) {
  if (!directory.startsWith(`${runtimeRoot}${path.sep}`)) {
    return false;
  }
  try {
    return readFileSync(`/proc/${pid}/cmdline`, 'utf8').includes(`--user-data-dir=${directory}`);
  } catch {
    return false;
  }
}

function ownsGeolocationHolder(pid) {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, 'utf8').includes(geolocationHolderPath);
  } catch {
    return false;
  }
}

function processExists(pid) {
  if (!Number.isInteger(pid) || pid < 2) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitForDevToolsPort(userDataDirectory) {
  const activePortPath = path.join(userDataDirectory, 'DevToolsActivePort');
  let raw = '';
  await waitUntil(() => {
    try {
      raw = readFileSync(activePortPath, 'utf8');
      return raw.trim().length > 0;
    } catch {
      return false;
    }
  }, 20000, `Chrome DevTools port file ${activePortPath}`);
  const port = Number.parseInt(raw.split(/\r?\n/, 1)[0], 10);
  if (!Number.isInteger(port) || port <= 0) {
    throw new Error(`Chrome did not expose a DevTools port for ${userDataDirectory}`);
  }
  return port;
}

async function installOperatorSession(port, scope, session, destinationUrl) {
  const credentialPath = process.env[session.credentialFileEnvironment];
  const info = lstatSync(credentialPath);
  if (!info.isFile() || (info.mode & 0o077) !== 0) {
    throw new Error('Operator credential file must be a protected regular file (0600).');
  }
  const credential = JSON.parse(await readFile(credentialPath, 'utf8'));
  if (typeof credential.email !== 'string' || typeof credential.password !== 'string') {
    throw new Error('Operator credential file is incomplete.');
  }
  const prefix = `myscoutee.${scope}.operator-bootstrap`;
  const result = await evaluateInOnlyPage(port, `(async () => {
    const response = await fetch('/api/auth/operator-bootstrap', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(${JSON.stringify({email: credential.email, password: credential.password})})
    });
    if (!response.ok) return { status: response.status, accepted: false };
    const body = await response.json();
    if (body.tokenType !== 'OperatorBootstrap' || !body.accessToken
        || Date.parse(body.expiresAt) <= Date.now()) return { accepted: false };
    sessionStorage.setItem(${JSON.stringify(prefix + '.session.v1')}, JSON.stringify({
      kind: 'operator-bootstrap', email: body.email, expiresAt: body.expiresAt
    }));
    sessionStorage.setItem(${JSON.stringify(prefix + '.token.v1')}, body.accessToken);
    return { accepted: true };
  })()`);
  if (!result?.accepted) throw new Error(`Operator startup login failed (HTTP ${result?.status ?? 0}).`);
  await evaluateInOnlyPage(port, `window.location.replace(${JSON.stringify(destinationUrl)})`, true);
  await waitUntil(async () => {
    try {
      return await evaluateInOnlyPage(port, `location.pathname === '/operator'
        && !!document.querySelector('app-operator-page')
        && !!sessionStorage.getItem(${JSON.stringify(prefix + '.token.v1')})`);
    } catch { return false; }
  }, 30000, 'Operator page ready');
}

async function registerTrackedDemoSession(port, userId) {
  const sessionId = `session:${randomUUID()}`;
  const attemptId = `attempt:${randomUUID()}`;
  const result = await evaluateInOnlyPage(port, `(async () => {
    try {
      const response = await fetch('/api/auth/session/login', {
        method: 'POST',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          'X-App-Session-Id': ${JSON.stringify(sessionId)},
          'X-App-Session-Kind': 'demo',
          'X-Demo-User-Id': ${JSON.stringify(userId)}
        },
        body: JSON.stringify({
          attemptId: ${JSON.stringify(attemptId)},
          provider: 'demo'
        })
      });
      const body = await response.json().catch(() => null);
      return {
        status: response.status,
        accepted: body?.accepted === true,
        outcome: typeof body?.outcome === 'string' ? body.outcome : '',
        message: typeof body?.message === 'string' ? body.message : ''
      };
    } catch (error) {
      return {
        status: 0,
        accepted: false,
        outcome: '',
        message: error instanceof Error ? error.message : String(error)
      };
    }
  })()`);
  if (result?.status !== 200 || result?.accepted !== true) {
    const detail = result?.outcome || result?.message || 'no response body';
    throw new Error(
      `Tracked demo login for ${userId} returned HTTP ${result?.status ?? 0} (${detail}).`
    );
  }
  return sessionId;
}

async function revokeTrackedDemoSession(session) {
  const sessionId = `${session?.sessionId ?? ''}`.trim();
  const userId = `${session?.userId ?? ''}`.trim();
  const sessionOrigin = `${session?.sessionOrigin ?? ''}`.trim();
  if (`${session?.sessionKind ?? ''}` !== 'demo' || !sessionId || !userId || !sessionOrigin) {
    return;
  }
  const response = await fetch(new URL('/api/auth/me/logout', sessionOrigin), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-App-Session-Id': sessionId,
      'X-App-Session-Kind': 'demo',
      'X-Demo-User-Id': userId
    },
    body: JSON.stringify({ userId })
  });
  if (response.status === 401) {
    return;
  }
  if (!response.ok) {
    throw new Error(
      `Unable to revoke the previous managed demo session for ${userId}: HTTP ${response.status}.`
    );
  }
}

async function installDemoSession(port, scope, userId, sessionId, destinationUrl, configuredLocalStorage) {
  const sessionKey = `myscoutee.${scope}.session.v1`;
  const activeUserKey = `myscoutee.${scope}.demo.active-user.v1`;
  const sessionValue = JSON.stringify({ kind: 'demo', userId, sessionId });
  const localStorageEntries = normalizeLocalStorageEntries(configuredLocalStorage);
  const expression = `(() => {
    for (const [key, value] of ${JSON.stringify(localStorageEntries)}) {
      localStorage.setItem(key, value);
    }
    localStorage.setItem(${JSON.stringify(sessionKey)}, ${JSON.stringify(sessionValue)});
    localStorage.setItem(${JSON.stringify(activeUserKey)}, ${JSON.stringify(userId)});
    window.location.replace(${JSON.stringify(destinationUrl)});
    return true;
  })()`;
  await evaluateInOnlyPage(port, expression, true);
}

async function clearManagedSession(port, scope, configuredLocalStorage) {
  const sessionKey = `myscoutee.${scope}.session.v1`;
  const activeUserKey = `myscoutee.${scope}.demo.active-user.v1`;
  const localStorageEntries = normalizeLocalStorageEntries(configuredLocalStorage);
  await evaluateInOnlyPage(port, `(() => {
    for (const [key, value] of ${JSON.stringify(localStorageEntries)}) {
      localStorage.setItem(key, value);
    }
    localStorage.removeItem(${JSON.stringify(sessionKey)});
    localStorage.removeItem(${JSON.stringify(activeUserKey)});
    return true;
  })()`);
}

async function prepareBrowserLocation(port, entryPageUrl, location, permission, userDataDirectory) {
  const origin = new URL(entryPageUrl).origin;
  const browser = await jsonRequest(`http://127.0.0.1:${port}/json/version`);
  if (browser.webSocketDebuggerUrl) {
    for (const allowedOrigin of new Set([origin, origin.replace('http:', 'https:')])) {
      if (permission === 'granted') {
        await cdpCommand(browser.webSocketDebuggerUrl, 'Browser.grantPermissions', {
          origin: allowedOrigin,
          permissions: ['geolocation']
        });
      } else {
        await cdpCommand(browser.webSocketDebuggerUrl, 'Browser.setPermission', {
          origin: allowedOrigin,
          permission: { name: 'geolocation' },
          setting: permission
        });
      }
    }
  }
  if (!location) {
    const initialPage = await waitForAnyPageTarget(port);
    await cdpCommand(initialPage.webSocketDebuggerUrl, 'Page.navigate', { url: entryPageUrl });
    await waitForAppPageTarget(port);
    return null;
  }

  // Geolocation emulation is scoped to the CDP session. A one-shot command is
  // reverted when that DevTools connection closes, so a tiny holder keeps the
  // target session alive for exactly as long as this managed Chrome process.
  const readyFile = path.join(userDataDirectory, 'GeolocationHolderReady.json');
  await rm(readyFile, { force: true });
  const holder = spawn(process.execPath, [
    geolocationHolderPath,
    '--port', `${port}`,
    '--latitude', `${Number(location.latitude)}`,
    '--longitude', `${Number(location.longitude)}`,
    '--accuracy', `${Number(location.accuracy ?? 10)}`,
    '--permission', permission,
    '--url', entryPageUrl,
    '--ready-file', readyFile
  ], {
    detached: true,
    stdio: 'ignore'
  });
  holder.unref();

  let holderState = null;
  await waitUntil(() => {
    try {
      holderState = JSON.parse(readFileSync(readyFile, 'utf8'));
      return holderState?.status === 'ready' || holderState?.status === 'error';
    } catch {
      return false;
    }
  }, 10000, `Chrome geolocation holder on DevTools port ${port}`);
  if (holderState?.status !== 'ready') {
    throw new Error(holderState?.message || `Chrome geolocation holder failed on port ${port}.`);
  }
  await waitForAppPageTarget(port);
  return holder.pid;
}

async function acceptEntryConsent(port, scope, destinationUrl) {
  const consentKey = `myscoutee.${scope}.entry.gdpr-consent.v1`;
  const destinationPath = new URL(destinationUrl).pathname;
  await waitUntil(async () => {
    try {
      return await evaluateInOnlyPage(port, `(() => {
        if (window.location.pathname === ${JSON.stringify(destinationPath)}) return true;
        const raw = localStorage.getItem(${JSON.stringify(consentKey)});
        const consent = raw ? JSON.parse(raw) : null;
        if (consent?.accepted === true) return true;
        const footer = document.querySelector('.document-viewer-actions');
        if (!footer) return false;
        const buttons = Array.from(footer.querySelectorAll('button:not([disabled])'));
        const accept = buttons.at(-1);
        if (!accept) return false;
        accept.click();
        return true;
      })()`);
    } catch {
      return false;
    }
  }, 30000, `entry consent action on DevTools port ${port}`);
  const alreadyAtDestination = await evaluateInOnlyPage(
    port,
    `window.location.pathname === ${JSON.stringify(destinationPath)}`
  );
  if (alreadyAtDestination) {
    return;
  }
  await waitUntil(async () => {
    try {
      return await evaluateInOnlyPage(port, `(() => {
        const raw = localStorage.getItem(${JSON.stringify(consentKey)});
        const consent = raw ? JSON.parse(raw) : null;
        return consent?.accepted === true;
      })()`);
    } catch {
      return false;
    }
  }, 10000, `persisted entry consent on DevTools port ${port}`);
  await evaluateInOnlyPage(
    port,
    `window.location.replace(${JSON.stringify(destinationUrl)})`,
    true
  );
}

async function acceptEntryConsentOnCurrentEntry(port, scope) {
  const consentKey = `myscoutee.${scope}.entry.gdpr-consent.v1`;
  await waitUntil(async () => {
    try {
      return await evaluateInOnlyPage(port, `(() => {
        const raw = localStorage.getItem(${JSON.stringify(consentKey)});
        const consent = raw ? JSON.parse(raw) : null;
        if (consent?.accepted === true) return true;
        const footer = document.querySelector('.document-viewer-actions');
        if (!footer) return false;
        const buttons = Array.from(footer.querySelectorAll('button:not([disabled])'));
        const accept = buttons.at(-1);
        if (!accept) return false;
        accept.click();
        return false;
      })()`);
    } catch {
      return false;
    }
  }, 30000, `persisted entry consent on DevTools port ${port}`);
}

async function dismissExplanationGuide(port) {
  await waitUntil(async () => {
    try {
      return await evaluateInOnlyPage(port, `(() => {
        const guide = document.querySelector('app-explanation-popup .ui-popup');
        if (!guide) return true;
        const button = guide.querySelector('button.ui-popup__close:not([disabled])');
        if (!button) return false;
        button.click();
        return false;
      })()`);
    } catch {
      return false;
    }
  }, 15000, `explanation guide dismissal on DevTools port ${port}`);
}

async function waitForConfiguredSession(port, scope, userId, sessionId, destinationUrl) {
  const sessionKey = `myscoutee.${scope}.session.v1`;
  const expectedPath = new URL(destinationUrl).pathname;
  await waitUntil(async () => {
    let value;
    try {
      value = await evaluateInOnlyPage(port, `(async () => {
        const raw = localStorage.getItem(${JSON.stringify(sessionKey)});
        const session = raw ? JSON.parse(raw) : null;
        let authentication = { status: 0, userId: '', belongsToAccount: false };
        let realtime = { status: 0, userId: '', hasCounters: false };
        try {
          const response = await fetch(
            '/api/auth/me?userId=' + encodeURIComponent(${JSON.stringify(userId)}),
            {
              cache: 'no-store',
              headers: {
                'X-App-Session-Id': ${JSON.stringify(sessionId)},
                'X-App-Session-Kind': 'demo',
                'X-Demo-User-Id': ${JSON.stringify(userId)}
              }
            }
          );
          const body = await response.json().catch(() => null);
          authentication = {
            status: response.status,
            userId: typeof body?.id === 'string' ? body.id : '',
            belongsToAccount: body?.id === ${JSON.stringify(userId)}
          };
          // /auth/me resolves the retained workspace profile. Verify its account
          // membership through the account-scoped workspace list, without
          // changing the user's saved selection during readiness checks.
          if (response.ok && authentication.userId && !authentication.belongsToAccount) {
            const workspaceResponse = await fetch('/api/groups/workspaces?userId=' + encodeURIComponent(${JSON.stringify(userId)}), {
              cache: 'no-store',
              headers: {
                'X-App-Session-Id': ${JSON.stringify(sessionId)},
                'X-App-Session-Kind': 'demo',
                'X-Demo-User-Id': ${JSON.stringify(userId)}
              }
            });
            const workspaces = await workspaceResponse.json().catch(() => null);
            authentication.belongsToAccount = workspaceResponse.ok && Array.isArray(workspaces)
              && workspaces.some(workspace => workspace.profileId === authentication.userId
                && workspace.membershipStatus === 'accepted');
          }
          const realtimeResponse = await fetch('/api/auth/me/realtime/long-poll', {
            method: 'POST',
            cache: 'no-store',
            headers: {
              'Content-Type': 'application/json',
              'X-App-Session-Id': ${JSON.stringify(sessionId)},
              'X-App-Session-Kind': 'demo',
              'X-Demo-User-Id': ${JSON.stringify(userId)}
            },
            body: JSON.stringify({ userId: ${JSON.stringify(userId)}, cursor: null })
          });
          const realtimeBody = await realtimeResponse.json().catch(() => null);
          realtime = {
            status: realtimeResponse.status,
            userId: typeof realtimeBody?.userId === 'string' ? realtimeBody.userId : '',
            hasCounters: Boolean(realtimeBody?.counters)
          };
        } catch {
          // Keep polling while the application/backend route is still starting.
        }
        return {
          userId: session?.kind === 'demo' ? session.userId : '',
          sessionId: session?.kind === 'demo' ? session.sessionId : '',
          authStatus: authentication.status,
          authenticatedUserId: authentication.userId,
          authenticatedAccountMatches: authentication.belongsToAccount,
          realtimeStatus: realtime.status,
          realtimeUserId: realtime.userId,
          realtimeHasCounters: realtime.hasCounters,
          pathname: window.location.pathname
        };
      })()`);
    } catch {
      return false;
    }
    if ([401, 403].includes(value?.authStatus) || [401, 403].includes(value?.realtimeStatus)) {
      throw new Error(
        `Authenticated readiness for ${userId} returned auth HTTP ${value?.authStatus ?? 0} and realtime HTTP ${value?.realtimeStatus ?? 0}.`
      );
    }
    return value?.userId === userId
      && value?.sessionId === sessionId
      && value?.authStatus === 200
      && value?.authenticatedAccountMatches === true
      && value?.realtimeStatus === 200
      && value?.realtimeUserId === value?.authenticatedUserId
      && value?.realtimeHasCounters === true
      && value?.pathname === expectedPath;
  }, 30000, `MyScoutee session ${userId} on DevTools port ${port}`);
}

async function waitForAnonymousEntry(port, scope, entryPageUrl) {
  const sessionKey = `myscoutee.${scope}.session.v1`;
  const expectedPath = new URL(entryPageUrl).pathname;
  await waitUntil(async () => {
    try {
      const value = await evaluateInOnlyPage(port, `({
        hasSession: Boolean(localStorage.getItem(${JSON.stringify(sessionKey)})),
        pathname: window.location.pathname
      })`);
      return value?.hasSession === false && value?.pathname === expectedPath;
    } catch {
      return false;
    }
  }, 30000, `anonymous MyScoutee entry on DevTools port ${port}`);
}

async function evaluateInOnlyPage(port, expression, tolerateNavigation = false) {
  const page = await waitForAppPageTarget(port);
  try {
    return await cdpEvaluate(page.webSocketDebuggerUrl, expression);
  } catch (error) {
    if (tolerateNavigation) {
      return undefined;
    }
    throw error;
  }
}

async function waitForAppPageTarget(port) {
  let page;
  await waitUntil(async () => {
    try {
      const targets = await jsonRequest(`http://127.0.0.1:${port}/json/list`);
      page = targets.find(target =>
        target.type === 'page'
        && target.webSocketDebuggerUrl
        && configuredAppOrigins.has(new URL(target.url).origin)
      );
      return Boolean(page);
    } catch {
      return false;
    }
  }, 20000, `MyScoutee page target on DevTools port ${port}`);
  return page;
}

async function waitForAnyPageTarget(port) {
  let page;
  await waitUntil(async () => {
    try {
      const targets = await jsonRequest(`http://127.0.0.1:${port}/json/list`);
      page = targets.find(target => target.type === 'page' && target.webSocketDebuggerUrl);
      return Boolean(page);
    } catch {
      return false;
    }
  }, 20000, `page target on DevTools port ${port}`);
  return page;
}

async function cdpEvaluate(webSocketUrl, expression) {
  const message = await cdpCommand(webSocketUrl, 'Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true
  });
  return message.result?.result?.value;
}

async function cdpCommand(webSocketUrl, method, params = {}) {
  return await new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketUrl);
    const requestId = 1;
    const timeout = setTimeout(() => {
      socket.close();
      reject(new Error('Chrome Runtime.evaluate timed out'));
    }, 5000);
    socket.addEventListener('open', () => {
      socket.send(JSON.stringify({
        id: requestId,
        method,
        params
      }));
    });
    socket.addEventListener('message', event => {
      const message = JSON.parse(`${event.data}`);
      if (message.id !== requestId) {
        return;
      }
      clearTimeout(timeout);
      socket.close();
      if (message.error || message.result?.exceptionDetails) {
        reject(new Error(message.error?.message || 'Chrome Runtime.evaluate failed'));
        return;
      }
      resolve(message);
    });
    socket.addEventListener('error', () => {
      clearTimeout(timeout);
      reject(new Error('Unable to connect to Chrome DevTools'));
    });
  });
}

async function jsonRequest(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${url} returned HTTP ${response.status}`);
  }
  return await response.json();
}

async function waitUntil(predicate, timeoutMs, description = 'condition') {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) {
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${description} after ${timeoutMs} ms`);
}

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}
