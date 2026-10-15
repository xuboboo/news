import { describe, expect, it } from 'vitest';
import { safeReturnPath } from './returnPath';

describe('safeReturnPath', () => {
  it.each([
    '/',
    '/hot',
    '/latest',
    '/events/42',
    '/drafts/17',
    '/site-drafts/8',
    '/wx-layouts/3',
    '/account',
    '/admin',
  ])('accepts an internal route: %s', (path) => {
    expect(safeReturnPath(path)).toBe(path);
  });

  it.each([
    undefined,
    null,
    'https://attacker.example',
    '//attacker.example',
    '/\\\\attacker.example',
    '/%5c%5cattacker.example',
    '/not-a-route',
    '/events/not-an-id',
  ])('falls back for an untrusted return target: %s', (path) => {
    expect(safeReturnPath(path)).toBe('/hot');
  });

  it('supports a caller-provided safe fallback', () => {
    expect(safeReturnPath('//attacker.example', '/account')).toBe('/account');
  });
});
