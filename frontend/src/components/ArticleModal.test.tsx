// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ArticleModal from './ArticleModal';
import type { ArticleWithScore } from '../types';

const createDraft = vi.hoisted(() => vi.fn());
const createSiteDraft = vi.hoisted(() => vi.fn());
vi.mock('../api/siteDrafts', async (original) => ({
  ...(await original<typeof import('../api/siteDrafts')>()),
  createSiteDraft,
}));
vi.mock('../api/drafts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/drafts')>()),
  createDraft,
}));

const article: ArticleWithScore = {
  id: '42', title: '测试资讯', channel: '智能时代', source: '媒体', url: 'javascript:alert(1)',
  publishOffsetMin: 1, views: 0, comments: 0, shares: 0, likes: 0, eventIds: [],
  aiStatus: 'pending' as const,
  summary: { summary_short: '', key_points: [], tags: [], model: '' },
  publishTime: new Date(), rawScore: 0, hotScore: 0,
};

afterEach(cleanup);
beforeEach(() => { createDraft.mockReset(); createSiteDraft.mockReset(); });

function mount(onOpenDraft = vi.fn()) {
  render(<ArticleModal article={article} onClose={vi.fn()} onOpenEvent={vi.fn()} onOpenDraft={onOpenDraft} />);
  return onOpenDraft;
}

describe('ArticleModal draft entry', () => {
  it('creates an independent website draft and opens its own detail route', async () => {
    createSiteDraft.mockResolvedValue({ id: 29 });
    const onOpenSiteDraft = vi.fn();
    render(<ArticleModal article={article} onClose={vi.fn()} onOpenEvent={vi.fn()} onOpenDraft={vi.fn()} onOpenSiteDraft={onOpenSiteDraft} />);
    fireEvent.click(screen.getByRole('button', { name: '生成网站稿' }));
    await waitFor(() => expect(onOpenSiteDraft).toHaveBeenCalledWith(29));
    expect(createSiteDraft).toHaveBeenCalledWith('article', 42, 'brief');
    expect(createDraft).not.toHaveBeenCalled();
  });

  it('opens a draft after successful creation', async () => {
    createDraft.mockResolvedValue({ id: 19 });
    const onOpenDraft = mount();
    fireEvent.click(screen.getByRole('button', { name: '生成公众号稿' }));
    await waitFor(() => expect(onOpenDraft).toHaveBeenCalledWith(19));
    expect(createDraft).toHaveBeenCalledWith('article', 42, 'brief');
  });

  it('does not navigate after the modal is closed during draft creation', async () => {
    let resolve!: (value: { id: number }) => void;
    createDraft.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const onOpenDraft = vi.fn();
    const view = render(<ArticleModal article={article} onClose={vi.fn()} onOpenEvent={vi.fn()} onOpenDraft={onOpenDraft} />);
    fireEvent.click(screen.getByRole('button', { name: '生成公众号稿' }));
    view.unmount();
    resolve({ id: 21 });
    await waitFor(() => expect(createDraft).toHaveBeenCalledOnce());
    expect(onOpenDraft).not.toHaveBeenCalled();
  });

  it('allows a new article request after changing modal articles and ignores the old response', async () => {
    let resolveOld!: (value: { id: number }) => void;
    let resolveCurrent!: (value: { id: number }) => void;
    createDraft
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveCurrent = resolve; }));
    const onOpenDraft = vi.fn();
    const view = render(<ArticleModal article={article} onClose={vi.fn()} onOpenEvent={vi.fn()} onOpenDraft={onOpenDraft} />);
    fireEvent.click(screen.getByRole('button', { name: '生成公众号稿' }));
    view.rerender(<ArticleModal article={{ ...article, id: '43', title: '另一条资讯' }} onClose={vi.fn()} onOpenEvent={vi.fn()} onOpenDraft={onOpenDraft} />);
    const button = await screen.findByRole('button', { name: '生成公众号稿' });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    expect(await screen.findByRole('button', { name: '创建中…' })).toBeTruthy();
    resolveOld({ id: 21 });
    await waitFor(() => expect(createDraft).toHaveBeenCalledTimes(2));
    expect(onOpenDraft).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: '创建中…' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { resolveCurrent({ id: 22 }); });
    await waitFor(() => expect(onOpenDraft).toHaveBeenCalledWith(22));
  });

  it('shows failures and prevents duplicate in-flight creation', async () => {
    let resolve!: (value: { id: number }) => void;
    createDraft.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    mount();
    const button = screen.getByRole('button', { name: '生成公众号稿' });
    fireEvent.click(button);
    expect(screen.getByRole('button', { name: '创建中…' }).hasAttribute('disabled')).toBe(true);
    expect(createDraft).toHaveBeenCalledOnce();
    resolve({ id: 20 });
    await waitFor(() => expect(createDraft).toHaveBeenCalledOnce());

    // 失败文案留在弹窗，重试可再次提交；不会静默关闭。
    createDraft.mockRejectedValueOnce(new Error('限流'));
    fireEvent.click(screen.getByRole('button', { name: '生成公众号稿' }));
    expect((await screen.findByRole('alert')).textContent).toContain('限流');
    expect(screen.getByRole('dialog', { name: '测试资讯' })).toBeTruthy();
  });
});
