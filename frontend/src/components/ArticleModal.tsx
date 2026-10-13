import { X, Eye, MessageSquare, Share2, ThumbsUp, ExternalLink, Link2, Zap, PenSquare, Globe2 } from 'lucide-react';
import { useLayoutEffect, useRef, useState } from 'react';
import { ArticleWithScore, EventStatus } from '../types';
import {
  AiBadge,
  ChannelTag,
  SourceBadge,
  TagChip,
} from './common';
import { dateLabel, formatCount, fullTime, timeAgo } from '../utils/format';
import { safeExternalUrl } from '../utils/url';
import { ARTICLES_WITH_SCORE, eventsForArticle } from '../api/client';
import { createDraft } from '../api/drafts';
import { createSiteDraft } from '../api/siteDrafts';
import { useModalA11y } from '../utils/hooks';

export default function ArticleModal({
  article: propArticle,
  onClose,
  onOpenEvent,
  onOpenDraft,
  onOpenSiteDraft,
}: {
  article: ArticleWithScore;
  onClose: () => void;
  onOpenEvent: (eventId: string) => void;
  /** 点击「生成文章」并成功建稿后回调（缺省 = 不展示入口，如早报页等无路由场景） */
  onOpenDraft?: (draftId: number) => void;
  onOpenSiteDraft?: (draftId: number) => void;
}) {
  // 按.id 重查最新数据：后台 60s 刷新后父页面 state 里的对象快照会过期
  // （阅读数/摘要不更新甚至文章已滚出列表），以最新数据为准
  const article = ARTICLES_WITH_SCORE.find((a) => a.id === propArticle.id) ?? propArticle;
  const relatedEvents = eventsForArticle(article);
  const s = article.summary;
  const hasSummary = s.summary_short.trim() !== '' || s.key_points.length > 0;
  const dialogRef = useModalA11y(onClose);
  // 非 http(s) 的垃圾/恶意 scheme（javascript: 等）不渲染为可点链接（审计整改 F-6）
  const safeUrl = safeExternalUrl(article.url);
  // 记录 mousedown 是否起始于遮罩自身：从弹窗内拖选文本到外部松开不应误关
  const downOnOverlay = useRef(false);
  // AI 生成文章（单篇选题，默认快讯版）：成功即跳转草稿详情
  const [pendingCreations, setPendingCreations] = useState<Record<string, 'wechat' | 'site'>>({});
  const pendingCreationsRef = useRef(new Map<string, number>());
  const mountedRef = useRef(true);
  const [genError, setGenError] = useState<{ id: string; message: string } | null>(null);
  const generationRequest = useRef(0);
  useLayoutEffect(() => {
    mountedRef.current = true;
    generationRequest.current += 1;
    return () => {
      mountedRef.current = false;
      generationRequest.current += 1;
    };
  }, [article.id]);

  const beginCreation = (sourceId: string, channel: 'wechat' | 'site') => {
    if (pendingCreationsRef.current.has(sourceId)) return null;
    const requestId = ++generationRequest.current;
    pendingCreationsRef.current.set(sourceId, requestId);
    setPendingCreations((previous) => ({ ...previous, [sourceId]: channel }));
    setGenError(null);
    return requestId;
  };
  const finishCreation = (sourceId: string, requestId: number) => {
    if (pendingCreationsRef.current.get(sourceId) !== requestId) return;
    pendingCreationsRef.current.delete(sourceId);
    if (!mountedRef.current) return;
    setPendingCreations((previous) => {
      if (!(sourceId in previous)) return previous;
      const next = { ...previous };
      delete next[sourceId];
      return next;
    });
  };

  const generateArticle = async () => {
    if (!onOpenDraft) return;
    const requestedArticleId = article.id;
    const requestId = beginCreation(requestedArticleId, 'wechat');
    if (requestId === null) return;
    try {
      const d = await createDraft('article', Number(requestedArticleId), 'brief');
      if (generationRequest.current === requestId) onOpenDraft(d.id);
    } catch (e) {
      if (generationRequest.current === requestId) setGenError({ id: requestedArticleId, message: (e as Error).message || String(e) });
    } finally {
      finishCreation(requestedArticleId, requestId);
    }
  };

  const generateSiteArticle = async () => {
    if (!onOpenSiteDraft) return;
    const requestedArticleId = article.id;
    const requestId = beginCreation(requestedArticleId, 'site');
    if (requestId === null) return;
    try {
      const d = await createSiteDraft('article', Number(requestedArticleId), 'brief');
      if (generationRequest.current === requestId) onOpenSiteDraft(d.id);
    } catch (e) {
      if (generationRequest.current === requestId) setGenError({ id: requestedArticleId, message: (e as Error).message || String(e) });
    } finally {
      finishCreation(requestedArticleId, requestId);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[rgb(0_0_0/var(--overlay-alpha))] backdrop-blur-sm"
      onMouseDown={(e) => {
        downOnOverlay.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (downOnOverlay.current && e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={article.title}
        className="card modal-scroll w-full max-w-2xl max-h-[85vh] overflow-y-auto outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部：元信息+操作一行，标题独占整行——避免右侧按钮列把标题挤成每行十余字的窄列 */}
        <div className="sticky top-0 bg-radar-card/95 backdrop-blur border-b border-radar-border px-6 py-4 z-10">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mb-2">
            <div className="flex items-center gap-2 flex-wrap min-w-0 flex-1">
              <ChannelTag channel={article.channel} />
              <SourceBadge source={article.source} size="sm" />
              <span className="text-xs text-slate-500">{fullTime(article.publishTime)}</span>
            </div>
            <div className="flex items-center gap-2 flex-wrap justify-end shrink-0">
            {onOpenDraft && (
              <button
                onClick={() => void generateArticle()}
                disabled={!!pendingCreations[article.id]}
                className="inline-flex items-center gap-1.5 text-xs text-accent hover:text-slate-200 border border-accent/30 hover:border-accent/60 rounded-lg px-3 py-1.5 transition-colors"
                title="基于该资讯生成公众号文章草稿"
              >
                <PenSquare className="h-3.5 w-3.5" /> {pendingCreations[article.id] === 'wechat' ? '创建中…' : '生成公众号稿'}
              </button>
            )}
            {onOpenSiteDraft && (
              <button
                onClick={() => void generateSiteArticle()}
                disabled={!!pendingCreations[article.id]}
                className="inline-flex items-center gap-1.5 text-xs text-emerald-300 hover:text-slate-200 border border-emerald-400/30 hover:border-emerald-400/60 rounded-lg px-3 py-1.5 transition-colors"
                title="生成独立的网站资讯稿，包含 SEO 字段"
              >
                <Globe2 className="h-3.5 w-3.5" /> {pendingCreations[article.id] === 'site' ? '创建中…' : '生成网站稿'}
              </button>
            )}
            {safeUrl && (
              <a
                href={safeUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="hidden sm:inline-flex items-center gap-1.5 text-xs text-accent hover:text-slate-200 border border-accent/30 hover:border-accent/60 rounded-lg px-3 py-1.5 transition-colors"
                title={safeUrl}
              >
                <ExternalLink className="h-3.5 w-3.5" /> 阅读原文
              </a>
            )}
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-slate-400 hover:text-slate-200 hover:bg-radar-card"
              aria-label="关闭"
              title="关闭"
            >
              <X className="h-5 w-5" />
            </button>
            </div>
          </div>
          <h2 className="text-lg font-semibold text-slate-100 leading-snug">{article.title}</h2>
        </div>

        <div className="px-6 py-5 space-y-5">
          {genError?.id === article.id && (
            <div role="alert" className="text-xs text-red-300 border border-red-400/25 bg-red-500/5 rounded-lg px-3 py-2">
              {genError.message}
            </div>
          )}
          {/* 元信息 */}
          {/* 元信息：互动芯片有值才显示（阅读/分享暂无公开数据源恒为 0） */}
          <div className="flex items-center gap-5 text-xs text-slate-500">
            {article.views > 0 && (
              <span className="inline-flex items-center gap-1.5">
                <Eye className="h-3.5 w-3.5" /> {formatCount(article.views)}
              </span>
            )}
            {article.comments > 0 && (
              <span className="inline-flex items-center gap-1.5">
                <MessageSquare className="h-3.5 w-3.5" /> {formatCount(article.comments)}
              </span>
            )}
            {article.likes > 0 && (
              <span className="inline-flex items-center gap-1.5">
                <ThumbsUp className="h-3.5 w-3.5" /> {formatCount(article.likes)}
              </span>
            )}
            {article.shares > 0 && (
              <span className="inline-flex items-center gap-1.5">
                <Share2 className="h-3.5 w-3.5" /> {formatCount(article.shares)}
              </span>
            )}
          </div>

          {/* AI 摘要 */}
          <section className="rounded-xl border border-cyan-400/15 bg-cyan-400/[0.04] p-4">
            <div className="flex items-center gap-2 mb-3">
              <AiBadge />
              <span className="text-[11px] text-slate-500">
                {s.model ? `模型 ${s.model}` : '内置抽取式摘要'}
              </span>
            </div>

            {hasSummary ? (
              <>
                <div className="mb-4">
                  <div className="text-[11px] font-medium text-slate-400 mb-1.5">一句话摘要</div>
                  <p className="text-sm text-slate-200 leading-relaxed">{s.summary_short}</p>
                </div>

                {s.key_points.length > 0 && (
                  <div>
                    <div className="text-[11px] font-medium text-slate-400 mb-1.5">核心信息</div>
                    <ul className="space-y-1.5">
                      {s.key_points.map((p, i) => (
                        <li key={i} className="flex gap-2 text-[13px] text-slate-300 leading-relaxed">
                          <span className="mt-[7px] h-1.5 w-1.5 rounded-full bg-cyan-400/70 shrink-0" />
                          {p}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            ) : (
              <p className="text-sm text-slate-500 leading-relaxed">
                暂无 AI 摘要 · AI Worker 将在下一轮处理中自动补充
              </p>
            )}

            {s.tags.length > 0 && (
              <div className="mt-4 flex flex-wrap gap-1.5">
                {s.tags.map((t) => (
                  <TagChip key={t} tag={t} />
                ))}
              </div>
            )}
          </section>

          {/* 关联事件 */}
          {relatedEvents.length > 0 && (
            <section>
              <div className="text-[11px] font-medium text-slate-400 mb-2">关联热点事件</div>
              <div className="space-y-2">
                {relatedEvents.map((e) => (
                  <button
                    key={e.id}
                    onClick={() => onOpenEvent(e.id)}
                    className="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-lg border border-radar-border bg-radar-card hover:border-accent/40 hover:bg-radar-cardHover transition-colors text-left"
                  >
                    <Zap className="h-4 w-4 text-amber-400 shrink-0" />
                    <span className="text-sm text-slate-200 flex-1 min-w-0 truncate">{e.title}</span>
                    <EventStatusTag status={e.status} />
                  </button>
                ))}
              </div>
            </section>
          )}

          {/* 原文 */}
          <div className="flex items-center justify-between pt-1">
            <span className="text-[11px] text-slate-500">
              归档时间 {dateLabel(article.publishTime)} · {timeAgo(article.publishTime)}
            </span>
            {safeUrl ? (
              <a
                href={safeUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs text-accent hover:text-slate-200 border border-accent/30 hover:border-accent/60 rounded-lg px-3 py-1.5 transition-colors"
              >
                <ExternalLink className="h-3.5 w-3.5" /> 阅读原文
              </a>
            ) : article.url ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-slate-500" title={article.url}>
                <Link2 className="h-3.5 w-3.5" /> 原文链接无效
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
                <Link2 className="h-3.5 w-3.5" /> 原文已归档至文章数据库
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function EventStatusTag({ status }: { status: EventStatus }) {
  const style =
    status === '发酵中'
      ? 'bg-red-500/10 text-red-300 border-red-400/25'
      : status === '持续关注'
      ? 'bg-amber-500/10 text-amber-300 border-amber-400/25'
      : 'bg-slate-500/10 text-slate-400 border-slate-400/20';
  return <span className={`chip border ${style}`}>{status}</span>;
}

export { ExternalLink };
