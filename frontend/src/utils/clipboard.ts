/**
 * 剪贴板工具：富文本（text/html + text/plain 双 MIME）与纯文本复制。
 *
 * 富文本是「复制发布」链路的关键：微信公众号后台编辑器与常见 CMS 富文本框
 * 粘贴时读 text/html 保留排版；仅写 text/plain 会丢掉全部加粗/标题/列表。
 * 渲染侧（renderRich）把 Markdown 转成「内联样式 HTML」——公众号编辑器粘贴时
 * 会剥离 <style> 标签与 class，只认 inline style（mdnice 同款原理），
 * 因此复制主题使用固定的绝对颜色（白底黑字），不随站点深色主题变化。
 */
import { marked } from 'marked';
import DOMPurify from 'dompurify';

// 「华军蓝」复制主题：粘贴目标永远是白底文章页，颜色写死不引用 CSS 变量
const ACCENT = '#1e6fff';
const STYLES: Record<string, string> = {
  h1: `font-size:18px;font-weight:700;color:#111;margin:24px 0 14px;`,
  h2: `font-size:17px;font-weight:700;color:#111;border-left:4px solid ${ACCENT};padding-left:10px;margin:26px 0 14px;line-height:1.4;`,
  h3: `font-size:15.5px;font-weight:700;color:#222;margin:20px 0 10px;`,
  p: `font-size:15px;color:#333;line-height:1.8;margin:0 0 16px;text-align:justify;letter-spacing:0.3px;`,
  strong: `font-weight:700;color:#111;`,
  em: `font-style:italic;color:#555;`,
  blockquote: `border-left:3px solid #c9d4e3;background:#f6f8fa;padding:10px 14px;margin:0 0 16px;color:#555;font-size:14px;line-height:1.8;`,
  ul: `margin:0 0 16px;padding-left:22px;`,
  ol: `margin:0 0 16px;padding-left:22px;`,
  li: `font-size:15px;color:#333;line-height:1.8;margin:0 0 6px;`,
  a: `color:${ACCENT};text-decoration:none;border-bottom:1px solid rgba(30,111,255,.35);`,
  code: `background:#f2f4f7;color:#c7254e;padding:2px 5px;border-radius:3px;font-size:13px;`,
  pre: `background:#f6f8fa;padding:12px;border-radius:6px;overflow-x:auto;margin:0 0 16px;`,
  hr: `border:none;border-top:1px solid #e5e7eb;margin:24px 0;`,
  table: `border-collapse:collapse;width:100%;margin:0 0 16px;font-size:14px;`,
  th: `border:1px solid #e5e7eb;background:#f6f8fa;padding:6px 10px;color:#111;text-align:left;`,
  td: `border:1px solid #e5e7eb;padding:6px 10px;color:#333;`,
};

// 配图建议占位行：黄底虚线框，编辑在后台看到即知此处需人工插图
// （公众号防盗链屏蔽外链图片，图片必须编辑手动上传）
const IMG_HINT_RE = /^\[配图建议[：:]/;
const IMG_HINT_STYLE = `display:block;font-size:13px;color:#9a6b00;background:#fdf6e3;border:1px dashed #e0c36a;border-radius:6px;padding:8px 12px;margin:0 0 16px;text-align:center;`;

function applyInlineStyles(root: HTMLElement): void {
  root.querySelectorAll<HTMLElement>('*').forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const isImgHint = tag === 'p' && IMG_HINT_RE.test((el.textContent ?? '').trim());
    const extra = isImgHint ? IMG_HINT_STYLE : STYLES[tag];
    if (extra) el.setAttribute('style', extra);
  });
}

export interface SourceReference {
  source: string;
  title: string;
}

const AI_DISCLOSURE_HEADING_RE = /^(?:AI|AIGC|人工智能)\s*(?:辅助)?\s*(?:生成(?:内容)?|创作)?\s*(?:声明|说明)/i;
const LEGACY_AI_DISCLOSURE_LINE_RE = /^[ \t]*(?:本文|本稿)由[ \t]*(?:AI|AIGC|人工智能)[ \t]*(?:基于公开报道[ \t]*)?(?:辅助)?(?:生成|整理撰写|撰写|创作)[^\r\n]*$/i;

/** 复制/预览时仅依据服务器保存的来源快照重建不可伪造的来源尾注。 */
export function ensureSourceFooter(contentMd: string, sourceRefs: SourceReference[]): string {
  const lines: string[] = [];
  let skippingLevel = 0;
  let fenceMarker = '';
  let fenceLength = 0;
  for (const line of (contentMd ?? '').split(/\r?\n/)) {
    if (fenceMarker) {
      if (!skippingLevel) lines.push(line);
      if (new RegExp(`^[ \\t]{0,3}${fenceMarker}{${fenceLength},}[ \\t]*$`).test(line)) fenceMarker = '';
      continue;
    }
    const fence = line.match(/^[ \t]{0,3}(`{3,}|~{3,})(.*)$/);
    if (fence && !(fence[1][0] === '`' && fence[2].includes('`'))) {
      fenceMarker = fence[1][0];
      fenceLength = fence[1].length;
      if (!skippingLevel) lines.push(line);
      continue;
    }
    const heading = line.match(/^[ \t]{0,3}(#{1,6})[ \t]*(.+?)\s*$/);
    if (heading) {
      const level = heading[1].length;
      if (skippingLevel && level > skippingLevel) continue;
      skippingLevel = 0;
      const label = heading[2].trim();
      if (label.startsWith('参考来源') || label.startsWith('声明') || AI_DISCLOSURE_HEADING_RE.test(label)) {
        skippingLevel = level;
        continue;
      }
    }
    if (!skippingLevel) {
      const label = line.trim().replace(/^[*_]+|[*_]+$/g, '');
      const aiLabel = label.match(AI_DISCLOSURE_HEADING_RE);
      const labelledDisclosure = aiLabel && /^[：:]/.test(label.slice(aiLabel[0].length).trimStart());
      if (!LEGACY_AI_DISCLOSURE_LINE_RE.test(label) && !labelledDisclosure) lines.push(line);
    }
  }
  const body = lines.join('\n').trimEnd();
  const refs = sourceRefs
    .filter((ref) => ref.title.trim())
    .map((ref) => `- ${ref.source.replace(/[\r\n]+/g, ' ').trim()}丨《${ref.title.replace(/[\r\n]+/g, ' ').trim()}》`);
  const sources = refs.length ? `\n\n## 参考来源\n${refs.join('\n')}` : '';
  return `${body}${sources}`.trim();
}

/** Markdown → 消毒后的内联样式 HTML（预览与富文本复制共用同一渲染）。 */
export function renderRich(md: string): string {
  const raw = marked.parse(md ?? '', { async: false }) as string;
  // 设计口径是纯文字 + [配图建议] 占位：外链图片在公众号必然被防盗链剥离、
  // 在预览里只会是裂图，iframe/style/form 超出文章语义——消毒层直接禁除
  const clean = DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['img', 'style', 'iframe', 'form', 'input', 'video', 'audio'],
  });
  const doc = new DOMParser().parseFromString(`<div id="__rw_root">${clean}</div>`, 'text/html');
  const root = doc.getElementById('__rw_root');
  if (!root) return '';
  applyInlineStyles(root);
  // 外层 section 携带基础排版（字号/行高/字距），整体包裹是公众号粘贴的惯例
  return `<section style="font-size:15px;color:#333;line-height:1.8;letter-spacing:0.3px;word-break:break-word;">${root.innerHTML}</section>`;
}

/** Markdown → 可读纯文本：用于富文本剪贴板的 text/plain MIME。 */
export function renderPlain(md: string): string {
  const doc = new DOMParser().parseFromString(renderRich(md), 'text/html');
  const chunks: string[] = [];
  const blockTags = new Set(['section', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'ul', 'ol', 'table']);
  const listStack: Array<{ ordered: boolean; value: number }> = [];
  const appendBreak = (count: number) => {
    if (!chunks.length) return;
    chunks.push('\n'.repeat(count));
  };
  const visit = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      chunks.push(node.nodeValue ?? '');
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      node.childNodes.forEach(visit);
      return;
    }
    const element = node as HTMLElement;
    const tag = element.tagName.toLowerCase();
    if (tag === 'br') {
      chunks.push('\n');
      return;
    }
    if (blockTags.has(tag)) appendBreak(tag === 'section' ? 0 : 2);
    if (tag === 'ol' || tag === 'ul') {
      const start = Number(element.getAttribute('start'));
      listStack.push({ ordered: tag === 'ol', value: Number.isSafeInteger(start) && start > 0 ? start - 1 : 0 });
    }
    if (tag === 'li') {
      appendBreak(1);
      const list = listStack[listStack.length - 1];
      if (list?.ordered) {
        const explicitValue = Number(element.getAttribute('value'));
        list.value = Number.isSafeInteger(explicitValue) && explicitValue > 0 ? explicitValue : list.value + 1;
        chunks.push(`${list.value}. `);
      } else {
        chunks.push('• ');
      }
    }
    element.childNodes.forEach(visit);
    if (tag === 'td' || tag === 'th') chunks.push('\t');
    if (blockTags.has(tag)) appendBreak(tag === 'section' ? 0 : 2);
    else if (tag === 'li' || tag === 'tr') appendBreak(1);
    if (tag === 'ol' || tag === 'ul') listStack.pop();
  };
  doc.body.childNodes.forEach(visit);
  return chunks.join('').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

/**
 * 富文本复制：优先 Clipboard API 双 MIME 写入（HTTPS / localhost），
 * 失败或非安全上下文（局域网 HTTP 访问）回退隐藏 contenteditable + execCommand。
 * plainText 作为 text/plain 兜底（不支持 HTML 的目标仍可粘贴纯文本）。
 */
export async function copyRich(html: string, plainText: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext && typeof ClipboardItem !== 'undefined') {
    try {
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([plainText], { type: 'text/plain' }),
        }),
      ]);
      return;
    } catch {
      /* 权限拒绝/浏览器不支持双 MIME：走 execCommand 兜底 */
    }
  }
  const container = document.createElement('div');
  container.contentEditable = 'true';
  container.style.position = 'fixed';
  container.style.opacity = '0';
  container.innerHTML = html;
  const onCopy = (event: ClipboardEvent) => {
    if (!event.clipboardData) return; // 旧浏览器让 execCommand 默认复制所选 HTML/纯文本
    event.preventDefault();
    event.clipboardData.setData('text/html', html);
    event.clipboardData.setData('text/plain', plainText);
  };
  container.addEventListener('copy', onCopy);
  document.body.appendChild(container);
  const selection = window.getSelection();
  const previousRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange())
                .filter((range) => range.commonAncestorContainer.isConnected && !range.collapsed)
    : [];
  let ok: boolean;
  try {
    if (!selection) throw new Error('Selection unavailable for copy fallback');
    selection.removeAllRanges();
    const range = document.createRange();
    range.selectNodeContents(container);
    selection.addRange(range);
    ok = document.execCommand('copy');
  } finally {
    selection?.removeAllRanges();
    for (const range of previousRanges) {
      try { selection?.addRange(range); } catch { /* 旧选区节点可能已被页面移除 */ }
    }
    container.remove();
  }
  if (!ok) throw new Error('execCommand copy failed');
}

/** 纯文本复制（复制 Markdown 用）：Clipboard API + textarea/execCommand 兜底。 */
export async function copyPlain(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      /* 落兜底 */
    }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  let ok: boolean;
  try {
    ta.select();
    ok = document.execCommand('copy');
  } finally {
    ta.remove();
  }
  if (!ok) throw new Error('execCommand copy failed');
}

/** 复制状态的三态提示（2s 自动复位），与早报页 copyState 同款。 */
export type CopyState = 'ok' | 'failed' | null;

export function flashCopyState(
  set: (s: CopyState) => void,
  promise: Promise<void>,
  timeout = 2000,
): void {
  promise
    .then(() => set('ok'))
    .catch(() => set('failed'))
    .finally(() => window.setTimeout(() => set(null), timeout));
}
