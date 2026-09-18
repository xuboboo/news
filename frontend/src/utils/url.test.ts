import { describe, expect, it } from 'vitest';
import { safeExternalUrl } from './url';

/**
 * 外链 scheme 白名单单测（2026-09-18 审计整改 F-6）：
 * 只有 http(s) 绝对地址可渲染为可点链接，危险/缺失 scheme 一律 null。
 */
describe('safeExternalUrl', () => {
  it('放行 http/https（含大小写混排）', () => {
    expect(safeExternalUrl('https://www.ithome.com/1.html')).toBe(
      'https://www.ithome.com/1.html'
    );
    expect(safeExternalUrl('http://a.b/c')).toBe('http://a.b/c');
    expect(safeExternalUrl('HTTPS://A.B')).toBe('HTTPS://A.B');
  });

  it('拦截 javascript:/data:/vbscript: 等危险 scheme', () => {
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull();
    expect(safeExternalUrl('JaVaScRiPt:alert(1)')).toBeNull();
    expect(safeExternalUrl('data:text/html,<script>alert(1)</script>')).toBeNull();
    expect(safeExternalUrl('vbscript:msgbox(1)')).toBeNull();
  });

  it('拦截相对路径/协议相对/空值（防上下文内跳转与伪造）', () => {
    expect(safeExternalUrl('/evil')).toBeNull();
    expect(safeExternalUrl('//evil.example.com/x')).toBeNull();
    expect(safeExternalUrl('')).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl(undefined)).toBeNull();
  });
});
