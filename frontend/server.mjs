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

server.listen(PORT, () => {
  console.log(`[华军科技资讯雷达 V2.0] 已上线: http://localhost:${PORT}`);
});
