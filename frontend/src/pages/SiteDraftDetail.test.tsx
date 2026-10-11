// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, Link, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SiteDraftDetail from './SiteDraftDetail';
import type { SiteDraftItem } from '../api/siteDrafts';

const api = vi.hoisted(() => ({ getSiteDraft: vi.fn(), updateSiteDraft: vi.fn(), deleteSiteDraft: vi.fn(), regenerateSiteDraft: vi.fn() }));
const clipboard = vi.hoisted(() => ({ copyPlain: vi.fn(), copyRich: vi.fn() }));
vi.mock('../api/siteDrafts', async (original) => ({ ...(await original<typeof import('../api/siteDrafts')>()), ...api }));
vi.mock('../utils/clipboard', async (original) => ({ ...(await original<typeof import('../utils/clipboard')>()), ...clipboard }));

const draft: SiteDraftItem = {
  id: 71, channel: 'site', status: 'ready', source_type: 'article', source_id: 1, source_title: '网站选题', tier: 'brief',
  title: '网站稿标题', alt_titles: ['备选标题'], digest: '网站内容简介', seo_keywords: '软件,更新', site_tags: '软件',
  cover_guidance: '清晰的1:1产品方图', software_id: 0, is_pinned: false, is_recommended: false, is_headline: false,
  publish_at: null, content_md: '事实正文', source_refs: [{ source: '可信媒体', title: '事实报道' }],
  model: 'mock', prompt_tokens: 0, completion_tokens: 0, duration_ms: 1, error: '', gen_count: 1, revision: 1,
  created_by: null, created_at: null, updated_at: null,
};
function mount() {
  const router = createMemoryRouter([
    { path: '/site-drafts/:id', element: <><Link to="/site-drafts/72">切换网站稿</Link><SiteDraftDetail /></> },
    { path: '/site-drafts', element: <div>网站列表目标</div> },
  ], { initialEntries: ['/site-drafts/71'] });
  return { ...render(<RouterProvider router={router} />), router };
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => {
  vi.clearAllMocks();
  api.getSiteDraft.mockResolvedValue({ ...draft });
  api.updateSiteDraft.mockImplementation(async (_id, patch) => ({ ...draft, ...patch, revision: 2 }));
  api.deleteSiteDraft.mockResolvedValue({ deleted: true });
  clipboard.copyPlain.mockResolvedValue(undefined); clipboard.copyRich.mockResolvedValue(undefined);
});
describe('website CMS fields', () => {
  it('copies individual fields and copies body without a duplicate H1', async () => {
    mount();
    const copyTitle = await screen.findByRole('button', { name: '复制标题' }) as HTMLButtonElement;
    await waitFor(() => expect(copyTitle.disabled).toBe(false));
    fireEvent.click(copyTitle);
    expect(clipboard.copyPlain).toHaveBeenCalledWith('网站稿标题');
    fireEvent.click(screen.getByRole('button', { name: '复制页面关键字' }));
    expect(clipboard.copyPlain).toHaveBeenCalledWith('软件,更新');
    fireEvent.click(screen.getByRole('button', { name: '复制新闻正文' }));
    await waitFor(() => expect(clipboard.copyRich).toHaveBeenCalled());
    const [html, plain] = clipboard.copyRich.mock.calls[0];
    expect(html).not.toContain('<h1'); expect(plain).not.toContain('网站稿标题');
    expect(plain).not.toContain('事实报道'); expect(plain).not.toContain('AI 基于公开报道辅助生成');
    expect(plain).not.toMatch(/(^|\n)##|\*\*/);
    expect((await screen.findByLabelText('＊标题') as HTMLInputElement).required).toBe(true);
    expect(screen.getByLabelText('＊内容简介').getAttribute('aria-required')).toBe('true');
    expect(screen.getByText(/人工上传1:1方图/)).toBeTruthy();
  });
  it('removes legacy AI declarations from website preview and clipboard output', async () => {
    api.getSiteDraft.mockResolvedValueOnce({
      ...draft,
      content_md: '事实正文\n\n## AI 声明\n本文由 AI 基于公开报道辅助生成，内容经编辑整理。',
    });
    mount();
    await screen.findByLabelText('＊标题');
    expect(document.querySelector('.bg-white')?.textContent).toContain('事实正文');
    expect(document.querySelector('.bg-white')?.textContent).not.toContain('本文由 AI');
    fireEvent.click(screen.getByRole('button', { name: '复制新闻正文' }));
    await waitFor(() => expect(clipboard.copyRich).toHaveBeenCalled());
    const [html, plain] = clipboard.copyRich.mock.calls[0];
    expect(html).not.toContain('本文由 AI');
    expect(plain).not.toContain('本文由 AI');
    expect(plain).not.toContain('事实报道');
  });
  it('restores clean state after changing a field back', async () => {
    mount();
    const title = await screen.findByLabelText('＊标题');
    fireEvent.change(title, { target: { value: '临时标题' } });
    expect(screen.getByRole('button', { name: '保存修改' })).toBeTruthy();
    fireEvent.change(title, { target: { value: draft.title } });
    expect(screen.getByRole('button', { name: '已保存' }).hasAttribute('disabled')).toBe(true);
  });
  it('keeps edits made while an earlier save is pending', async () => {
    let resolveSave!: (value: SiteDraftItem) => void;
    api.updateSiteDraft.mockReturnValue(new Promise((resolve) => { resolveSave = resolve; }));
    mount();
    const title = await screen.findByLabelText('＊标题');
    fireEvent.change(title, { target: { value: '提交标题' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    fireEvent.change(title, { target: { value: '继续修改' } });
    await act(async () => { resolveSave({ ...draft, title: '提交标题', revision: 2 }); });
    expect(screen.getByDisplayValue('继续修改')).toBeTruthy();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeTruthy();
  });
  it('polls a generating draft and displays its ready fields', async () => {
    const timer = vi.spyOn(window, 'setInterval');
    api.getSiteDraft.mockResolvedValueOnce({ ...draft, status: 'generating' }).mockResolvedValue({ ...draft, revision: 2 });
    mount();
    await screen.findByText(/完成后自动显示编辑字段/);
    await act(async () => { (timer.mock.calls[timer.mock.calls.length - 1][0] as () => void)(); });
    expect(await screen.findByLabelText('＊标题')).toBeTruthy();
  });
  it('keeps an uncertain submitted save protected after the user reverts locally', async () => {
    let rejectSave!: (error: Error) => void;
    api.updateSiteDraft.mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject; }));
    mount();
    const title = await screen.findByLabelText('＊标题');
    fireEvent.change(title, { target: { value: '提交后失败' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    fireEvent.change(title, { target: { value: draft.title } });
    await act(async () => { rejectSave(new Error('保存失败')); });
    expect((await screen.findByRole('alert')).textContent).toContain('保存失败');
    fireEvent.change(title, { target: { value: draft.title } });
    expect((screen.getByRole('button', { name: '保存修改' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText(/上次保存结果尚未确认/)).toBeTruthy();
  });
  it('clears reverted edits after a definite client-side save rejection', async () => {
    let rejectSave!: (error: Error & { status?: number }) => void;
    api.updateSiteDraft.mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject; }));
    mount();
    const title = await screen.findByLabelText('＊标题');
    fireEvent.change(title, { target: { value: '无效提交' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    fireEvent.change(title, { target: { value: draft.title } });
    await act(async () => { rejectSave(Object.assign(new Error('字段校验失败'), { status: 400 })); });
    expect((await screen.findByRole('alert')).textContent).toContain('字段校验失败');
    expect(screen.getByRole('button', { name: '已保存' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByText(/所有修改已保存/)).toBeTruthy();
  });
  it('blocks leaving while a save is pending even if the form is reverted', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    let resolveSave!: (value: SiteDraftItem) => void;
    api.updateSiteDraft.mockReturnValue(new Promise((resolve) => { resolveSave = resolve; }));
    const { router } = mount();
    const title = await screen.findByLabelText('＊标题');
    fireEvent.change(title, { target: { value: '提交中的标题' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    fireEvent.change(title, { target: { value: draft.title } });
    fireEvent.click(screen.getByRole('link', { name: '切换网站稿' }));
    await waitFor(() => expect(confirm).toHaveBeenCalledOnce());
    expect(confirm.mock.calls[0][0]).toContain('正在保存');
    expect(router.state.location.pathname).toBe('/site-drafts/71');
    await act(async () => { resolveSave({ ...draft, title: '提交中的标题', revision: 2 }); });
    expect(screen.getByDisplayValue(draft.title)).toBeTruthy();
    expect(screen.getByRole('button', { name: '保存修改' })).toBeTruthy();
  });
  it('disables editing during regeneration and applies the new result', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const timer = vi.spyOn(window, 'setInterval');
    let resolveRegenerate!: (value: SiteDraftItem) => void;
    api.regenerateSiteDraft.mockReturnValue(new Promise((resolve) => { resolveRegenerate = resolve; }));
    const { router } = mount();
    const title = await screen.findByLabelText('＊标题') as HTMLInputElement;
    fireEvent.click(screen.getByRole('button', { name: '重新生成' }));
    expect(title.disabled).toBe(true);
    expect((screen.getByRole('button', { name: '备选标题' }) as HTMLButtonElement).disabled).toBe(true);
    expect(title.value).toBe(draft.title);
    await act(async () => { resolveRegenerate({ ...draft, status: 'generating', gen_count: 2, revision: 2 }); });
    expect(await screen.findByText(/完成后自动显示编辑字段/)).toBeTruthy();
    api.getSiteDraft.mockResolvedValue({ ...draft, title: '重新生成的新标题', gen_count: 2, revision: 3 });
    const poll = timer.mock.calls.find((call) => call[1] === 3000)?.[0] as (() => void) | undefined;
    expect(poll).toBeDefined();
    await act(async () => { poll?.(); });
    expect(await screen.findByDisplayValue('重新生成的新标题')).toBeTruthy();
    expect(router.state.location.pathname).toBe('/site-drafts/71');
    expect(confirm).toHaveBeenCalledOnce();
  });
  it('clears a transient poll error after a later successful response', async () => {
    const timer = vi.spyOn(window, 'setInterval');
    api.getSiteDraft
      .mockResolvedValueOnce({ ...draft, status: 'generating' })
      .mockRejectedValueOnce(new Error('临时轮询失败'))
      .mockResolvedValueOnce({ ...draft, revision: 2 });
    mount();
    await screen.findByText(/完成后自动显示编辑字段/);
    const poll = timer.mock.calls.find((call) => call[1] === 3000)?.[0] as (() => void) | undefined;
    expect(poll).toBeDefined();
    await act(async () => { poll?.(); });
    expect((await screen.findByRole('alert')).textContent).toContain('临时轮询失败');
    await act(async () => { poll?.(); });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
  it('rejects invalid software IDs instead of silently changing them to 0', async () => {
    mount();
    fireEvent.change(await screen.findByLabelText('关联软件 ID'), { target: { value: '1.9' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '关联软件 ID 必须是有效的非负整数；不关联请填0');
    expect(api.updateSiteDraft).not.toHaveBeenCalled();
  });
  it('saves publication time with an explicit Beijing offset', async () => {
    mount();
    fireEvent.change(await screen.findByLabelText('发布时间（北京时间）'), { target: { value: '2026-09-27T15:30:47' } });
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
    await waitFor(() => expect(api.updateSiteDraft).toHaveBeenCalledWith(71, expect.objectContaining({ publish_at: '2026-09-27T15:30:47+08:00' })));
  });
  it('does not show a late delete failure on another draft', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let rejectDelete!: (error: Error) => void;
    api.deleteSiteDraft.mockReturnValue(new Promise((_resolve, reject) => { rejectDelete = reject; }));
    api.getSiteDraft.mockImplementation((id) => Promise.resolve({ ...draft, id, title: `稿件${id}` }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '删除草稿' }));
    fireEvent.click(screen.getByRole('link', { name: '切换网站稿' }));
    await screen.findByDisplayValue('稿件72');
    await act(async () => { rejectDelete(new Error('旧稿失败')); });
    expect(screen.queryByText('旧稿失败')).toBeNull(); expect(screen.queryByText('网站列表目标')).toBeNull();
    confirm.mockRestore();
  });
  it('deletes a dirty draft without a second leave confirmation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mount();
    fireEvent.change(await screen.findByLabelText('＊标题'), { target: { value: '未保存' } });
    fireEvent.click(screen.getByRole('button', { name: '删除草稿' }));
    expect(await screen.findByText('网站列表目标')).toBeTruthy(); expect(confirm).toHaveBeenCalledOnce();
  });
});
