import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { Link, useBlocker, useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, ClipboardCopy, Copy, RefreshCw, Save, Trash2 } from 'lucide-react';
import { deleteSiteDraft, getSiteDraft, regenerateSiteDraft, updateSiteDraft, type SiteDraftItem } from '../api/siteDrafts';
import { copyPlain, copyRich, stripGeneratedSections, flashCopyState, renderPlain, renderRich, type CopyState } from '../utils/clipboard';
import { Loading } from '../components/common';
import { usePageTitle } from '../utils/hooks';

const TEXT_LIMITS = { title: 80, digest: 160, seo_keywords: 160, site_tags: 120, cover_guidance: 240 };
const length = (value: string) => Array.from(value).length;
const inputClass = 'w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2.5 text-sm text-slate-100 outline-none focus:border-accent/60';
const buttonClass = 'inline-flex items-center gap-1.5 rounded-lg border border-radar-border px-3 py-2 text-xs text-slate-300 hover:border-accent/40 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed';

type SiteForm = {
  title: string; digest: string; seo_keywords: string; site_tags: string; cover_guidance: string;
  software_id: string; is_pinned: boolean; is_recommended: boolean; is_headline: boolean;
  publish_at: string; content_md: string;
};
const emptyForm: SiteForm = {
  title: '', digest: '', seo_keywords: '', site_tags: '', cover_guidance: '', software_id: '0',
  is_pinned: false, is_recommended: false, is_headline: false, publish_at: '', content_md: '',
};

/** 后台按北京时间填写，不依赖操作系统时区。 */
function beijingInput(iso: string | null) {
  if (!iso) return '';
  const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(iso) ? iso : `${iso}+00:00`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return '';
  return new Date(date.getTime() + 8 * 3600_000).toISOString().slice(0, 19);
}
function toForm(draft: SiteDraftItem): SiteForm {
  return {
    title: draft.title, digest: draft.digest, seo_keywords: draft.seo_keywords, site_tags: draft.site_tags,
    cover_guidance: draft.cover_guidance, software_id: String(draft.software_id || 0),
    is_pinned: draft.is_pinned, is_recommended: draft.is_recommended, is_headline: draft.is_headline,
    publish_at: beijingInput(draft.publish_at), content_md: draft.content_md ?? '',
  };
}
const differs = (a: SiteForm, b: SiteForm) => (Object.keys(a) as (keyof SiteForm)[]).some((key) => a[key] !== b[key]);

export default function SiteDraftDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  usePageTitle('网站资讯稿详情');
  const [loaded, setLoaded] = useState<{ id: string; draft: SiteDraftItem | null; error: string } | null>(null);
  const entry = loaded?.id === String(id) ? loaded : null;
  const draft = entry?.draft ?? null;
  const [form, setForm] = useState<SiteForm>(emptyForm);
  const formRef = useRef(form);
  const savedRef = useRef(emptyForm);
  const syncedRef = useRef('');
  const [dirty, setDirty] = useState(false);
  const [uncertainSave, setUncertainSave] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [copyState, setCopyState] = useState<CopyState>(null);
  const [mobileTab, setMobileTab] = useState<'edit' | 'preview'>('edit');
  const blocker = useBlocker(dirty);
  const { state: blockerState, proceed, reset } = blocker;
  const routeLife = useRef(0);

  useLayoutEffect(() => {
    const life = ++routeLife.current;
    return () => { if (routeLife.current === life) routeLife.current += 1; };
  }, [id]);
  useEffect(() => {
    const life = routeLife.current;
    getSiteDraft(Number(id)).then((value) => {
      if (routeLife.current !== life) return;
      setLoaded({ id: String(id), draft: value, error: '' });
      setSaving(false); setBusy(''); setError('');
    }).catch((e: Error) => {
      if (routeLife.current === life) setLoaded({ id: String(id), draft: null, error: e.message || String(e) });
    });
  }, [id]);

  const syncKey = draft ? `${draft.id}:${draft.gen_count}:${draft.status}` : '';
  useEffect(() => {
    if (!draft || draft.status !== 'ready' || syncedRef.current === syncKey) return;
    const next = toForm(draft);
    syncedRef.current = syncKey;
    formRef.current = next; savedRef.current = next;
    setForm(next); setDirty(false); setUncertainSave(false); setError('');
  }, [draft, syncKey]);

  const draftId = draft?.id;
  const draftStatus = draft?.status;
  useEffect(() => {
    if (!draftId || draftStatus !== 'generating') return;
    const life = routeLife.current;
    const timer = window.setInterval(() => {
      getSiteDraft(draftId).then((value) => {
        if (routeLife.current !== life) return;
        setLoaded((previous) => {
          const old = previous?.draft;
          if (!old || previous.id !== String(draftId) || value.revision < old.revision || value.gen_count < old.gen_count) return previous;
          return { id: String(draftId), draft: value, error: '' };
        });
        setError('');
      }).catch((e: Error & { status?: number }) => {
        if (routeLife.current !== life) return;
        if (e.status === 404) setLoaded({ id: String(draftId), draft: null, error: '该网站稿已被删除。' });
        else setError(e.message || '生成状态刷新失败，稍后自动重试');
      });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [draftId, draftStatus]);

  useEffect(() => {
    if (blockerState !== 'blocked') return;
    if (window.confirm(uncertainSave
      ? '上次保存结果尚未确认，离开前建议刷新页面核对服务器内容。仍要离开吗？'
      : saving
        ? '网站稿正在保存，离开不会取消已提交的保存请求。确定离开吗？'
        : '网站稿有未保存的修改，确定离开并丢弃吗？')) proceed?.();
    else reset?.();
  }, [blockerState, proceed, reset, saving, uncertainSave]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  // 网站稿正文不含任何参考来源：只清理历史来源小节/声明，不再重建来源尾注。
  const canonicalContent = useMemo(() => stripGeneratedSections(form.content_md), [form.content_md]);
  // CMS有单独标题字段，新闻正文复制不再添加第二个 H1。
  const bodyHtml = useMemo(() => renderRich(canonicalContent), [canonicalContent]);
  const previewHtml = useMemo(() => renderRich(`# ${form.title.trim()}\n\n${canonicalContent}`), [form.title, canonicalContent]);
  const checks = useMemo(() => {
    const result: string[] = [];
    if (!form.title.trim()) result.push('标题尚未填写');
    if (!form.digest.trim()) result.push('内容简介尚未填写');
    if (length(form.title.trim()) > 35) result.push('标题较长，可检查是否能更简洁（不是搜索引擎硬性字数限制）');
    if (/^#\s/m.test(form.content_md)) result.push('正文含一级标题，请确认没有重复 CMS 页面主标题');
    const keywords = form.seo_keywords.split(/[,，、\n]/).map((item) => item.trim()).filter(Boolean);
    if (new Set(keywords).size !== keywords.length) result.push('页面关键字存在重复，请去重');
    if (!form.content_md.trim()) result.push('新闻正文尚未填写');
    return result;
  }, [form]);

  if (!entry) return <Loading />;
  if (!draft || entry.error) return <div className="card py-20 text-center">
    <p className="text-sm text-slate-400 mb-4">{entry.error || '未找到网站资讯稿'}</p>
    <Link to="/site-drafts" className={buttonClass}>返回网站资讯稿</Link>
  </div>;

  const edit = (patch: Partial<SiteForm>) => {
    const next = { ...form, ...patch };
    formRef.current = next;
    setForm(next); setDirty(saving || uncertainSave || differs(next, savedRef.current));
  };
  const flash = (promise: Promise<void>) => flashCopyState(setCopyState, promise);
  const copy = (value: string) => flash(copyPlain(value));
  const formBusy = busy === 'regen' || busy === 'del';

  const save = async () => {
    if (saving || busy) return;
    if (!form.title.trim() || !form.digest.trim() || !form.content_md.trim()) {
      setError('标题、内容简介和新闻正文不能为空'); return;
    }
    for (const key of Object.keys(TEXT_LIMITS) as (keyof typeof TEXT_LIMITS)[]) {
      if (length(form[key].trim()) > TEXT_LIMITS[key]) { setError('字段超过保存上限，请检查红色字数提示'); return; }
    }
    const softwareId = Number(form.software_id);
    if (!/^\d+$/.test(form.software_id) || !Number.isSafeInteger(softwareId) || softwareId > 2147483647) {
      setError('关联软件 ID 必须是有效的非负整数；不关联请填0'); return;
    }
    const publishAt = form.publish_at ? `${form.publish_at}+08:00` : null;
    if (publishAt && Number.isNaN(new Date(publishAt).getTime())) { setError('发布时间格式无效'); return; }
    const submitted = { ...form };
    const life = routeLife.current;
    setSaving(true); setError('');
    try {
      const updated = await updateSiteDraft(draft.id, {
        revision: draft.revision, ...submitted, software_id: softwareId, publish_at: publishAt,
      });
      if (routeLife.current !== life) return;
      const canonical = toForm(updated);
      const latest = formRef.current;
      const merged = { ...canonical };
      for (const key of Object.keys(merged) as (keyof SiteForm)[]) {
        if (latest[key] !== submitted[key]) Object.assign(merged, { [key]: latest[key] });
      }
      syncedRef.current = `${updated.id}:${updated.gen_count}:${updated.status}`;
      formRef.current = merged; savedRef.current = canonical;
      setForm(merged); setUncertainSave(false); setLoaded({ id: String(updated.id), draft: updated, error: '' });
      setDirty(differs(merged, canonical));
    } catch (e) {
      if (routeLife.current === life) {
        const status = (e as Error & { status?: number }).status;
        const outcomeUncertain = status === undefined || status === 0 || status >= 500;
        if (outcomeUncertain && differs(submitted, savedRef.current)) setUncertainSave(true);
        setError((e as Error).message || String(e));
        setDirty(uncertainSave || (outcomeUncertain && differs(submitted, savedRef.current)) || differs(formRef.current, savedRef.current));
      }
    }
    finally { if (routeLife.current === life) setSaving(false); }
  };
  const regenerate = async () => {
    if (saving || busy || !window.confirm('重新生成会覆盖网站标题、简介、关键字、标签、配图建议和正文。发布时间与特殊属性保留，关联软件需重新核对，确定？')) return;
    const life = routeLife.current;
    setBusy('regen'); setError('');
    try {
      const updated = await regenerateSiteDraft(draft.id, draft.revision);
      if (routeLife.current !== life) return;
      setLoaded({ id: String(updated.id), draft: updated, error: '' });
      setMobileTab('edit');
      setDirty(false);
    } catch (e) { if (routeLife.current === life) setError((e as Error).message || String(e)); }
    finally { if (routeLife.current === life) setBusy(''); }
  };
  const remove = async () => {
    if (saving || busy || !window.confirm('删除后不可恢复（包括未保存的修改），确定删除该网站稿？')) return;
    const life = routeLife.current;
    setBusy('del'); setError('');
    try {
      await deleteSiteDraft(draft.id, draft.revision);
      if (routeLife.current !== life) return;
      flushSync(() => setDirty(false));
      navigate('/site-drafts');
    } catch (e) { if (routeLife.current === life) { setError((e as Error).message || String(e)); setBusy(''); } }
  };

  return <div>
    <Link to="/site-drafts" className="inline-flex items-center gap-1.5 text-xs text-slate-500 mb-4"><ArrowLeft className="h-3.5 w-3.5" />返回网站资讯稿</Link>
    <section className="card p-5 mb-4">
      <div className="flex flex-wrap justify-between gap-4">
        <div><h1 className="text-xl font-semibold text-slate-100">网站资讯稿</h1><p className="text-xs text-slate-400 mt-2">选题：{draft.source_title} · 第{draft.gen_count}次生成</p></div>
        <div className="flex items-center gap-2 flex-wrap">
          {draft.status === 'ready' && <>
            <button onClick={() => void save()} disabled={saving || !!busy || !dirty} className={`${buttonClass} bg-accent text-white`}><Save className="h-3.5 w-3.5" />{saving ? '保存中…' : dirty ? '保存修改' : '已保存'}</button>
            <button onClick={() => flash(copyRich(bodyHtml, renderPlain(canonicalContent)))} disabled={!form.content_md.trim()} className={buttonClass}><ClipboardCopy className="h-3.5 w-3.5" />复制新闻正文</button>
            <button onClick={() => copy(canonicalContent)} disabled={!form.content_md.trim()} className={buttonClass}>复制 Markdown 正文</button>
          </>}
          <button onClick={() => void remove()} disabled={saving || !!busy} className={`${buttonClass} text-red-300`}><Trash2 className="h-3.5 w-3.5" />删除草稿</button>
        </div>
      </div>
      <p className="text-xs text-slate-500 mt-3">与公众号草稿独立。按下方字段复制到网站后台；此处保存不会自动发布网站文章。</p>
      {draft.status !== 'ready' && <div className="mt-4 flex items-center justify-between gap-3 text-sm">
        <span className={draft.status === 'failed' ? 'text-red-300' : 'text-cyan-300'}>{draft.status === 'generating' ? 'AI正在生成网站文章，完成后自动显示编辑字段。' : draft.error || '生成失败'}</span>
        <button onClick={() => void regenerate()} disabled={saving || !!busy} className={buttonClass}>{draft.status === 'generating' ? '卡住了？重新生成' : '重新生成'}</button>
      </div>}
    </section>
    {error && <div role="alert" className="card p-4 mb-4 text-sm text-red-300">{error}</div>}
    {copyState && <div role="status" aria-live="polite" className={`mb-3 text-sm ${copyState === 'ok' ? 'text-emerald-300' : 'text-red-300'}`}>{copyState === 'ok' ? '已复制 ✓' : '复制失败，请重试'}</div>}
    {draft.status === 'ready' && <div className="space-y-4">
      <section className="card p-5 space-y-4" aria-label="网站后台字段">
        <Field label="标题" required disabled={formBusy} value={form.title} onChange={(value) => edit({ title: value })} limit={TEXT_LIMITS.title} onCopy={() => copy(form.title)} hint="准确说明事件，主要实体自然出现；80字是本工具保存上限，并非SEO规则。" />
        {draft.alt_titles.length > 0 && <div className="flex gap-2 flex-wrap text-xs"><span className="text-slate-500">备选标题：</span>{draft.alt_titles.map((title, index) => <button key={index} disabled={formBusy} aria-pressed={form.title === title} onClick={() => edit({ title })} className={buttonClass}>{title}</button>)}</div>}
        {(draft.longtail_suggestions?.length ?? 0) > 0 && <div className="rounded-md border border-radar-border p-3 text-xs space-y-2" aria-label="长尾选题建议">
          <div className="flex items-center justify-between gap-2">
            <span className="text-slate-500">长尾承接选题（发布本文后可逐篇另写，互相内链），共 {draft.longtail_suggestions!.length} 条：</span>
            <button disabled={formBusy} onClick={() => copy(draft.longtail_suggestions!.map((item, index) => `${index + 1}. ${item.keyword}｜意图${item.intent}｜${item.form}｜建议标题：${item.title}`).join('\n'))} className={buttonClass}>复制长尾清单</button>
          </div>
          <ol className="list-decimal list-inside space-y-1 text-slate-300">
            {draft.longtail_suggestions!.map((item, index) => (
              <li key={index}>{item.keyword}（意图{item.intent}·{item.form}）建议标题：{item.title}</li>
            ))}
          </ol>
        </div>}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Field label="页面关键字" disabled={formBusy} value={form.seo_keywords} onChange={(value) => edit({ seo_keywords: value })} limit={TEXT_LIMITS.seo_keywords} onCopy={() => copy(form.seo_keywords)} hint="固定3个相关短语，英文逗号分隔；Google不使用meta keywords作为排名依据。" />
          <Field label="标签" disabled={formBusy} value={form.site_tags} onChange={(value) => edit({ site_tags: value })} limit={TEXT_LIMITS.site_tags} onCopy={() => copy(form.site_tags)} hint="用于栏目归类和相关文章，避免建立大量重复标签。" />
        </div>
        <Field label="内容简介" required disabled={formBusy} multiline value={form.digest} onChange={(value) => edit({ digest: value })} limit={TEXT_LIMITS.digest} onCopy={() => copy(form.digest)} hint="独立、准确概括本文。是否输出为网页meta description需要由网站模板确认。" />
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Field label="发布时间（北京时间）" disabled={formBusy} type="datetime-local" value={form.publish_at} onChange={(value) => edit({ publish_at: value })} onCopy={() => copy(form.publish_at.replace('T', ' '))} hint="可留空，到网站后台按实际发布时刻填写；不是事件发生时间。" />
          <Field label="关联软件 ID" disabled={formBusy} value={form.software_id} onChange={(value) => edit({ software_id: value })} onCopy={() => copy(form.software_id)} hint="保持0表示不关联；需从网站后台确认真实软件ID，AI不会猜测。" />
        </div>
        <div><p className="text-xs text-slate-400 mb-2">特殊属性（人工决定）</p><div className="flex gap-5 flex-wrap">
          <Toggle label="置顶" disabled={formBusy} value={form.is_pinned} onChange={(value) => edit({ is_pinned: value })} />
          <Toggle label="推荐" disabled={formBusy} value={form.is_recommended} onChange={(value) => edit({ is_recommended: value })} />
          <Toggle label="头条" disabled={formBusy} value={form.is_headline} onChange={(value) => edit({ is_headline: value })} />
        </div></div>
        <div className="border-t border-radar-border pt-4">
          <p className="text-sm text-amber-300 mb-2">标题图片：发布前必须在网站后台人工上传1:1方图</p>
          <Field label="标题图片构图建议" disabled={formBusy} multiline value={form.cover_guidance} onChange={(value) => edit({ cover_guidance: value })} limit={TEXT_LIMITS.cover_guidance} onCopy={() => copy(form.cover_guidance)} hint="本栏只是建议，不是图片文件。使用有授权、与报道相关的清晰图片，按你截图的后台要求裁成1:1。" />
        </div>
      </section>
      <section className="card p-5">
        <div className="flex items-center justify-between mb-3 gap-3 flex-wrap"><h2 id="site-body-label" className="text-sm font-semibold text-slate-200">新闻正文</h2><div className="flex gap-2">
          <div className="seg lg:hidden" role="group" aria-label="编辑或预览"><button className={`seg-item ${mobileTab === 'edit' ? 'seg-item-active' : ''}`} aria-pressed={mobileTab === 'edit'} onClick={() => setMobileTab('edit')}>编辑</button><button className={`seg-item ${mobileTab === 'preview' ? 'seg-item-active' : ''}`} aria-pressed={mobileTab === 'preview'} onClick={() => setMobileTab('preview')}>预览</button></div>
          <button onClick={() => void regenerate()} disabled={saving || !!busy} className={buttonClass}><RefreshCw className="h-3.5 w-3.5" />重新生成</button>
        </div></div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <textarea aria-labelledby="site-body-label" value={form.content_md} onChange={(e) => edit({ content_md: e.target.value })} disabled={formBusy} className={`${inputClass} min-h-[520px] font-mono text-[13px] leading-relaxed ${mobileTab === 'edit' ? '' : 'hidden lg:block'}`} />
          <div className={`min-h-[520px] rounded-lg border border-radar-border bg-white overflow-y-auto ${mobileTab === 'preview' ? '' : 'hidden lg:block'}`}><div className="px-5 py-4" dangerouslySetInnerHTML={{ __html: previewHtml }} /></div>
        </div>
        <p role="status" aria-live="polite" className="text-xs text-slate-500 mt-3">{uncertainSave ? '上次保存结果尚未确认，请重试保存或刷新页面核对。' : dirty ? '有未保存的修改，请使用顶部保存按钮。' : '所有修改已保存。'}正文复制不包含参考来源，不重复复制主标题。</p>
      </section>
      <section className="card p-5 text-xs text-slate-400 space-y-2" aria-label="发布前检查">
        <h2 className="text-sm font-semibold text-slate-200">发布前检查（不是排名评分）</h2>
        {checks.map((message) => <p key={message} className="text-amber-300">• {message}</p>)}
        <p>• 人工核对事实、标题图片授权、实际发布时间和关联软件。</p>
        <p>• 检查正式网页是否正确输出标题、简介、文章主标题、作者和发布时间；本工具无法修改网站模板。</p>
        <p>• 如需要 NewsArticle 结构化数据、规范网址和站点地图，需在正式网站模板部署，不能只粘贴到正文。</p>
        <p>• SEO（搜索引擎优化）不保证收录或排名，优先提供可靠、有用的文章。</p>
      </section>
    </div>}
  </div>;
}

function Field({ label, value, onChange, onCopy, hint, limit, required, multiline, disabled, type = 'text' }: {
  label: string; value: string; onChange: (value: string) => void; onCopy: () => void;
  hint?: string; limit?: number; required?: boolean; multiline?: boolean; disabled?: boolean; type?: string;
}) {
  const id = useId();
  return <div>
    <div className="flex items-center justify-between mb-1.5 gap-2"><label htmlFor={id} className="text-xs font-medium text-slate-400">{required ? '＊' : ''}{label}</label><div className="flex items-center gap-2">
      {limit !== undefined && <span className={`text-[11px] tabular-nums ${length(value.trim()) > limit ? 'text-red-300' : 'text-slate-500'}`}>{length(value.trim())}/{limit}</span>}
    <button type="button" onClick={onCopy} disabled={!value.trim()} className="text-xs text-accent disabled:opacity-40 cursor-pointer" aria-label={`复制${label}`}><Copy className="h-3 w-3 inline mr-1" />复制</button>
    </div></div>
    {multiline ? <textarea id={id} value={value} onChange={(e) => onChange(e.target.value)} rows={3} required={required} aria-required={required || undefined} disabled={disabled} className={`${inputClass} resize-y`} /> : <input id={id} type={type} step={type === 'datetime-local' ? 1 : undefined} value={value} onChange={(e) => onChange(e.target.value)} required={required} aria-required={required || undefined} disabled={disabled} className={inputClass} />}
    {hint && <p className="text-[11px] text-slate-500 mt-1">{hint}</p>}
  </div>;
}
function Toggle({ label, value, onChange, disabled }: { label: string; value: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <label className="inline-flex items-center gap-2 text-xs text-slate-300 cursor-pointer"><input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} disabled={disabled} />{label}</label>;
}
