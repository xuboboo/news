// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Drafts from './Drafts';
import type { DraftItem, DraftListPage, DraftStatus } from '../api/drafts';

const api = vi.hoisted(() => ({ listDrafts: vi.fn(), deleteDraft: vi.fn() }));
const siteApi = vi.hoisted(() => ({ listSiteDrafts: vi.fn(), deleteSiteDraft: vi.fn() }));
vi.mock('../api/drafts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/drafts')>()),
  listDrafts: api.listDrafts,
  deleteDraft: api.deleteDraft,
}));
vi.mock('../api/siteDrafts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/siteDrafts')>()),
  ...siteApi,
}));
vi.mock('../utils/hooks', () => ({
  activateOnKey: (fn: () => void) => fn,
  usePageTitle: vi.fn(),
}));
vi.mock('../components/common', () => ({
  Loading: () => <div>loading</div>,
  PageHeader: ({ title, children }: { title: string; children?: React.ReactNode }) => <header><h1>{title}</h1>{children}</header>,
}));

const draft = (id: number, title: string, status: DraftStatus = 'ready'): DraftItem => ({
  id, status, source_type: 'article', source_id: id, source_title: `source-${id}`, tier: 'brief',
  title, alt_titles: [], digest: '', model: '', prompt_tokens: 0, completion_tokens: 0,
  duration_ms: 0, error: '', gen_count: 1, revision: 1, source_refs: [], created_by: null,
  created_at: null, updated_at: null,
});
const page = (items: DraftItem[], total: number): DraftListPage => ({ items, total });

function mount(channel: 'wechat' | 'site' = 'wechat') {
  return render(<MemoryRouter><Drafts channel={channel} /></MemoryRouter>);
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.deleteDraft.mockResolvedValue({ deleted: true });
});

describe('Drafts regression scenarios', () => {
  it('uses the independent website list endpoint and page copy', async () => {
    const siteDraft = { ...draft(701, '网站稿件'), channel: 'site' as const, seo_keywords: '软件,更新', site_tags: '软件', cover_guidance: '方图', software_id: 0, is_pinned: false, is_recommended: false, is_headline: false, publish_at: null };
    siteApi.listSiteDrafts.mockResolvedValue({ items: [siteDraft], total: 1 });
    mount('site');
    expect(await screen.findByRole('heading', { name: '网站资讯稿' })).toBeTruthy();
    expect(await screen.findByRole('button', { name: '查看草稿「网站稿件」' })).toBeTruthy();
    expect(siteApi.listSiteDrafts).toHaveBeenCalledWith('', 50, 0);
    expect(api.listDrafts).not.toHaveBeenCalled();
  });

  it('announces site-draft list failures to assistive technology', async () => {
    siteApi.listSiteDrafts.mockRejectedValue(new Error('网站草稿列表暂不可用'));
    mount('site');
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '网站草稿列表暂不可用');
  });

  it('exposes filter selection, status and summary to assistive technology', async () => {
    api.listDrafts.mockResolvedValue(page([{ ...draft(9, '状态稿件'), digest: '摘要说明' }], 1));
    mount();
    const open = await screen.findByRole('button', { name: '查看草稿「状态稿件」' });
    const filter = screen.getByRole('button', { name: '全部' });
    const described = open.getAttribute('aria-describedby')?.split(/\s+/).map((id) => document.getElementById(id)?.textContent).join(' ') ?? '';
    expect(filter.getAttribute('aria-pressed')).toBe('true');
    expect(described).toContain('就绪');
    expect(described).toContain('摘要说明');
  });

  it('describes an empty status filter without claiming all drafts are missing', async () => {
    api.listDrafts.mockResolvedValue(page([], 0));
    mount();
    await screen.findByText('还没有文章草稿');
    fireEvent.click(screen.getByRole('button', { name: '失败' }));
    await waitFor(() => expect(api.listDrafts).toHaveBeenCalledWith('failed', 50, 0));
    await waitFor(() => expect(screen.queryByText('loading')).toBeNull());
    expect(screen.queryByText('还没有文章草稿')).toBeNull();
    expect(screen.getByText('暂无失败的草稿')).toBeTruthy();
  });
  it('keeps a new filter request alive when an earlier delete completes', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let resolveDelete!: (value: { deleted: boolean }) => void;
    let resolveReady!: (value: DraftListPage) => void;
    api.deleteDraft.mockReturnValue(new Promise((resolve) => { resolveDelete = resolve; }));
    api.listDrafts.mockImplementation((status: DraftStatus | '') => status === 'ready'
      ? new Promise((resolve) => { resolveReady = resolve; })
      : Promise.resolve(page([draft(9, '待删除')], 1)));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '删除草稿「待删除」' }));
    fireEvent.click(screen.getByRole('button', { name: '就绪' }));
    await waitFor(() => expect(api.listDrafts).toHaveBeenCalledWith('ready', 50, 0));
    await act(async () => { resolveDelete({ deleted: true }); });
    await act(async () => { resolveReady(page([draft(8, '新筛选结果')], 1)); });
    expect(screen.queryByText('loading')).toBeNull();
    expect(screen.getByRole('button', { name: '查看草稿「新筛选结果」' })).toBeTruthy();
    confirm.mockRestore();
  });

  it.each(['', 'generating'] as const)('refreshes older loaded drafts for the %s filter', async (status) => {
    const poll = vi.spyOn(window, 'setInterval');
    const first = Array.from({ length: 50 }, (_, i) => draft(100 - i, `row-${100 - i}`, status || 'ready'));
    let finished = false;
    api.listDrafts.mockImplementation((_status: DraftStatus | '', _limit: number, _offset: number, beforeId?: number) => {
      if (beforeId !== undefined) return Promise.resolve(page(finished
        ? status ? [] : [draft(50, '较早稿件', 'ready')]
        : [draft(50, '较早稿件', 'generating')], finished && status ? 50 : 51));
      return Promise.resolve(page(first, finished && status ? 50 : 51));
    });
    mount();
    await screen.findByRole('button', { name: /加载更早/ });
    if (status) {
      fireEvent.click(screen.getByRole('button', { name: '生成中' }));
      await screen.findByRole('button', { name: /加载更早/ });
    }
    fireEvent.click(screen.getByRole('button', { name: /加载更早/ }));
    await screen.findByRole('button', { name: '查看草稿「较早稿件」' });
    finished = true;
    await act(async () => { (poll.mock.calls[poll.mock.calls.length - 1]?.[0] as () => void)(); });
    poll.mockRestore();
    if (status) {
      expect(screen.queryByRole('button', { name: '查看草稿「较早稿件」' })).toBeNull();
    } else {
      const card = screen.getByRole('button', { name: '查看草稿「较早稿件」' });
      expect(card.textContent).toContain('就绪');
      expect(card.textContent).not.toContain('正在基于');
    }
  });
});

describe('Drafts cursor pagination and stale-response protection', () => {
  it('deletes generating and failed drafts directly from their cards', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const generating = draft(9, '生成中的稿件', 'generating');
    const failed = draft(8, '失败的稿件', 'failed');
    let remaining = [generating, failed];
    api.listDrafts.mockImplementation(() => Promise.resolve(page(remaining, remaining.length)));
    api.deleteDraft.mockImplementation(async (id: number) => {
      remaining = remaining.filter((item) => item.id !== id);
      return { deleted: true };
    });
    mount();

    const deleteGenerating = await screen.findByRole('button', { name: '删除草稿「生成中的稿件」' });
    fireEvent.click(deleteGenerating);
    await waitFor(() => expect(api.deleteDraft).toHaveBeenCalledWith(9, 1));
    expect(screen.queryByRole('button', { name: '查看草稿「生成中的稿件」' })).toBeNull();
    expect(screen.getByRole('button', { name: '查看草稿「失败的稿件」' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '删除草稿「失败的稿件」' }));
    await waitFor(() => expect(api.deleteDraft).toHaveBeenCalledWith(8, 1));
    expect(screen.queryByRole('button', { name: '查看草稿「失败的稿件」' })).toBeNull();
    expect(confirm).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });

  it('uses an ID cursor and ignores an older-page response after the filter changes', async () => {
    const firstPage = Array.from({ length: 50 }, (_, i) => draft(100 - i, `row-${100 - i}`));
    let resolveOlder!: (value: DraftListPage) => void;
    api.listDrafts.mockImplementation((status: DraftStatus | '', _limit: number, _offset: number, beforeId?: number) => {
      if (status === '' && beforeId === undefined && api.listDrafts.mock.calls.filter((call) => call[0] === '').length === 1) {
        return Promise.resolve(page(firstPage, 51));
      }
      if (status === '' && beforeId === 51) return new Promise((resolve) => { resolveOlder = resolve; });
      if (status === 'failed') return Promise.resolve(page([draft(500, 'failed-row', 'failed')], 1));
      return Promise.resolve(page([draft(101, 'new-first-page-row')], 1));
    });

    mount();
    await screen.findByRole('heading', { name: '文章草稿' });
    await screen.findByRole('button', { name: /加载更早/ });
    fireEvent.click(screen.getByRole('button', { name: /加载更早/ }));
    await waitFor(() => expect(api.listDrafts).toHaveBeenCalledWith('', 50, 0, 51));

    fireEvent.click(screen.getByRole('button', { name: '失败' }));
    await screen.findByRole('button', { name: /查看草稿「failed-row」/ });
    fireEvent.click(screen.getByRole('button', { name: '全部' }));
    await screen.findByRole('button', { name: /查看草稿「new-first-page-row」/ });

    await act(async () => {
      resolveOlder(page([draft(51, 'stale-older-row')], 51));
    });
    expect(screen.queryByRole('button', { name: /查看草稿「stale-older-row」/ })).toBeNull();
    expect(screen.getByRole('button', { name: /查看草稿「new-first-page-row」/ })).toBeTruthy();
  });
});
