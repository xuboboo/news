/**
 * 文章草稿详情：编辑 + 富文本复制 + 重新生成。
 * 生成中每 3s 轮询；就绪后进入编辑态。预览与「复制富文本」共用
 * renderRich 的内联样式渲染（公众号粘贴保排版的同一份 HTML）。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { Link, useBlocker, useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  Check,
  ClipboardCopy,
  ClipboardPaste,
  Copy,
  Eye,
  FileEdit,
  RefreshCw,
  Save,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import { deleteDraft, getDraft, regenerateDraft, updateDraft, type DraftItem } from '../api/drafts';
import { fetchArticleById } from '../api/client';
import { ArticleWithScore } from '../types';
import ArticleModal from '../components/ArticleModal';
import { Loading } from '../components/common';
import { copyPlain, copyRich, ensureComplianceFooter, flashCopyState, renderPlain, renderRich, type CopyState } from '../utils/clipboard';
import { usePageTitle } from '../utils/hooks';
import { fullTime } from '../utils/format';
import { TIER_LABELS } from './Drafts';

const TITLE_LIMIT = 30;
const DIGEST_LIMIT = 120;
const POLL_MS = 3000;

// JS string.length counts UTF-16 code units; Array.from matches Python len(str)
// for Unicode codepoints, including emoji outside the BMP.
const codepointLength = (value: string) => Array.from(value).length;

interface DraftForm {
  title: string;
  digest: string;
  contentMd: string;
}

export default function DraftDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const routeIdRef = useRef(String(id));
  const routeLifetimeRef = useRef(0);
  useLayoutEffect(() => {
    routeIdRef.current = String(id);
    routeLifetimeRef.current += 1;
    return () => { routeLifetimeRef.current += 1; };
  }, [id]);
  usePageTitle('草稿详情');

  // loaded 与路由 id 绑定：loading 派生，effect 内零同步 setState
  const [loaded, setLoaded] = useState<{ id: string; draft: DraftItem | null; error: string } | null>(null);
  const entry = loaded && loaded.id === String(id) ? loaded : null;
  const loading = !entry;
  const draft = entry?.draft ?? null;
  const loadError = entry?.error ?? '';
  const [form, setForm] = useState<DraftForm>({ title: '', digest: '', contentMd: '' });
  const formRef = useRef(form);
  const [dirty, setDirty] = useState(false);
  const blocker = useBlocker(dirty);
  const blockerState = blocker.state;
  const proceedNavigation = blocker.proceed;
  const resetNavigation = blocker.reset;
  useEffect(() => {
    if (blockerState !== 'blocked') return;
    const proceed = window.confirm('草稿有未保存的修改，确定离开并丢弃吗？');
    if (proceed) proceedNavigation?.();
    else resetNavigation?.();
  }, [blockerState, proceedNavigation, resetNavigation]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [busy, setBusy] = useState('');
  const [copyState, setCopyState] = useState<CopyState>(null);
  const [mobileTab, setMobileTab] = useState<'edit' | 'preview'>('edit');
  // form 只在「同一篇草稿的同一代结果」内自由编辑；换草稿/重新生成/状态翻转才重置
  const syncedRef = useRef('');
  const sourceRequestRef = useRef(0);
  const [sourceArticle, setSourceArticle] = useState<{ draftId: number; article: ArticleWithScore } | null>(null);
  const [sourceState, setSourceState] = useState<{
    draftId: number; sourceId: number; loading: boolean; error: string; notFound: boolean;
  } | null>(null);

  useEffect(() => () => {
    sourceRequestRef.current += 1;
  }, [id]);

  const loadSourceArticle = async (sourceId: number) => {
    if (!draft) return;
    const requestId = ++sourceRequestRef.current;
    setSourceArticle(null);
    setSourceState({ draftId: draft.id, sourceId, loading: true, error: '', notFound: false });
    try {
      const article = await fetchArticleById(sourceId);
      if (sourceRequestRef.current === requestId) {
        setSourceArticle({ draftId: draft.id, article });
        setSourceState(null);
      }
    } catch (e) {
      if (sourceRequestRef.current !== requestId) return;
      const error = e as Error & { status?: number };
      setSourceState({
        draftId: draft.id,
        sourceId,
        loading: false,
        error: error.message || String(error),
        notFound: error.status === 404,
      });
    }
  };

  const closeSourceArticle = () => {
    sourceRequestRef.current += 1;
    setSourceArticle(null);
    setSourceState(null);
  };

  // 首次加载与路由 id 切换（setState 仅出现在回调中）
  useEffect(() => {
    let cancelled = false;
    const key = String(id);
    getDraft(Number(id))
      .then((d) => {
        if (!cancelled) {
          setLoaded({ id: key, draft: d, error: '' });
          setBusy('');
          setSaving(false);
          setSaveError('');
        }
      })
      .catch((e: Error & { status?: number }) => {
        if (!cancelled) setLoaded({ id: key, draft: null, error: e.message || String(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const syncKey = draft ? `${draft.id}:${draft.gen_count}:${draft.status}` : '';
  useEffect(() => {
    if (draft && draft.status === 'ready' && syncedRef.current !== syncKey) {
      syncedRef.current = syncKey;
      setForm({ title: draft.title, digest: draft.digest, contentMd: draft.content_md ?? '' });
      setDirty(false);
      setSaveError('');
    }
  }, [draft, syncKey]);

  // 生成中轮询（状态翻转后依赖变化，定时器自动清除）
  const draftId = draft?.id;
  const draftStatus = draft?.status;
  useEffect(() => {
    if (!draftId || draftStatus !== 'generating') return;
    const timer = window.setInterval(() => {
      getDraft(draftId)
        .then((d) => setLoaded((prev) => {
          if (prev?.id !== String(draftId)) return prev;
          const previous = prev.draft;
          if (previous && (d.gen_count < previous.gen_count ||
            (d.gen_count === previous.gen_count && previous.status !== 'generating' && d.status === 'generating'))) {
            return prev;
          }
          return { id: String(draftId), draft: d, error: '' };
        }))
        .catch((e: Error & { status?: number }) => {
          if (e.status === 404) setLoaded((prev) =>
            prev?.id === String(draftId)
              ? { id: String(draftId), draft: null, error: '该草稿已被删除或不存在。' }
              : prev,
          );
        });
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [draftId, draftStatus]);

  useEffect(() => {
    formRef.current = form;
  }, [form]);

  // 浏览器刷新/关闭时提醒未保存正文；页面内重生成和删除另行确认。
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  const currentDraftStatus = draft?.status;
  const publishContentMd = useMemo(
    () => ensureComplianceFooter(form.contentMd, draft?.source_refs ?? []),
    [form.contentMd, draft?.source_refs],
  );
  const previewHtml = useMemo(
    () => (currentDraftStatus === 'ready' ? renderRich(`# ${form.title.trim()}\n\n${publishContentMd}`) : ''),
    [form.title, publishContentMd, currentDraftStatus],
  );

  if (loading) return <Loading />;

  if (loadError || !draft) {
    return (
      <div className="card py-20 text-center">
        <div className="text-sm text-slate-400 mb-4">{loadError || '未找到该草稿'}</div>
        <button
          onClick={() => navigate('/drafts')}
          className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 cursor-pointer"
        >
          返回草稿列表
        </button>
      </div>
    );
  }

  const editForm = (patch: Partial<DraftForm>) => {
    const next = { ...form, ...patch };
    formRef.current = next;
    setForm(next);
    setDirty(next.title !== draft.title || next.digest !== draft.digest ||
      next.contentMd !== (draft.content_md ?? ''));
  };

  const flash = (p: Promise<void>) => flashCopyState(setCopyState, p);
  const copyRichText = () => {
    const plainText = `# ${form.title.trim()}\n\n${publishContentMd}`;
    const operation = copyRich(previewHtml || renderRich(plainText), renderPlain(plainText));
    return flash(operation);
  };
  const copyMarkdown = () => flash(copyPlain(`# ${form.title.trim()}\n\n${publishContentMd}`));
  const copyDigest = () => flash(copyPlain(form.digest));

  const save = async () => {
    const submitted = { ...form };
    const requestedId = String(draft.id);
    if (codepointLength(submitted.title.trim()) > TITLE_LIMIT || codepointLength(submitted.digest.trim()) > DIGEST_LIMIT) {
      setSaveError(`标题最多 ${TITLE_LIMIT} 个字符，摘要最多 ${DIGEST_LIMIT} 个字符`);
      return;
    }
    setSaving(true);
    setSaveError('');
    try {
      const updated = await updateDraft(draft.id, {
        revision: draft.revision,
        title: submitted.title,
        digest: submitted.digest,
        content_md: submitted.contentMd,
      });
      if (routeIdRef.current !== requestedId) return;
      const latest = formRef.current;
      const merged: DraftForm = {
        title: latest.title === submitted.title ? updated.title : latest.title,
        digest: latest.digest === submitted.digest ? updated.digest : latest.digest,
        contentMd: latest.contentMd === submitted.contentMd ? updated.content_md ?? '' : latest.contentMd,
      };
      syncedRef.current = `${updated.id}:${updated.gen_count}:${updated.status}`;
      formRef.current = merged;
      setForm(merged);
      setLoaded({ id: requestedId, draft: updated, error: '' });
      setDirty(merged.title !== updated.title || merged.digest !== updated.digest ||
        merged.contentMd !== (updated.content_md ?? ''));
    } catch (e) {
      const error = e as Error & { status?: number };
      if (routeIdRef.current === requestedId) setSaveError(error.message || String(error));
    } finally {
      setSaving(false);
    }
  };

  const regenerate = async () => {
    if (saving || busy) return;
    if (!window.confirm('重新生成将覆盖当前标题、摘要与正文（包括未保存的修改），确定？')) return;
    const requestedId = String(draft.id);
    setBusy('regen');
    try {
      const updated = await regenerateDraft(draft.id, draft.revision);
      if (routeIdRef.current !== requestedId) return;
      setLoaded({ id: requestedId, draft: updated, error: '' });
      setMobileTab('edit');
    } catch (e) {
      if (routeIdRef.current === requestedId) setSaveError((e as Error).message || String(e));
    } finally {
      setBusy('');
    }
  };

  const remove = async () => {
    if (saving || busy) return;
    if (!window.confirm('删除后不可恢复（包括未保存的修改），确定删除该草稿？')) return;
    const requestedId = String(draft.id);
    const requestedLifetime = routeLifetimeRef.current;
    setBusy('del');
    try {
      await deleteDraft(draft.id, draft.revision);
      if (routeIdRef.current !== requestedId || routeLifetimeRef.current !== requestedLifetime) return;
      flushSync(() => setDirty(false));
      navigate('/drafts');
    } catch (e) {
      if (routeIdRef.current !== requestedId || routeLifetimeRef.current !== requestedLifetime) return;
      setSaveError((e as Error).message || String(e));
      setBusy('');
    }
  };

  const tokens = draft.prompt_tokens + draft.completion_tokens;

  return (
    <div>
      {/* 顶部：返回 + 元信息 + 操作 */}
      <div className="mb-5">
        <Link
          to="/drafts"
          className="inline-flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 mb-4 cursor-pointer"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> 返回文章草稿
        </Link>

        <div className="card p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap mb-2 text-xs text-slate-500">
                <span className="chip text-[10px] bg-radar-panel text-slate-400 border border-radar-border">
                  {TIER_LABELS[draft.tier] ?? draft.tier}
                </span>
                <span>
                  选题：
                  {draft.source_type === 'event' ? (
                    <Link to={`/events/${draft.source_id}`} className="text-accent/90 hover:text-accent">
                      {draft.source_title}
                    </Link>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void loadSourceArticle(draft.source_id)}
                      className="text-accent/90 hover:text-accent cursor-pointer"
                    >
                      {draft.source_title}
                    </button>
                  )}
                </span>
                {draft.created_at && <span>创建于 {fullTime(new Date(draft.created_at))}</span>}
              </div>
              <div className="flex items-center gap-3 flex-wrap text-[11px] text-slate-500">
                {draft.model && <span>模型 {draft.model}</span>}
                {tokens > 0 && <span className="tabular-nums">{tokens.toLocaleString()} tokens</span>}
                {draft.duration_ms > 0 && (
                  <span className="tabular-nums">耗时 {(draft.duration_ms / 1000).toFixed(1)}s</span>
                )}
                <span>第 {draft.gen_count} 次生成</span>
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0 flex-wrap">
              {draft.status === 'ready' && (
                <>
                  {copyState && (
                    <span
                      role="status"
                      aria-live="polite"
                      className={`text-xs ${copyState === 'ok' ? 'text-emerald-300' : 'text-red-300'}`}
                    >
                      {copyState === 'ok' ? '已复制 ✓' : '复制失败 ✕'}
                    </span>
                  )}
                  <button
                    onClick={copyRichText}
                    disabled={!form.contentMd.trim()}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-xs font-medium text-white hover:opacity-90 transition-opacity cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                    title="带排版复制正文，粘贴到公众号富文本编辑器"
                  >
                    <ClipboardCopy className="h-3.5 w-3.5" /> 复制富文本
                  </button>
                  <button
                    onClick={copyMarkdown}
                    disabled={!form.contentMd.trim()}
                    className="chip bg-radar-card text-slate-300 border border-radar-border px-3 py-2 hover:border-accent/40 transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <Copy className="h-3.5 w-3.5 inline mr-1 -mt-0.5" /> 复制 Markdown
                  </button>
                  <button
                    type="button"
                    onClick={() => void save()}
                    disabled={saving || busy !== '' || !dirty}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-xs font-semibold text-white hover:opacity-90 transition-opacity cursor-pointer disabled:opacity-45 disabled:cursor-not-allowed"
                    aria-label={saving ? '保存中' : dirty ? '保存修改' : '已保存'}
                  >
                    {saving ? <Save className="h-3.5 w-3.5 animate-pulse" /> : dirty ? <Save className="h-3.5 w-3.5" /> : <Check className="h-3.5 w-3.5" />}
                    {saving ? '保存中…' : dirty ? '保存修改' : '已保存'}
                  </button>
                </>
              )}
              <button
                type="button"
                onClick={remove}
                disabled={busy !== '' || saving}
                className="chip bg-red-500/10 text-red-300 border border-red-400/25 px-3 py-2 hover:bg-red-500/15 transition-colors cursor-pointer disabled:opacity-50"
                aria-label={`删除草稿「${draft.title || draft.source_title}」`}
              >
                <Trash2 className="h-3.5 w-3.5 inline mr-1 -mt-0.5" /> 删除草稿
              </button>
            </div>
          </div>

          {draft.status === 'generating' && (
            <div className="mt-4 flex items-center gap-3 rounded-xl border border-cyan-400/20 bg-cyan-400/[0.05] px-4 py-3">
              <span className="h-4 w-4 rounded-full border-2 border-accent border-t-transparent animate-spin shrink-0" />
              <span className="text-sm text-slate-300 flex-1">
                AI 正在撰写{draft.gen_count > 1 ? `（第 ${draft.gen_count} 次生成）` : ''}……
                一般需要 20~60 秒，页面会自动刷新
              </span>
              {/* 卡死自救：worker 崩溃/入队丢失时 generating 永不翻转；
                  后端对超时（>15 分钟）的 generating 放行重试，未超时返回 409 提示 */}
              <button
                onClick={regenerate}
                disabled={busy !== '' || saving}
                className="chip bg-radar-card text-slate-400 border border-radar-border px-3 py-1.5 text-xs hover:border-accent/40 transition-colors cursor-pointer disabled:opacity-50 shrink-0"
              >
                <RefreshCw className="h-3.5 w-3.5 inline mr-1 -mt-0.5" /> 卡住了？重新生成
              </button>
            </div>
          )}

          {draft.status === 'failed' && (
            <div className="mt-4 rounded-xl border border-red-400/25 bg-red-500/[0.06] px-4 py-3 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="text-sm text-red-300 font-medium mb-1 flex items-center gap-1.5">
                  <TriangleAlert className="h-4 w-4" /> 生成失败
                </div>
                <div className="text-xs text-slate-400 break-all">{draft.error || '未知错误'}</div>
              </div>
              <button
                onClick={regenerate}
                disabled={busy !== '' || saving}
                className="chip bg-accent/15 text-accent border border-accent/40 px-3 py-2 text-xs shrink-0 cursor-pointer disabled:opacity-50"
              >
                <RefreshCw className="h-3.5 w-3.5 inline mr-1 -mt-0.5" /> 重新生成
              </button>
            </div>
          )}
        </div>
      </div>

      {/* 顶层错误条：generate/保存失败在任何状态都可见（saving 错误在编辑区另有提示位） */}
      {saveError && (
        <div className="card p-4 mb-4 text-sm text-red-300 border border-red-400/30 bg-red-500/5">
          {saveError}
        </div>
      )}

      {sourceState && sourceState.draftId === draft.id && sourceState.sourceId === draft.source_id && (
        <div className="card p-4 mb-4 flex items-center gap-3 text-sm text-slate-300" role="status">
          {sourceState.loading ? (
            <>
              <span className="flex-1">正在加载原始资讯…</span>
              <button type="button" onClick={closeSourceArticle} className="text-slate-400 hover:text-slate-200">
                取消
              </button>
            </>
          ) : (
            <>
              <span className="flex-1">
                {sourceState.notFound
                  ? '原始资讯已清理或不存在，无法查看原文。'
                  : `原始资讯加载失败：${sourceState.error}`}
              </span>
              {!sourceState.notFound && (
                <button
                  type="button"
                  onClick={() => void loadSourceArticle(draft.source_id)}
                  className="chip bg-accent/15 text-accent border border-accent/40 px-3 py-1.5 cursor-pointer"
                >
                  重试
                </button>
              )}
              <button type="button" onClick={closeSourceArticle} className="text-slate-400 hover:text-slate-200">
                关闭
              </button>
            </>
          )}
        </div>
      )}

      {draft.status === 'ready' && (
        <div className="space-y-4">
          {/* 标题与摘要 */}
          <section className="card p-5 space-y-4">
            <div>
              <div className="flex items-center justify-between mb-1.5">
                    <label htmlFor="draft-title" className="text-[11px] font-medium text-slate-400">主标题（公众号发布用）</label>
                <span
                  className={`text-[11px] tabular-nums ${
                    codepointLength(form.title.trim()) > TITLE_LIMIT ? 'text-red-300' : 'text-slate-500'
                  }`}
                >
                  {codepointLength(form.title.trim())}/{TITLE_LIMIT}
                </span>
              </div>
              <input
                id="draft-title"
                value={form.title}
                onChange={(e) => editForm({ title: e.target.value })}
                className="w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-accent/60"
                placeholder="文章标题"
              />
              {draft.alt_titles.length > 0 && (
                <div className="flex items-center gap-2 flex-wrap mt-2">
                  <span className="text-[11px] text-slate-500">备选标题（点击换为主标题）：</span>
                  {draft.alt_titles.map((t, i) => (
                    <button
                      key={i}
                      onClick={() => editForm({ title: t })}
                      aria-pressed={form.title === t}
                      className={`chip text-[11px] border px-2 py-1 cursor-pointer transition-colors ${
                        form.title === t
                          ? 'bg-accent/15 text-accent border-accent/40'
                          : 'bg-radar-panel text-slate-400 border-radar-border hover:text-slate-200'
                      }`}
                    >
                      {t}
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label htmlFor="draft-digest" className="text-[11px] font-medium text-slate-400">
                  摘要（公众号发布栏用，不超过 120 字）
                </label>
                <div className="flex items-center gap-2">
                  <button
                    onClick={copyDigest}
                    className="text-[11px] text-accent/80 hover:text-accent inline-flex items-center gap-1 cursor-pointer"
                  >
                    <ClipboardPaste className="h-3 w-3" /> 复制摘要
                  </button>
                  <span
                    className={`text-[11px] tabular-nums ${
                      codepointLength(form.digest.trim()) > DIGEST_LIMIT ? 'text-red-300' : 'text-slate-500'
                    }`}
                  >
                    {codepointLength(form.digest.trim())}/{DIGEST_LIMIT}
                  </span>
                </div>
              </div>
              <textarea
                id="draft-digest"
                value={form.digest}
                onChange={(e) => editForm({ digest: e.target.value })}
                rows={2}
                className="w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-accent/60 resize-y"
                placeholder="文章摘要"
              />
            </div>
          </section>

          {/* 正文：编辑 + 预览（预览即复制富文本所见的排版） */}
          <section className="card p-5">
            <div className="flex items-center justify-between mb-3">
              <h2 id="draft-body-label" className="text-sm font-semibold text-slate-200">正文</h2>
              <div className="flex items-center gap-2">
                {/* 移动端编辑/预览切换 */}
                <div className="seg lg:hidden" role="group" aria-label="编辑或预览">
                  <button
                    className={`seg-item ${mobileTab === 'edit' ? 'seg-item-active' : ''}`}
                    aria-pressed={mobileTab === 'edit'}
                    onClick={() => setMobileTab('edit')}
                  >
                    <FileEdit className="h-3.5 w-3.5" /> 编辑
                  </button>
                  <button
                    className={`seg-item ${mobileTab === 'preview' ? 'seg-item-active' : ''}`}
                    aria-pressed={mobileTab === 'preview'}
                    onClick={() => setMobileTab('preview')}
                  >
                    <Eye className="h-3.5 w-3.5" /> 预览
                  </button>
                </div>
                <button
                  onClick={regenerate}
                  disabled={busy !== '' || saving}
                  className="chip bg-radar-card text-slate-300 border border-radar-border px-3 py-1.5 text-xs hover:border-accent/40 transition-colors cursor-pointer disabled:opacity-50"
                >
                  <RefreshCw className="h-3.5 w-3.5 inline mr-1 -mt-0.5" /> 重新生成
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <textarea
                id="draft-content"
                aria-labelledby="draft-body-label"
                value={form.contentMd}
                onChange={(e) => editForm({ contentMd: e.target.value })}
                className={`w-full min-h-[480px] rounded-lg border border-radar-border bg-radar-panel px-3.5 py-3 text-[13px] font-mono text-slate-200 leading-relaxed outline-none focus:border-accent/60 resize-y ${
                  mobileTab === 'edit' ? '' : 'hidden lg:block'
                }`}
                placeholder="Markdown 正文"
              />
              <div
                className={`min-h-[480px] rounded-lg border border-radar-border bg-white overflow-y-auto ${
                  mobileTab === 'preview' ? '' : 'hidden lg:block'
                }`}
              >
                {/* 预览白底黑字 = 公众号发布目标观感；HTML 已过 DOMPurify */}
                <div
                  className="px-5 py-4"
                  dangerouslySetInnerHTML={{ __html: previewHtml }}
                />
              </div>
            </div>

            {dirty && (
              <span className="text-[11px] text-amber-300/80">有未保存的修改</span>
            )}

            <div className="flex items-center justify-between mt-4 pt-4 border-t border-radar-border">
              <span className="text-[11px] text-slate-500">
                「复制富文本」粘贴到公众号编辑器可保留标题、加粗和列表；
                网站新闻请使用独立的网站资讯稿板块。
                [配图建议] 占位行需人工配图（公众号屏蔽外链图片）
              </span>
              <span role="status" aria-live="polite" className={`text-xs ${dirty ? 'text-amber-300' : 'text-slate-500'}`}>
                {saving ? '保存中…' : dirty ? '尚有未保存修改，请使用页面顶部“保存修改”' : '所有修改已保存'}
              </span>
            </div>
          </section>
        </div>
      )}
      {sourceArticle && sourceArticle.draftId === draft.id &&
        String(sourceArticle.article.id) === String(draft.source_id) && draft.source_type === 'article' && (
        <ArticleModal
          article={sourceArticle.article}
          onClose={closeSourceArticle}
          onOpenEvent={(eventId) => {
            closeSourceArticle();
            navigate(`/events/${eventId}`);
          }}
        />
      )}
    </div>
  );
}
