// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createSiteDraft, deleteSiteDraft, listSiteDrafts, updateSiteDraft } from './siteDrafts';

beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Response('{}', { status: 200 }))); });
afterEach(() => { vi.unstubAllGlobals(); });
it('uses independent site endpoints, normalizes event IDs and sends revision', async () => {
  await createSiteDraft('event', 'e3', 'depth');
  expect(fetch).toHaveBeenCalledWith('/api/v2/site-drafts', expect.objectContaining({ body: JSON.stringify({ source_type: 'event', source_id: 3, tier: 'depth' }) }));
  await listSiteDrafts('ready', 50, 0, 99);
  expect(fetch).toHaveBeenCalledWith('/api/v2/site-drafts?limit=50&offset=0&status=ready&before_id=99', expect.anything());
  await updateSiteDraft(7, { revision: 3, seo_keywords: '软件,更新' });
  expect(fetch).toHaveBeenCalledWith('/api/v2/site-drafts/7', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ revision: 3, seo_keywords: '软件,更新' }) }));
  await deleteSiteDraft(7, 4);
  expect(fetch).toHaveBeenCalledWith('/api/v2/site-drafts/7', expect.objectContaining({ method: 'DELETE', body: '{"revision":4}' }));
});
it('rejects invalid source IDs without a request', async () => {
  await expect(createSiteDraft('article', 1.9, 'brief')).rejects.toThrow('选题编号无效');
  expect(fetch).not.toHaveBeenCalled();
});
