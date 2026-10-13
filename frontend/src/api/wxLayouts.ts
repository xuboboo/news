/**
 * 公众号排版草稿 API 客户端（对接后端 /api/v2/wx-layouts*）。
 * 鉴权与 CSRF 约定同 drafts.ts（复用其请求封装：HttpOnly Cookie + X-Requested-With）。
 * 排版为后端确定性渲染（不经 LLM），生成同步完成，无 generating 轮询。
 */
import { draftRequest as request } from './drafts';

export interface WxLayoutItem {
  id: number;
  report_id: number;
  report_date: string;
  issue_no: number;
  /** 拟发布的公众号标题（后台单独填写，单独复制） */
  title: string;
  /** 公众号摘要，取早报综述 */
  digest: string;
  gen_count: number;
  revision: number;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
  /** 整篇内联样式 HTML（仅详情接口返回） */
  content_html?: string;
}

export interface WxLayoutListPage {
  items: WxLayoutItem[];
  total: number;
}

/** 排版某日早报；同日已存在则覆盖重排版（regenerated=true）。 */
export const generateWxLayout = (date: string) =>
  request<WxLayoutItem & { regenerated: boolean }>('/wx-layouts', {
    method: 'POST',
    body: JSON.stringify({ date }),
  });

export const listWxLayouts = (limit = 50, offset = 0) => {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<WxLayoutListPage>(`/wx-layouts?${qs.toString()}`);
};

export const getWxLayout = (id: number) => request<WxLayoutItem>(`/wx-layouts/${id}`);

/** 以源早报当前内容重新排版（早报重建后同步排版稿）。 */
export const regenerateWxLayout = (id: number) =>
  request<WxLayoutItem>(`/wx-layouts/${id}/regenerate`, {
    method: 'POST',
    body: JSON.stringify({}),
  });

export const deleteWxLayout = (id: number, revision: number) =>
  request<{ deleted: boolean }>(`/wx-layouts/${id}`, {
    method: 'DELETE',
    body: JSON.stringify({ revision }),
  });
