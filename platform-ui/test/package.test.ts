import { describe, expect, it } from 'vitest';

import pkg from '../package.json';

const EXACT_VERSION = /^\d+\.\d+\.\d+$/;

describe('platform-ui package contract', () => {
  it('stays private and ESM', () => {
    expect(pkg.private).toBe(true);
    expect(pkg.type).toBe('module');
  });

  it('pins every direct dependency to an exact version (no ^ or ~)', () => {
    const direct = { ...pkg.dependencies, ...pkg.devDependencies };
    expect(Object.keys(direct).length).toBeGreaterThan(0);
    for (const [name, version] of Object.entries(direct)) {
      expect(version, `${name} must be pinned`).toMatch(EXACT_VERSION);
    }
  });
});
