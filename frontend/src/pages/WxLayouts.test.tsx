// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import WxLayouts from './WxLayouts';
import type { WxLayoutItem } from '../api/wxLayouts';

const api = vi.hoisted(() => ({
  listWxLayouts: vi.fn(),
  deleteWxLayout: vi.fn(),
  generateWxLayout: vi.fn(),
}));
const client = vi.hoisted(() => ({
  availableDates: vi.fn(),
  loadReports: vi.fn(),
  reportsLoaded: true,
}));
vi.mock('../api/wxLayouts', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/wxLayouts')>()),
  listWxLayouts: api.listWxLayouts,
  deleteWxLayout: api.deleteWxLayout,
  generateWxLayout: api.generateWxLayout,
}));
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  availableDates: client.availableDates,
  loadReports: client.loadReports,
  reportsLoaded: client.reportsLoaded,
}));
vi.mock('../utils/hooks', () => ({
  usePageTitle: vi.fn(),
}));
vi.mock('../components/common', () => ({
  Loading: () => <div>loading</div>,
  PageHeader: ({ title, children }: { title: string; children?: React.ReactNode }) => (
    <header><h1>{title}</h1>{children}</header>
  ),
}));

const layout = (id: number, title: string): WxLayoutItem => ({
  id,
  report_id: id,
  report_date: '2026-10-14',
  issue_no: 200,
  title,
  digest: '早报综述摘要',
  gen_count: 1,
  revision: 1,
  created_by: null,
  created_at: null,
  updated_at: null,
});

function mount() {
  return render(
    <MemoryRouter>
      <WxLayouts />
    </MemoryRouter>
  );
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  client.reportsLoaded = true;
  client.availableDates.mockReturnValue([
    { key: '2026-10-14', label: '今天', offsetMin: 0 },
    { key: '2026-10-13', label: '昨天', offsetMin: 0 },
  ]);
  client.loadReports.mockResolvedValue(undefined);
  api.listWxLayouts.mockResolvedValue({ items: [], total: 0 });
  api.deleteWxLayout.mockResolvedValue({ deleted: true });
  api.generateWxLayout.mockResolvedValue({ ...layout(9, '新稿'), regenerated: false });
});

describe('WxLayouts 公众号排版草稿箱', () => {
  it('渲染排版草稿列表：期数/标题/摘要', async () => {
    api.listWxLayouts.mockResolvedValue({ items: [layout(1, '今日早报丨10月14日·昨夜今晨科技热点')], total: 1 });
    mount();
    expect(await screen.findByText('今日早报丨10月14日·昨夜今晨科技热点')).toBeTruthy();
    expect(screen.getByText('第 200 期')).toBeTruthy();
    expect(screen.getByText('早报综述摘要')).toBeTruthy();
  });

  it('默认选最新日期生成，成功后不再停留本页（导航由路由处理）', async () => {
    mount();
    const btn = await screen.findByRole('button', { name: '排版 2026-10-14 早报' });
    fireEvent.click(btn);
    await waitFor(() => expect(api.generateWxLayout).toHaveBeenCalledWith('2026-10-14'));
  });

  it('点「昨天」chip 切换生成日期', async () => {
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '昨天' }));
    const btn = await screen.findByRole('button', { name: '排版 2026-10-13 早报' });
    fireEvent.click(btn);
    await waitFor(() => expect(api.generateWxLayout).toHaveBeenCalledWith('2026-10-13'));
  });

  it('生成失败展示后端错误信息（如该日期无早报）', async () => {
    api.generateWxLayout.mockRejectedValue(new Error('该日期暂无早报，请先在「运维监控」生成早报'));
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '排版 2026-10-14 早报' }));
    expect((await screen.findByRole('alert')).textContent).toContain('该日期暂无早报');
  });

  it('删除草稿携带乐观锁 revision 并刷新列表', async () => {
    api.listWxLayouts.mockResolvedValue({ items: [layout(1, '早报排版稿')], total: 1 });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '删除排版草稿「早报排版稿」' }));
    await waitFor(() => expect(api.deleteWxLayout).toHaveBeenCalledWith(1, 1));
    await waitFor(() => expect(api.listWxLayouts).toHaveBeenCalledTimes(2));
    confirmSpy.mockRestore();
  });

  it('删除取消确认则不调用接口', async () => {
    api.listWxLayouts.mockResolvedValue({ items: [layout(1, '早报排版稿')], total: 1 });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '删除排版草稿「早报排版稿」' }));
    await waitFor(() => expect(api.listWxLayouts).toHaveBeenCalled());
    expect(api.deleteWxLayout).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
