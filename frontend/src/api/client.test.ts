import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ArticleWithScore } from '../types';

/**
 * API client 数据链路单测（2026-09-09 审计补齐，此前前端零测试）：
 * - mapApiArticle 保留 event_ids / ai_status（审计：旧映射丢弃，关联靠缓存反推失真）；
 * - eventsForArticle 优先后端 eventIds、缺省回退缓存反推；
 * - aiSummaryText 三态（有摘要 / pending 处理中 / 其余未生成）。
 *
 * 通过 stub 全局 fetch 提供 /health、/articles/latest、/events 响应后走
 * bootstrapFromApi 真实水合路径，测试对象与生产完全同源。
 */

function jsonResponse(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => data,
  } as unknown as Response;
}

const ARTICLE_1 = {
  id: 1,
  title: '豆包输入法推出 Windows 版',
  channel: '智能时代',
  source: 'IT之家',
  url: 'https://example.com/1',
  publish_time: new Date().toISOString(),
  views: 10,
  comments: 0,
  shares: 0,
  likes: 0,
  hot_score: 42.5,
  ai_status: 'summarized',
  event_ids: [3],
  summary: { summary_short: '豆包输入法登陆 Windows。', key_points: [], tags: ['豆包'], model: 'local' },
};

const EVENT_3 = {
  id: 3,
  title: '豆包输入法覆盖全平台',
  summary: '',
  impacts: [],
  status: '发酵中',
  hot_score: 10,
  articles_count: 1,
  sources_count: 1,
  articles: [],
  timeline: [],
};

let fetchMock = vi.fn();

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/health')) return jsonResponse({ status: 'ok' });
    if (url.includes('/articles/latest'))
      return jsonResponse({ items: [ARTICLE_1], total: 1 });
    if (url.includes('/events')) return jsonResponse({ items: [EVENT_3] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
});

import {
  aiSummaryText,
  bootstrapFromApi,
  eventById,
  eventsForArticle,
  loadMoreArticles,
  loadMoreEvents,
  ARTICLES_WITH_SCORE,
  EVENTS_WITH_SCORE,
} from './client';

async function boot() {
  const ok = await bootstrapFromApi();
  expect(ok).toBe(true);
}

describe('mapApiArticle（经 bootstrapFromApi 水合）', () => {
  it('保留后端 event_ids 与 ai_status', async () => {
    await boot();
    const a = ARTICLES_WITH_SCORE[0];
    expect(a.eventIds).toEqual(['3']);
    expect(a.aiStatus).toBe('summarized');
  });
});

describe('eventsForArticle', () => {
  it('优先后端 eventIds 关联（全量口径）', async () => {
    await boot();
    const a = ARTICLES_WITH_SCORE[0];
    const evs = eventsForArticle(a);
    expect(evs.map((e) => e.id)).toContain('e3');
  });

  it('eventIds 缺省时回退事件缓存反推', async () => {
    await boot();
    const legacy: Pick<ArticleWithScore, 'id' | 'eventIds'> = { id: '1', eventIds: [] };
    // 事件列表未内嵌成员（articles: []），反推路径应返回空而不是崩溃
    const evs = eventsForArticle(legacy);
    expect(evs).toEqual([]);
  });
});

describe('aiSummaryText 三态', () => {
  it('有摘要 → 展示摘要', () => {
    expect(
      aiSummaryText({ summary: { summary_short: '内容', key_points: [], tags: [], model: '' }, aiStatus: 'pending' })
    ).toBe('内容');
  });
  it('pending 无摘要 → 处理中', () => {
    expect(
      aiSummaryText({ summary: { summary_short: '', key_points: [], tags: [], model: '' }, aiStatus: 'pending' })
    ).toBe('AI 摘要处理中…');
  });
  it('失败/缺失 → 未生成（不再永远显示"生成中"）', () => {
    expect(
      aiSummaryText({ summary: { summary_short: '', key_points: [], tags: [], model: '' }, aiStatus: 'clustered' })
    ).toBe('暂无 AI 摘要');
    expect(
      aiSummaryText({ summary: { summary_short: '', key_points: [], tags: [], model: '' }, aiStatus: undefined })
    ).toBe('暂无 AI 摘要');
  });
});

describe('eventById', () => {
  it('兼容 "3" 与 "e3" 两种路由参数', async () => {
    await boot();
    expect(eventById('3')?.id).toBe('e3');
    expect(eventById('e3')?.id).toBe('e3');
  });
});

describe('loadMoreArticles', () => {
  it('无新增时返回 0（页面据此显示"没有更多"）', async () => {
    await boot();
    // offset=1 请求返回与首页相同的文章 → 去重后新增 0
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('offset=1')) return jsonResponse({ items: [], total: 1 });
      if (url.includes('/health')) return jsonResponse({ status: 'ok' });
      if (url.includes('/articles/latest')) return jsonResponse({ items: [ARTICLE_1], total: 1 });
      if (url.includes('/events')) return jsonResponse({ items: [EVENT_3] });
      return jsonResponse({});
    });
    expect(await loadMoreArticles()).toBe(0);
  });

  it('追加的新文章进入全局列表且不重复', async () => {
    await boot();
    const article2 = { ...ARTICLE_1, id: 2, title: '更早的文章' };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('offset=1')) return jsonResponse({ items: [article2], total: 2 });
      if (url.includes('/health')) return jsonResponse({ status: 'ok' });
      if (url.includes('/articles/latest')) return jsonResponse({ items: [ARTICLE_1], total: 2 });
      if (url.includes('/events')) return jsonResponse({ items: [EVENT_3] });
      return jsonResponse({});
    });
    expect(await loadMoreArticles()).toBe(1);
    expect(ARTICLES_WITH_SCORE.map((a) => a.id)).toEqual(['1', '2']);
  });
});

describe('loadMoreEvents', () => {
  it('无新增时返回 0；追加事件进入全局列表且不重复', async () => {
    await boot();
    expect(EVENTS_WITH_SCORE.map((e) => e.id)).toEqual(['e3']);
    const event4 = { ...EVENT_3, id: 4, title: '更早的事件' };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/events?limit=100&offset=1')) return jsonResponse({ items: [event4], total: 2 });
      if (url.includes('/health')) return jsonResponse({ status: 'ok' });
      if (url.includes('/articles/latest')) return jsonResponse({ items: [ARTICLE_1], total: 1 });
      if (url.includes('/events')) return jsonResponse({ items: [EVENT_3] });
      return jsonResponse({});
    });
    expect(await loadMoreEvents()).toBe(1);
    expect(EVENTS_WITH_SCORE.map((e) => e.id)).toEqual(['e3', 'e4']);
    // 追加后 eventById 可命中新事件（关联反查缓存补全）
    expect(eventById('e4')?.title).toBe('更早的事件');
    expect(await loadMoreEvents()).toBe(0);
  });
});
