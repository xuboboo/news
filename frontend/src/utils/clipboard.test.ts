// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { copyPlain, copyRich, ensureComplianceFooter, renderPlain, renderRich } from './clipboard';

describe('rich text clipboard', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('sanitizes unsafe markup, keeps formatting, and applies inline styles', () => {
    const html = renderRich('# 标题\n\n**加粗** [链接](javascript:alert(1))\n\n<script>alert(2)</script>');
    expect(html).toContain('<h1');
    expect(html).toContain('font-weight:700');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('javascript:');
    const blocked = renderRich('<p onclick="alert(1)">事件属性</p><img src=x><iframe>frame</iframe><form><input></form><video><source></video><audio></audio>');
    expect(blocked).toContain('事件属性');
    expect(blocked).not.toMatch(/onclick|<img|<iframe|<form|<input|<video|<audio/i);
  });

  it('rebuilds canonical source and AI sections while removing alternate headings', () => {
    const result = ensureComplianceFooter(
      '正文\n\n### 参考来源：假条目\n- 假媒体丨《伪造》\n\n## 声明 - 假声明\n假文本',
      [{ source: '可信媒体', title: '真实标题' }],
    );
    expect(result).toContain('- 可信媒体丨《真实标题》');
    expect(result).toContain('本文由 AI 基于公开报道辅助生成');
    expect(result).not.toContain('假媒体');
    expect(result).not.toContain('假文本');
  });

  it('removes nested spoofed source sections without removing later body sections', () => {
    const result = ensureComplianceFooter(
      '正文\n\n## 参考来源\n### 来源补充\n- 假媒体丨《伪造报道》\n\n## 后续正文\n应保留内容',
      [{ source: '可信媒体', title: '真实标题' }],
    );
    expect(result).not.toContain('假媒体');
    expect(result).toContain('应保留内容');
    expect(result).toContain('真实标题');
  });

  it.each(['```', '~~~~'])('preserves headings inside a %s fenced code block', (fence) => {
    const body = `正文\n\n${fence}text\n## 声明\n代码中的内容\n${fence}\n\n应保留的尾段`;
    const result = ensureComplianceFooter(body, []);
    expect(result).toContain(body);
  });

  it('renders readable plain text for rich clipboard without Markdown syntax', () => {
    const plain = renderPlain('# 标题\n\n第一段 **加粗**。\n\n- 条目一\n- 条目二');
    expect(plain).toContain('标题');
    expect(plain).toContain('第一段 加粗。');
    expect(plain).toContain('• 条目一');
    expect(plain).toContain('• 条目二');
    expect(plain).not.toMatch(/(^|\n)#|\*\*/);
    const numbered = renderPlain('3. 第三项\n4. 第四项');
    expect(numbered).toContain('3. 第三项');
    expect(numbered).toContain('4. 第四项');
  });

  it('writes HTML and plain text MIME types through Clipboard API', async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    class Item {
      value: Record<string, Blob>;
      constructor(value: Record<string, Blob>) { this.value = value; }
    }
    vi.stubGlobal('ClipboardItem', Item);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write } });
    await copyRich('<p>正文</p>', '**正文**');
    expect(write).toHaveBeenCalledOnce();
    const item = write.mock.calls[0][0][0] as Item;
    expect(Object.keys(item.value).sort()).toEqual(['text/html', 'text/plain']);
    expect(await item.value['text/plain'].text()).toBe('**正文**');
  });

  it('falls back to execCommand after clipboard rejection and cleans temporary DOM', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { write: vi.fn().mockRejectedValue(new Error('denied')) },
    });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(true) });
    const exec = vi.spyOn(document, 'execCommand');
    await copyRich('<p>安全正文</p>', '安全正文');
    expect(exec).toHaveBeenCalledWith('copy');
    expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  });

  it('uses plain-text Clipboard API when the context is secure', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    await copyPlain('原文 **文本**');
    expect(writeText).toHaveBeenCalledWith('原文 **文本**');
  });

  it('falls back to plain execCommand in an insecure context and cleans textarea', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn() } });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(true) });
    await copyPlain('纯文本');
    expect(document.querySelector('textarea')).toBeNull();
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
  });

  it("restores the user's previous selection after the copy fallback", async () => {
    document.body.innerHTML = '<p id="old-selection">旧选中内容</p>';
    const selection = window.getSelection();
    const oldRange = document.createRange();
    oldRange.selectNodeContents(document.getElementById('old-selection')!);
    selection?.addRange(oldRange);
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn(() => selection?.toString() === '临时正文'),
    });
    await copyRich('<p>临时正文</p>', '临时正文');
    expect(document.execCommand).toHaveReturnedWith(true);
    expect(selection?.toString()).toBe('旧选中内容');
  });

  it('rejects when getSelection is unavailable and removes temporary DOM', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    vi.spyOn(window, 'getSelection').mockReturnValue(null);
    const exec = vi.fn();
    Object.defineProperty(document, 'execCommand', { configurable: true, value: exec });
    await expect(copyRich('<p>正文</p>', '正文')).rejects.toThrow('Selection unavailable');
    expect(exec).not.toHaveBeenCalled();
    expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  });

  it('cleans selection and temporary DOM when execCommand throws', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => { throw new Error('blocked'); }) });
    await expect(copyRich('<p>正文</p>', '正文')).rejects.toThrow('blocked');
    expect(document.querySelector('[contenteditable="true"]')).toBeNull();
    expect(window.getSelection()?.rangeCount).toBe(0);
  });

  it('removes the plain-text fallback textarea when execCommand throws', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => { throw new Error('blocked'); }) });
    await expect(copyPlain('正文')).rejects.toThrow('blocked');
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('rejects when all copy mechanisms fail and still cleans temporary DOM', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn().mockReturnValue(false) });
    await expect(copyRich('<p>正文</p>', '正文')).rejects.toThrow('execCommand copy failed');
    expect(document.querySelector('[contenteditable="true"]')).toBeNull();
  });
});
