import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createDraft } from './drafts';

const fetchMock = vi.fn();
beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, text: async () => JSON.stringify({ id: 9 }) });
  vi.stubGlobal('fetch', fetchMock);
});

describe('createDraft 选题编号', () => {
  it('将页面中的 e 前缀事件 ID 转为数据库整数 ID', async () => {
    await createDraft('event', 'e3', 'brief');
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body).source_id).toBe(3);
  });

  it('保持单篇资讯的整数 ID', async () => {
    await createDraft('article', 42, 'depth');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).source_id).toBe(42);
  });

  it.each(['eabc', '', 'e0', 'e-1', 'e1.5'])('拒绝无效编号 %s，避免错误派发', async (id) => {
    await expect(createDraft('event', id, 'brief')).rejects.toThrow('选题编号无效');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
