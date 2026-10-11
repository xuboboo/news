import { draftRequest, type DraftItem, type DraftListPage, type DraftSourceType, type DraftTier, type DraftStatus } from './drafts';

export interface LongtailSuggestion {
  keyword: string;
  intent: string;
  form: string;
  title: string;
}

export interface SiteDraftItem extends DraftItem {
  channel: 'site';
  seo_keywords: string;
  site_tags: string;
  cover_guidance: string;
  software_id: number;
  is_pinned: boolean;
  is_recommended: boolean;
  is_headline: boolean;
  publish_at: string | null;
  longtail_suggestions?: LongtailSuggestion[];
}

export interface SiteDraftListPage extends Omit<DraftListPage, 'items'> {
  items: SiteDraftItem[];
}

export type SiteDraftPatch = {
  revision: number;
  title?: string;
  digest?: string;
  seo_keywords?: string;
  site_tags?: string;
  cover_guidance?: string;
  software_id?: number;
  is_pinned?: boolean;
  is_recommended?: boolean;
  is_headline?: boolean;
  publish_at?: string | null;
  content_md?: string;
};

export const createSiteDraft = (sourceType: DraftSourceType, sourceId: string | number, tier: DraftTier) => {
  const raw = sourceType === 'event' && typeof sourceId === 'string' && sourceId.startsWith('e')
    ? sourceId.slice(1) : sourceId;
  const id = Number(raw);
  if (!Number.isSafeInteger(id) || id <= 0 || String(raw).trim() === '')
    return Promise.reject(new Error('选题编号无效，请刷新页面重试'));
  return draftRequest<SiteDraftItem>('/site-drafts', {
    method: 'POST', body: JSON.stringify({ source_type: sourceType, source_id: id, tier }),
  });
};

export const listSiteDrafts = (status: DraftStatus | '', limit = 50, offset = 0, beforeId?: number) => {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) qs.set('status', status);
  if (beforeId !== undefined) qs.set('before_id', String(beforeId));
  return draftRequest<SiteDraftListPage>(`/site-drafts?${qs.toString()}`);
};

export const getSiteDraft = (id: number) => draftRequest<SiteDraftItem>(`/site-drafts/${id}`);
export const updateSiteDraft = (id: number, patch: SiteDraftPatch) => draftRequest<SiteDraftItem>(`/site-drafts/${id}`, {
  method: 'PUT', body: JSON.stringify(patch),
});
export const regenerateSiteDraft = (id: number, revision: number) => draftRequest<SiteDraftItem>(`/site-drafts/${id}/regenerate`, {
  method: 'POST', body: JSON.stringify({ revision }),
});
export const deleteSiteDraft = (id: number, revision: number) => draftRequest<{ deleted: boolean }>(`/site-drafts/${id}`, {
  method: 'DELETE', body: JSON.stringify({ revision }),
});
