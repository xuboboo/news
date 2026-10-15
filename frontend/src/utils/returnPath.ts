/**
 * Only return to routes that exist in this SPA. The value comes from browser
 * history state and can therefore be attacker-controlled (for example through
 * a crafted hash URL before the user signs in).
 */
const INTERNAL_RETURN_PATHS = [
  /^\/$/,
  /^\/(?:hot|latest|events|report|drafts|site-drafts|wx-layouts|account|users|audit|usage|admin)\/?$/,
  /^\/(?:events|drafts|site-drafts|wx-layouts)\/\d+\/?$/,
];

export function safeReturnPath(value: unknown, fallback = '/hot'): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) {
    return fallback;
  }
  return INTERNAL_RETURN_PATHS.some((pattern) => pattern.test(value)) ? value : fallback;
}
