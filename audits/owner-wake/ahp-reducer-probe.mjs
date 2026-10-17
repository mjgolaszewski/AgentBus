/** Offline qualification of pinned upstream state semantics; no host transport. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, relative, dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { stripTypeScriptTypes } from 'node:module';

const [sourceArg, manifestArg] = process.argv.slice(2);
if (!sourceArg || !manifestArg) throw new Error('usage: node ahp-reducer-probe.mjs SOURCE_ROOT PIN_MANIFEST');
const source = resolve(sourceArg);
const pins = JSON.parse(readFileSync(manifestArg, 'utf8'));
assert.equal(pins.commit, '1ef0642965efaa7ef98caff287bca923fbc80ec6');
const actual = {};
function readPinned(name) {
  assert(!relative(source, resolve(source, name)).startsWith('..'), 'source must stay within root');
  const bytes = readFileSync(resolve(source, name));
  const digest = createHash('sha256').update(bytes).digest('hex');
  assert.equal(digest, pins.files[name], `source pin mismatch: ${name}`);
  actual[name] = digest;
  return bytes.toString('utf8');
}
const output = mkdtempSync(join(tmpdir(), 'agentbus-ahp-reducer-'));
try {
  writeFileSync(join(output, 'package.json'), '{"type":"module"}\n');
  const compiled = new Set();
  function compile(name) {
    if (compiled.has(name)) return;
    compiled.add(name);
    const js = stripTypeScriptTypes(readPinned(name), { mode: 'transform' });
    for (const match of js.matchAll(/^\s*(?:import|export)\s+[^;]*?\bfrom\s+['"]([^'"]+)['"]/gm)) {
      assert(match[1].startsWith('.'), `unexpected external import ${match[1]}`);
      compile(relative(source, resolve(source, dirname(name), match[1].replace(/\.js$/, '.ts'))));
    }
    const destination = join(output, name.replace(/\.ts$/, '.js'));
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, js);
  }
  compile('types/channels-chat/reducer.ts');
  const { chatReducer } = await import(pathToFileURL(join(output, 'types/channels-chat/reducer.js')).href);
  readPinned('types/reducers.test.ts');
  // Match the pinned upstream fixture loader's null-to-undefined semantics.
  function nullToUndefined(value) {
    if (value === null) return undefined;
    if (Array.isArray(value)) return value.map(nullToUndefined);
    if (typeof value === 'object' && value !== null) {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, nullToUndefined(item)]));
    }
    return value;
  }
  const fixtures = 'types/test-cases/reducers';
  let cases = 0;
  for (const name of readdirSync(join(source, fixtures)).sort()) {
    if (!name.endsWith('.json')) continue;
    const fixtureName = `${fixtures}/${name}`;
    // All fixtures are pinned, including other reducer channels, to bind selection.
    const fixture = nullToUndefined(JSON.parse(readPinned(fixtureName)));
    if (fixture.reducer !== 'chat') continue;
    const result = fixture.actions.reduce((state, action) => chatReducer(state, action), structuredClone(fixture.initial));
    assert.deepEqual(result, fixture.expected, fixture.description);
    cases++;
  }
  assert.equal(cases, 168, 'pinned upstream chat fixture population must remain complete');
  const message = { text: 'AgentBus stable references only', origin: { kind: 'user' } };
  const empty = { resource: 'ahp-chat:/disposable', title: 'offline', turns: [], status: 1 };
  const set = (id, kind) => ({ type: 'chat/pendingMessageSet', id, kind, message });
  let queued = chatReducer(empty, set('attempt-1', 'queued'));
  queued = chatReducer(queued, set('attempt-1', 'queued'));
  assert.equal(queued.queuedMessages.length, 1, 'same pending ID upserts');
  assert.equal(queued.activeTurn, undefined, 'reducer custody does not execute');
  assert.equal(queued.steeringMessage, undefined, 'queued is not steering');
  const active = { ...queued, activeTurn: { id: 'user-turn' } };
  const raced = chatReducer(active, set('attempt-2', 'queued'));
  assert.equal(raced.activeTurn.id, 'user-turn');
  assert.equal(raced.queuedMessages.length, 2, 'busy reducer still retains queued input');
  const wrongKind = chatReducer(queued, { type: 'chat/pendingMessageRemoved', id: 'attempt-1', kind: 'steering' });
  assert.equal(wrongKind.queuedMessages.length, 1, 'kind mismatch cannot remove queue');
  const consumed = chatReducer(queued, { type: 'chat/turnStarted', turnId: 'wake-turn', startedAt: '2026-10-17T00:00:00Z', message, queuedMessageId: 'attempt-1' });
  assert.equal(consumed.queuedMessages?.length ?? 0, 0);
  const replay = chatReducer(consumed, set('attempt-1', 'queued'));
  assert.equal(replay.queuedMessages.length, 1, 'consumed ID is not a durable deduplication tombstone');
  const steering = chatReducer(queued, set('attempt-1', 'steering'));
  assert.equal(steering.steeringMessage.id, 'attempt-1', 'same ID can change kind; adapter must pin queued');
  const back = chatReducer(steering, set('attempt-1', 'queued'));
  assert.equal(back.steeringMessage, undefined);
  assert.deepEqual(Object.keys(actual).sort(), Object.keys(pins.files).sort(), 'manifest population must exactly match');
  console.log(JSON.stringify({ status: 'pass', scope: 'upstream_pure_reducer_only', commit: pins.commit, node: process.version, upstream_chat_cases: cases, targeted_cases: 8, source_files: actual, host_connections: 0, model_turns: 0, atomic_admission_proved: false, provider_interoperability_proved: false }, null, 2));
} finally {
  rmSync(output, { recursive: true, force: true });
}
