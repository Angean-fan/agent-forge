import assert from 'node:assert/strict';
import { test } from 'node:test';

test('node toolchain smoke test', () => {
  assert.equal(1 + 1, 2);
});

test('platform-ui package is private and typed as module', async () => {
  const pkg = await import('../package.json', { with: { type: 'json' } });
  assert.equal(pkg.default.private, true);
  assert.equal(pkg.default.type, 'module');
});
