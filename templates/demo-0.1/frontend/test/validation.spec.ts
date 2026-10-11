import { describe, expect, it } from 'vitest';

import { canSubmitLogin, isNonEmpty } from '../src/utils/validation';

describe('login validation', () => {
  it('accepts a non-empty username and password of at least 6 chars', () => {
    expect(canSubmitLogin('admin', 'admin123')).toBe(true);
  });

  it('rejects a blank username', () => {
    expect(canSubmitLogin('   ', 'admin123')).toBe(false);
  });

  it('rejects a short password', () => {
    expect(canSubmitLogin('admin', '123')).toBe(false);
  });

  it('trims whitespace when checking emptiness', () => {
    expect(isNonEmpty('  ')).toBe(false);
    expect(isNonEmpty(' a ')).toBe(true);
  });
});
