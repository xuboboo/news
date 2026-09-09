/**
 * 华军科技资讯雷达 V2.0 — 生产静态服务器
 * 用法：node server.mjs [端口，默认 8080]
 */
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, relative, sep } from 'node:path';
import { gzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), 'dist');
const PORT = Number(process.argv[2] ?? 8080);
// 监听地址：默认 0.0.0.0 保持历史行为（局域网可访问）；仅本机预览时可用 HOST=127.0.0.1 收敛暴露面
const HOST = process.env.HOST ?? '0.0.0.0';
// /api 反向代理目标（本机 compose 后端直连口）；预览站借此展示真实数据，
// 避免 SPA fallback 把 HTML 当 API 应答（200 + text/html）静默落入演示模式
const API_TARGET = process.env.API_TARGET ?? 'http://127.0.0.1:18000';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.json': 'application/json; charset=utf-8',
  '.woff2': 'font/woff2',
};

const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'SAMEORIGIN',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
};

// gzip 结果按「文件路径 + mtime」缓存：构建产物文件名带 hash 天然去重，
// index.html 这类会变的文件靠 mtime 失效，超量时简单淘汰最早条目
const gzipCache = new Map(); // 纯 JS 运行（.mjs），不得使用 TS 语法
function gzipFor(file, mtimeMs, data) {
  const hit = gzipCache.get(file);
  if (hit && hit.mtimeMs === mtimeMs) return hit.data;
  const gz = gzipSync(data, { level: 6 });
  gzipCache.set(file, { mtimeMs, data: gz });
  if (gzipCache.size > 64) {
    const oldest = gzipCache.keys().next().value;
    if (oldest) gzipCache.delete(oldest);
  }
  return gz;
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);

    // /api 反向代理：透传方法/头/查询串/请求体，注入真实来源信息，响应整体回写
    if (url.pathname.startsWith('/api/')) {
      const headers = new Headers();
      for (const [key, value] of Object.entries(req.headers)) {
        // 剥离 hop-by-hop 头与长度/编码类头（fetch 会自行处理）
        if (
          ![
            'host',
            'connection',
            'keep-alive',
            'transfer-encoding',
            'upgrade',
            'content-length',
            'te',
            'trailer',
          ].includes(key)
        ) {
          headers.set(key, Array.isArray(value) ? value.join(', ') : value);
        }
      }
      // 后端日志/限流需要真实客户端信息
      const remote = req.socket.remoteAddress ?? '';
      headers.set('x-forwarded-for', remote);
      headers.set('x-forwarded-proto', 'http');
      if (req.headers.host) headers.set('x-forwarded-host', String(req.headers.host));

      let upstream;
      try {
        upstream = await fetch(`${API_TARGET}${req.url}`, {
          method: req.method,
          headers,
          body: req.method === 'GET' || req.method === 'HEAD' ? undefined : req,
          duplex: 'half',
        });
      } catch {
        res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ detail: '后端不可达（API_TARGET 连接失败）' }));
        return;
      }
      // fetch 已自动解压 gzip/br，转发前必须去掉编码与长度头，避免响应体错位
      const outHeaders = { ...SECURITY_HEADERS };
      upstream.headers.forEach((value, key) => {
        if (!['content-encoding', 'content-length', 'transfer-encoding'].includes(key)) {
          outHeaders[key] = value;
        }
      });
      res.writeHead(upstream.status, outHeaders);
      res.end(Buffer.from(await upstream.arrayBuffer()));
      return;
    }

    let path = normalize(decodeURIComponent(url.pathname)).replace(/^[/\\]+/, '');
    if (path === '' || !extname(path)) path = 'index.html';
    const file = join(ROOT, path);
    // 路径逃逸校验：relative 必须落在 ROOT 内（startsWith 会被 distX 这类兄弟目录名绕过）
    const rel = relative(ROOT, file);
    if (rel.startsWith('..') || rel === '') {
      res.writeHead(403).end('Forbidden');
      return;
    }
    const s = await stat(file).catch(() => null);
    if (!s?.isFile()) {
      // 带扩展名的缺失文件按 404 处理：把 HTML 当 JS/CSS 下发只会掩盖断链问题
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Not Found');
      return;
    }
    const data = await readFile(file);
    const immutable = rel.startsWith(`assets${sep}`) || rel.startsWith('assets/');
    const headers = {
      'Content-Type': MIME[extname(file)] ?? 'application/octet-stream',
      // index.html 与无 hash 的 public 文件（如 radar.svg）禁止长缓存，部署后即时生效
      'Cache-Control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache',
      ...SECURITY_HEADERS,
    };
    // 客户端支持 gzip 且体积达标时压缩传输
    const acceptsGzip = String(req.headers['accept-encoding'] ?? '').includes('gzip');
    if (acceptsGzip && data.length >= 1024 && !extname(file).startsWith('.png') && !extname(file).startsWith('.jpg')) {
      const gz = gzipFor(file, s.mtimeMs, data);
      headers['Content-Encoding'] = 'gzip';
      headers['Vary'] = 'Accept-Encoding';
      headers['Content-Length'] = String(gz.length);
      res.writeHead(200, headers);
      res.end(gz);
    } else {
      headers['Content-Length'] = String(data.length);
      res.writeHead(200, headers);
      res.end(data);
    }
  } catch (err) {
    console.error('[server] 请求处理失败:', err);
    res.writeHead(500).end('Internal Server Error');
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[华军科技资讯雷达 V2.0] 已上线: http://localhost:${PORT}（监听 ${HOST}，/api → ${API_TARGET}）`);
});
