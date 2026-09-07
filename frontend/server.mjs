/**
 * 华军科技资讯雷达 V2.0 — 生产静态服务器
 * 用法：node server.mjs [端口，默认 8080]
 */
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
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

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);

    // /api 反向代理：透传方法/头/查询串/请求体，响应整体回写
    if (url.pathname.startsWith('/api/')) {
      const headers = new Headers();
      for (const [key, value] of Object.entries(req.headers)) {
        if (!['host', 'connection', 'content-length', 'transfer-encoding'].includes(key)) {
          headers.set(key, Array.isArray(value) ? value.join(', ') : value);
        }
      }
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
      const outHeaders = {};
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
    if (!file.startsWith(ROOT)) {
      res.writeHead(403).end('Forbidden');
      return;
    }
    const s = await stat(file).catch(() => null);
    const target = s?.isFile() ? file : join(ROOT, 'index.html');
    const data = await readFile(target);
    res.writeHead(200, {
      'Content-Type': MIME[extname(target)] ?? 'application/octet-stream',
      'Cache-Control': target.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable',
    });
    res.end(data);
  } catch (err) {
    res.writeHead(500).end('Internal Server Error');
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[华军科技资讯雷达 V2.0] 已上线: http://localhost:${PORT}（监听 ${HOST}，/api → ${API_TARGET}）`);
});
