// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Latest from './Latest';
import HotBoard from './HotBoard';

const mocks = vi.hoisted(() => ({
  fetchHotBoard: vi.fn(),
  article: {
    id: 'article-7', title: '页面接线资讯', channel: '科技', source: '测试来源', url: '',
    publishOffsetMin: 1, views: 0, comments: 0, shares: 0, likes: 0, eventIds: [],
    aiStatus: 'pending', summary: { summary_short: '', key_points: [], tags: [], model: '' },
    publishTime: new Date('2026-09-26T10:00:00Z'), rawScore: 1, hotScore: 1,
  },
}));
vi.mock('../api/client', () => ({
  ARTICLES_WITH_SCORE: [mocks.article], FRESH_ARTICLE_IDS: new Set(), LAST_SYNC: Date.now(),
  aiSummaryText: () => '', eventsForArticle: () => [], loadMoreArticles: vi.fn().mockResolvedValue(0),
  fetchHotBoard: mocks.fetchHotBoard,
}));
vi.mock('../utils/hooks', () => ({
  activateOnKey: (fn: () => void) => fn,
  useBootstrapReady: () => true, useDataVersion: () => 1, usePageTitle: vi.fn(), useTick: () => 0,
}));
vi.mock('../components/common', () => {
  const Stub = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  return { AiBadge: Stub, ChannelTag: Stub, HeatBar: Stub, Loading: Stub, PageHeader: Stub,
    RankBadge: Stub, SourceBadge: Stub, StatCard: Stub, TagChip: Stub };
});
vi.mock('../components/ArticleModal', () => ({
  default: ({ onOpenDraft, onOpenSiteDraft }: { onOpenDraft: (id: number) => void; onOpenSiteDraft: (id: number) => void }) => (
    <><button onClick={() => onOpenDraft(391)}>modal-draft-entry</button><button onClick={() => onOpenSiteDraft(492)}>modal-site-draft-entry</button></>
  ),
}));

function Destination() {
  const location = useLocation();
  return <output data-testid="destination">{location.pathname}</output>;
}

function renderPage(page: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={page} />
        <Route path="/drafts/:id" element={<Destination />} />
        <Route path="/site-drafts/:id" element={<Destination />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('article list pages connect draft navigation', () => {
  it('Latest passes the modal callback through to /drafts/:id', async () => {
    renderPage(<Latest />);
    fireEvent.click(await screen.findByRole('button', { name: '查看「页面接线资讯」详情' }));
    fireEvent.click(screen.getByRole('button', { name: 'modal-draft-entry' }));
    expect((await screen.findByTestId('destination')).textContent).toBe('/drafts/391');
  });

  it('HotBoard passes the modal callback through to /drafts/:id', async () => {
    mocks.fetchHotBoard.mockResolvedValue({ items: [mocks.article], total: 1 });
    renderPage(<HotBoard />);
    fireEvent.click(await screen.findByRole('button', { name: '查看「页面接线资讯」的 AI 摘要详情' }));
    fireEvent.click(screen.getByRole('button', { name: 'modal-draft-entry' }));
    expect(screen.getByTestId('destination').textContent).toBe('/drafts/391');
  });
  it('Latest navigates independently to /site-drafts/:id', async () => {
    renderPage(<Latest />);
    fireEvent.click(await screen.findByRole('button', { name: '查看「页面接线资讯」详情' }));
    fireEvent.click(screen.getByRole('button', { name: 'modal-site-draft-entry' }));
    expect((await screen.findByTestId('destination')).textContent).toBe('/site-drafts/492');
  });
  it('HotBoard navigates independently to /site-drafts/:id', async () => {
    mocks.fetchHotBoard.mockResolvedValue({ items: [mocks.article], total: 1 });
    renderPage(<HotBoard />);
    fireEvent.click(await screen.findByRole('button', { name: '查看「页面接线资讯」的 AI 摘要详情' }));
    fireEvent.click(screen.getByRole('button', { name: 'modal-site-draft-entry' }));
    expect(screen.getByTestId('destination').textContent).toBe('/site-drafts/492');
  });
});
