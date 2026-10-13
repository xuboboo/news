import { beforeEach, describe, expect, it, vi } from 'vitest';
import { deleteWxLayout, generateWxLayout, listWxLayouts } from './wxLayouts';

const fetchMock = vi.fn();
beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, text: async () => JSON.stringify({ id: 9 }) });
  vi.stubGlobal('fetch', fetchMock);
});

describe('wxLayouts API 客户端', () => {
  it('排版指定日期早报：POST /wx-layouts 携带 date 与 CSRF 头', async () => {
    await generateWxLayout('2026-10-14');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/wx-layouts');
    expect(init.method).toBe('POST');
    expect(init.headers['X-Requested-With']).toBe('XMLHttpRequest');
    expect(JSON.parse(init.body)).toEqual({ date: '2026-10-14' });
  });

  it('列表请求按 limit/offset 拼查询串', async () => {
    await listWxLayouts(20, 40);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v2/wx-layouts?limit=20&offset=40');
  });

  it('删除携带乐观锁 revision', async () => {
    await deleteWxLayout(9, 4);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('DELETE');
    expect(JSON.parse(init.body)).toEqual({ revision: 4 });
  });

  it('非 2xx 响应抛出后端 detail 信息', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 404,
      text: async () => JSON.stringify({ detail: '该日期暂无早报，请先在「运维监控」生成早报' }),
    });
    await expect(generateWxLayout('2026-10-01')).rejects.toThrow('该日期暂无早报');
  });
});
