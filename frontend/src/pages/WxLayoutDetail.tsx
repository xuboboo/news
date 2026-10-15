/**
 * 公众号排版草稿详情：整篇 HTML 预览 + 一键复制。
 * 复制走 copyRich（text/html + text/plain 双 MIME）——公众号后台编辑器
 * 粘贴时读 text/html 保留全部排版；模板图片由公众号后台自动转存。
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Copy, FileText, RefreshCw, Trash2 } from 'lucide-react';
import { deleteWxLayout, getWxLayout, regenerateWxLayout, type WxLayoutItem } from '../api/wxLayouts';
import { Loading } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { timeAgo } from '../utils/format';
import { copyPlain, copyRich, type CopyState } from '../utils/clipboard';

/** 排版 HTML → 纯文本（富文本剪贴板的 text/plain 兜底 MIME）。 */
function htmlToPlain(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const chunks: string[] = [];
  const blockTags = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'li', 'tr']);
  const visit = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      chunks.push(node.nodeValue ?? '');
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      node.childNodes.forEach(visit);
      return;
    }
    const tag = (node as HTMLElement).tagName.toLowerCase();
    if (tag === 'br') {
      chunks.push('\n');
      return;
    }
    if (blockTags.has(tag)) chunks.push('\n');
    node.childNodes.forEach(visit);
  };
  doc.body.childNodes.forEach(visit);
  return chunks.join('').replace(/[ \t]+\n/g, '\n').replace(/\n{2,}/g, '\n').trim();
}

export default function WxLayoutDetail() {
  usePageTitle('公众号排版详情');
  const navigate = useNavigate();
  const idParam = useParams().id;
  const id = Number(idParam);
  const idValid = Number.isSafeInteger(id) && id > 0;
  const activeId = useRef<number | null>(idValid ? id : null);
  useLayoutEffect(() => {
    activeId.current = idValid ? id : null;
    return () => {
      activeId.current = null;
    };
  }, [id, idValid]);
  const [layout, setLayout] = useState<WxLayoutItem | null>(null);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState<'copy' | 'title' | 'regen' | 'delete' | null>(null);
  const [copyState, setCopyState] = useState<CopyState>(null);
  const [titleState, setTitleState] = useState<CopyState>(null);

  // 路由参数变化时渲染期同步重置（React 认可的 props 派生状态模式），不闪现上一稿
  const [prevId, setPrevId] = useState<number | null>(idValid ? id : null);
  if ((idValid ? id : null) !== prevId) {
    setPrevId(idValid ? id : null);
    setLayout(null);
    setLoadError('');
    setActionError('');
    setBusy(null);
  }

  useEffect(() => {
    if (!idValid) return;
    let cancelled = false;
    getWxLayout(id)
      .then((item) => {
        if (!cancelled) setLayout(item);
      })
      .catch((e: Error) => {
        if (!cancelled) setLoadError(e.message || String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [id, idValid]);

  if (!idValid) {
    return (
      <div className="card py-16 text-center">
        <div className="text-sm text-slate-400 mb-4">草稿编号无效</div>
        <button
          onClick={() => navigate('/wx-layouts')}
          className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer hover:bg-accent/25"
        >
          返回排版草稿箱
        </button>
      </div>
    );
  }
  if (loadError) {
    return (
      <div className="card py-16 text-center">
        <div className="text-sm text-slate-400 mb-4">{loadError}</div>
        <button
          onClick={() => navigate('/wx-layouts')}
          className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer hover:bg-accent/25"
        >
          返回排版草稿箱
        </button>
      </div>
    );
  }
  if (!layout) return <Loading />;

  const html = layout.content_html ?? '';

  const onCopyAll = () => {
    if (busy) return;
    setBusy('copy');
    copyRich(html, htmlToPlain(html))
      .then(() => setCopyState('ok'))
      .catch(() => setCopyState('failed'))
      .finally(() => {
        setBusy(null);
        window.setTimeout(() => setCopyState(null), 2000);
      });
  };
  const onCopyTitle = () => {
    if (busy) return;
    setBusy('title');
    copyPlain(layout.title)
      .then(() => setTitleState('ok'))
      .catch(() => setTitleState('failed'))
      .finally(() => {
        setBusy(null);
        window.setTimeout(() => setTitleState(null), 2000);
      });
  };
  const onRegenerate = async () => {
    if (busy) return;
    if (!window.confirm(`以源早报当前内容重新排版「${layout.title}」？当前排版将被覆盖。`)) return;
    setBusy('regen');
    setActionError('');
    try {
      const updated = await regenerateWxLayout(layout.id);
      if (activeId.current === id) setLayout(updated);
    } catch (e) {
      if (activeId.current === id) setActionError((e as Error).message || String(e));
    } finally {
      if (activeId.current === id) setBusy(null);
    }
  };
  const onDelete = async () => {
    if (busy) return;
    if (!window.confirm(`删除「${layout.title}」后不可恢复，确定删除吗？`)) return;
    setBusy('delete');
    setActionError('');
    try {
      await deleteWxLayout(layout.id, layout.revision);
      if (activeId.current === id) navigate('/wx-layouts');
    } catch (e) {
      if (activeId.current === id) {
        setActionError((e as Error).message || String(e));
        setBusy(null);
      }
    }
  };

  const copyBtnClass = (state: CopyState) =>
    `inline-flex items-center gap-1.5 text-sm rounded-lg px-4 py-2 border transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed ${
      state === 'ok'
        ? 'bg-emerald-500/15 text-emerald-300 border-emerald-400/25'
        : state === 'failed'
          ? 'bg-red-500/15 text-red-300 border-red-400/25'
          : 'bg-accent/15 text-accent border-accent/40 hover:bg-accent/25'
    }`;

  return (
    <div>
      <button
        onClick={() => navigate('/wx-layouts')}
        className="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 mb-3 cursor-pointer"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> 返回排版草稿箱
      </button>

      <div className="card p-5 mb-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap mb-2">
              <span className="chip text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-400/30">
                可复制
              </span>
              <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border">
                第 {layout.issue_no} 期
              </span>
              <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border">
                {layout.report_date}
              </span>
              {layout.gen_count > 1 && (
                <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border">
                  已生成 {layout.gen_count} 次
                </span>
              )}
              <span className="text-[11px] text-slate-500">
                {layout.updated_at ? `更新于 ${timeAgo(new Date(layout.updated_at))}` : ''}
              </span>
            </div>
            <h2 className="text-lg font-semibold text-slate-100 leading-snug">
              <FileText className="h-4 w-4 inline-block mr-1.5 text-accent" />
              {layout.title}
            </h2>
            {layout.digest && (
              <p className="text-[13px] text-slate-400 mt-1.5 leading-relaxed max-w-3xl">摘要：{layout.digest}</p>
            )}
            <p className="text-[11px] text-slate-500 mt-2">
              点「复制全文」后直接粘贴到公众号后台正文编辑区；开头结尾模板图片会由公众号自动转存，标题/摘要需在后台单独粘贴。
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap shrink-0">
            <button onClick={onCopyTitle} disabled={busy !== null} className={copyBtnClass(titleState)}>
              <Copy className="h-3.5 w-3.5" />
              {titleState === 'ok' ? '标题已复制 ✓' : titleState === 'failed' ? '复制失败 ✕' : '复制标题'}
            </button>
            <button onClick={onCopyAll} disabled={busy !== null} className={copyBtnClass(copyState)}>
              <Copy className="h-3.5 w-3.5" />
              {copyState === 'ok' ? '全文已复制 ✓' : copyState === 'failed' ? '复制失败 ✕' : '复制全文'}
            </button>
            <button
              onClick={() => void onRegenerate()}
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 text-sm bg-radar-panel text-slate-300 border border-radar-border rounded-lg px-4 py-2 hover:border-accent/40 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${busy === 'regen' ? 'animate-spin' : ''}`} />
              {busy === 'regen' ? '重新排版中…' : '重新排版'}
            </button>
            <button
              onClick={() => void onDelete()}
              disabled={busy !== null}
              className="inline-flex items-center gap-1.5 text-sm text-red-300 border border-red-400/25 bg-red-500/5 rounded-lg px-4 py-2 hover:bg-red-500/15 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Trash2 className="h-3.5 w-3.5" /> 删除
            </button>
          </div>
        </div>
        {actionError && (
          <div role="alert" className="mt-3 text-sm text-red-300 border border-red-400/30 bg-red-500/5 rounded-lg px-3 py-2">
            {actionError}
          </div>
        )}
      </div>

      {/* 排版预览：白底文章视口，完全沙箱（无脚本/表单），与站点深色主题隔离 */}
      <div className="card overflow-hidden">
        <div className="px-5 py-3 border-b border-radar-border text-xs text-slate-500">
          排版预览（粘贴到公众号后台后的效果）
        </div>
        <iframe
          title="公众号排版预览"
          srcDoc={html}
          sandbox=""
          className="w-full bg-white"
          style={{ height: '80vh', border: 'none' }}
        />
      </div>
    </div>
  );
}
