// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, Link, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DraftDetail from './DraftDetail';
import { copyRich } from '../utils/clipboard';
import type { DraftItem } from '../api/drafts';

const api = vi.hoisted(() => ({
  getDraft: vi.fn(),
  updateDraft: vi.fn(),
  regenerateDraft: vi.fn(),
  deleteDraft: vi.fn(),
  fetchArticleById: vi.fn(),
}));
vi.mock('../api/drafts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/drafts')>()),
  ...api,
}));
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  fetchArticleById: api.fetchArticleById,
}));
vi.mock('../utils/clipboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  copyRich: vi.fn().mockResolvedValue(undefined),
  copyPlain: vi.fn().mockResolvedValue(undefined),
}));

const draft: DraftItem = {
  id: 17, status: 'ready', source_type: 'article', source_id: 82, source_title: '来源文章',
  tier: 'brief', title: '标题', alt_titles: [], digest: '摘要', model: 'mock',
  prompt_tokens: 0, completion_tokens: 0, duration_ms: 1, error: '', gen_count: 1, revision: 1, source_refs: [],
  created_by: null, created_at: null, updated_at: null, content_md: '初始正文',
};

function mount(initialEntry = '/drafts/17') {
  const router = createMemoryRouter([
    { path: '/drafts', element: <div>草稿列表目标</div> },
    { path: '/drafts/:id', element: <><Link to="/drafts/18">切换草稿</Link><DraftDetail /></> },
    { path: '/other', element: <Link to="/drafts/18">go18</Link> },
  ], { initialEntries: [initialEntry] });
  return { ...render(<RouterProvider router={router} />), router };
}

describe('DraftDetail edit behavior', () => {
  it.each(['success', 'failure'] as const)('ignores a late delete %s after changing drafts', async (result) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let resolveDelete!: (value: { deleted: boolean }) => void;
    let rejectDelete!: (reason: Error) => void;
    api.deleteDraft.mockReturnValue(new Promise((resolve, reject) => { resolveDelete = resolve; rejectDelete = reject; }));
    api.getDraft.mockImplementation((id) => Promise.resolve({ ...draft, id: Number(id), title: `稿件${id}` }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '删除草稿「稿件17」' }));
    fireEvent.click(screen.getByRole('link', { name: '切换草稿' }));
    await screen.findByDisplayValue('稿件18');
    await act(async () => {
      if (result === 'success') resolveDelete({ deleted: true });
      else rejectDelete(new Error('旧稿删除失败'));
    });
    expect(screen.queryByText('草稿列表目标')).toBeNull();
    expect(screen.queryByText('旧稿删除失败')).toBeNull();
    expect(screen.getByDisplayValue('稿件18')).toBeTruthy();
    confirm.mockRestore();
  });

  afterEach(() => cleanup());
  beforeEach(() => {
    vi.clearAllMocks();
    api.getDraft.mockResolvedValue({ ...draft });
    api.fetchArticleById.mockReset();
    api.updateDraft.mockImplementation(async (_id, patch) => ({ ...draft, ...patch }));
  });

  it.each(['unmounted', 'returned'] as const)('ignores an old delete after the route lifetime is %s', async (mode) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let resolveDelete!: (value: { deleted: boolean }) => void;
    api.deleteDraft.mockReturnValue(new Promise((resolve) => { resolveDelete = resolve; }));
    const { router } = mount();
    fireEvent.click(await screen.findByRole('button', { name: '删除草稿「标题」' }));
    await act(async () => { await router.navigate('/other'); });
    if (mode === 'returned') {
      await act(async () => { await router.navigate('/drafts/17'); });
      await screen.findByDisplayValue('标题');
    }
    await act(async () => { resolveDelete({ deleted: true }); });
    expect(screen.queryByText('草稿列表目标')).toBeNull();
    if (mode === 'unmounted') expect(screen.getByRole('link', { name: 'go18' })).toBeTruthy();
    else expect(screen.getByDisplayValue('标题')).toBeTruthy();
    confirm.mockRestore();
  });

  it('returns to saved state when all fields are restored to their saved values', async () => {
    mount();
    const title = await screen.findByDisplayValue('标题');
    fireEvent.change(title, { target: { value: '临时标题' } });
    fireEvent.change(title, { target: { value: '标题' } });
    const saved = screen.queryByRole('button', { name: '已保存' });
    expect(saved).not.toBeNull();
    expect(screen.queryByText('有未保存的修改')).toBeNull();
  });

  it('exposes edit and preview selection to assistive technology', async () => {
    mount();
    const edit = await screen.findByRole('button', { name: '编辑' });
    expect(edit.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: '预览' }));
    expect(edit.getAttribute('aria-pressed')).toBe('false');
    expect(screen.getByRole('button', { name: '预览' }).getAttribute('aria-pressed')).toBe('true');
  });

  it('shows a prominent save action beside the draft metadata', async () => {
    mount();
    const saveButton = await screen.findByRole('button', { name: '已保存' });
    expect(saveButton.hasAttribute('disabled')).toBe(true);
    fireEvent.change(await screen.findByPlaceholderText('Markdown 正文'), { target: { value: '顶部保存测试' } });
    const activeSave = screen.getByRole('button', { name: '保存修改' });
    expect(activeSave.hasAttribute('disabled')).toBe(false);
    fireEvent.click(activeSave);
    await waitFor(() => expect(api.updateDraft).toHaveBeenCalledOnce());
  });

  it.each(['generating', 'failed'] as const)('offers deletion for %s drafts', async (status) => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.getDraft.mockResolvedValue({ ...draft, status });
    api.deleteDraft.mockResolvedValue({ deleted: true });
    mount();
    const deleteButton = await screen.findByRole('button', { name: '删除草稿「标题」' });
    fireEvent.click(deleteButton);
    await waitFor(() => expect(api.deleteDraft).toHaveBeenCalledWith(17, 1));
    expect(await screen.findByText('草稿列表目标')).toBeTruthy();
    confirm.mockRestore();
  });

  it('confirms before discarding edits during in-app navigation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    mount();
    const editor = await screen.findByPlaceholderText('Markdown 正文');
    fireEvent.change(editor, { target: { value: '尚未保存' } });
    fireEvent.click(screen.getByRole('link', { name: '切换草稿' }));
    await waitFor(() => expect(confirm).toHaveBeenCalledWith('草稿有未保存的修改，确定离开并丢弃吗？'));
    expect(await screen.findByDisplayValue('尚未保存')).toBeTruthy();
    confirm.mockRestore();
  });

  it('does not ask a second time after a confirmed delete', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.deleteDraft.mockResolvedValue({ deleted: true });
    mount();
    fireEvent.change(await screen.findByPlaceholderText('Markdown 正文'), { target: { value: '未保存' } });
    fireEvent.click(screen.getByRole('button', { name: '删除草稿「标题」' }));
    expect(await screen.findByText('草稿列表目标')).toBeTruthy();
    expect(confirm).toHaveBeenCalledOnce();
    expect(api.deleteDraft).toHaveBeenCalledWith(17, 1);
    confirm.mockRestore();
  });

  it('copies the current unsaved editor content', async () => {
    mount();
    const editor = await screen.findByPlaceholderText('Markdown 正文');
    fireEvent.change(editor, { target: { value: '**未保存的正文**' } });
    fireEvent.click(screen.getByRole('button', { name: /复制富文本/ }));
    await waitFor(() => expect(copyRich).toHaveBeenCalled());
    const [html, plain] = vi.mocked(copyRich).mock.calls[0];
    expect(html).toContain('未保存的正文');
    expect(plain).toContain('标题');
    expect(plain).toContain('未保存的正文');
    expect(plain).not.toContain('本文由 AI 基于公开报道辅助生成');
    expect(plain).not.toMatch(/(^|\n)#+|\*\*/);
  });

  it('copies the server-canonical footer after saving an edited body', async () => {
    api.updateDraft.mockImplementationOnce(async (_id, patch) => ({
      ...draft,
      ...patch,
      revision: 2,
      content_md: `${patch.content_md}\n\n## 参考来源\n- 媒体丨《固定来源》\n\n## 声明\n本文由 AI 基于公开报道辅助生成，内容经编辑整理。`,
      source_refs: [{ source: '媒体', title: '固定来源' }],
    }));
    mount();
    const editor = await screen.findByPlaceholderText('Markdown 正文');
    fireEvent.change(editor, { target: { value: '编辑后的正文' } });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));
    await waitFor(() => expect(screen.getByDisplayValue(/固定来源/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /复制富文本/ }));
    await waitFor(() => expect(copyRich).toHaveBeenCalled());
    expect(vi.mocked(copyRich).mock.calls[0][0]).toContain('固定来源');
    expect(vi.mocked(copyRich).mock.calls[0][0]).not.toContain('本文由 AI 基于公开报道辅助生成');
  });

  it('counts emoji as Python codepoints and blocks oversize form saves', async () => {
    mount();
    const title = await screen.findByPlaceholderText('文章标题');
    fireEvent.change(title, { target: { value: '😀'.repeat(31) } });
    expect(screen.getByText('31/30')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));
    expect(await screen.findByText('标题最多 30 个字符，摘要最多 120 个字符')).toBeTruthy();
    expect(api.updateDraft).not.toHaveBeenCalled();
  });

  it('opens the source article detail modal from a saved single-article draft', async () => {
    api.fetchArticleById.mockResolvedValue({
      id: '82', title: '原文标题', channel: '科技', source: '媒体', url: 'https://example.com/article',
      publishOffsetMin: 1, views: 0, comments: 0, shares: 0, likes: 0, eventIds: [],
      aiStatus: 'pending', summary: { summary_short: '', key_points: [], tags: [], model: '' },
      publishTime: new Date(), rawScore: 0, hotScore: 0,
    });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '来源文章' }));
    expect(await screen.findByRole('dialog', { name: '原文标题' })).toBeTruthy();
    expect(api.fetchArticleById).toHaveBeenCalledWith(82);
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows deleted-source 404 without opening a stale cached article', async () => {
    api.fetchArticleById.mockRejectedValue(Object.assign(new Error('HTTP 404'), { status: 404 }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '来源文章' }));
    expect(await screen.findByText('原始资讯已清理或不存在，无法查看原文。')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('offers retry for transient source lookup errors', async () => {
    api.fetchArticleById
      .mockRejectedValueOnce(Object.assign(new Error('network'), { status: 0 }))
      .mockResolvedValueOnce({
        id: '82', title: '重试成功', channel: '科技', source: '媒体', url: '', publishOffsetMin: 0,
        views: 0, comments: 0, shares: 0, likes: 0, eventIds: [], aiStatus: 'pending',
        summary: { summary_short: '', key_points: [], tags: [], model: '' },
        publishTime: new Date(), rawScore: 0, hotScore: 0,
      });
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '来源文章' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试' }));
    expect(await screen.findByRole('dialog', { name: '重试成功' })).toBeTruthy();
  });

  it('keeps edits after save rejection', async () => {
    api.updateDraft.mockRejectedValue(new Error('保存失败'));
    mount();
    const title = await screen.findByPlaceholderText('文章标题');
    fireEvent.change(title, { target: { value: '保留的编辑' } });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));
    expect(await screen.findByText('保存失败')).toBeTruthy();
    expect(screen.getByDisplayValue('保留的编辑')).toBeTruthy();
    expect(screen.getByText('有未保存的修改')).toBeTruthy();
  });

  it('trims only for validation, matching the server save policy', async () => {
    mount();
    const title = await screen.findByPlaceholderText('文章标题');
    fireEvent.change(title, { target: { value: ` ${'字'.repeat(30)} ` } });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));
    await waitFor(() => expect(api.updateDraft).toHaveBeenCalledOnce());
    expect(api.updateDraft.mock.calls[0][1].title).toBe(` ${'字'.repeat(30)} `);
  });

  it('cancels a loading source request and ignores its late response', async () => {
    let resolveArticle!: (article: object) => void;
    api.fetchArticleById.mockReturnValue(new Promise((resolve) => { resolveArticle = resolve; }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '来源文章' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消' }));
    await act(async () => resolveArticle({ ...draft, id: '82', title: '迟到原文' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('ignores a source response after navigating to another draft', async () => {
    let resolveArticle!: (article: object) => void;
    api.fetchArticleById.mockReturnValue(new Promise((resolve) => { resolveArticle = resolve; }));
    api.getDraft.mockImplementation((id) => Promise.resolve({ ...draft, id: Number(id), source_id: 900 + Number(id) }));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '来源文章' }));
    fireEvent.click(screen.getByRole('link', { name: '切换草稿' }));
    await screen.findByRole('button', { name: '来源文章' });
    await act(async () => resolveArticle({ ...draft, id: '82', title: '迟到原文' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps edits made during save marked dirty after the earlier patch completes', async () => {
    let resolveSave!: (value: DraftItem) => void;
    api.updateDraft.mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; }));
    mount();
    const title = await screen.findByPlaceholderText('文章标题');
    fireEvent.change(title, { target: { value: '提交版本' } });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));
    await waitFor(() => expect(api.updateDraft).toHaveBeenCalledOnce());
    expect(screen.getByText('有未保存的修改')).toBeTruthy();

    fireEvent.change(title, { target: { value: '保存期间继续编辑' } });
    await act(async () => resolveSave({ ...draft, title: '提交版本' }));
    expect(screen.getByDisplayValue('保存期间继续编辑')).toBeTruthy();
    expect(screen.getByText('有未保存的修改')).toBeTruthy();
  });
});
