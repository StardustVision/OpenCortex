import { createServer, request as httpRequest } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { join, extname } from 'node:path';

const MIME_TYPES = {
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
};

let _server = null;
let _apiTarget = 'http://127.0.0.1:8921';

/**
 * Proxy /api/* requests to the backend HTTP server.
 */
function proxyApiRequest(req, res) {
  const url = new URL(_apiTarget);
  const opts = {
    hostname: url.hostname,
    port: url.port,
    path: req.url,
    method: req.method,
    headers: { ...req.headers, host: `${url.hostname}:${url.port}` },
  };
  const proxy = httpRequest(opts, (proxyRes) => {
    res.writeHead(proxyRes.statusCode, proxyRes.headers);
    proxyRes.pipe(res, { end: true });
  });
  proxy.on('error', () => {
    res.writeHead(502);
    res.end('Bad Gateway');
  });
  req.pipe(proxy, { end: true });
}

/**
 * Start a static file server for web/dist/ with /api proxy.
 * @param {string} distDir - Absolute path to web/dist/
 * @param {number} port - Port to listen on
 * @param {string} [apiUrl] - Backend API URL to proxy /api/* to
 * @returns {Promise<boolean>} true if started, false if already running or failed
 */
export async function startUiServer(distDir, port, apiUrl) {
  if (_server) return false;
  if (apiUrl) _apiTarget = apiUrl;

  // Check if dist dir exists
  try {
    await stat(join(distDir, 'index.html'));
  } catch {
    return false;
  }

  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      try {
        const urlPath = new URL(req.url, `http://localhost:${port}`).pathname;

        // Proxy /api/* to backend
        if (urlPath.startsWith('/api/')) {
          proxyApiRequest(req, res);
          return;
        }

        let servePath = urlPath;
        if (servePath === '/') servePath = '/index.html';

        const filePath = join(distDir, servePath);

        // Security: prevent path traversal
        if (!filePath.startsWith(distDir)) {
          res.writeHead(403);
          res.end('Forbidden');
          return;
        }

        try {
          const fileStat = await stat(filePath);
          if (fileStat.isFile()) {
            const ext = extname(filePath);
            const contentType = MIME_TYPES[ext] || 'application/octet-stream';
            const content = await readFile(filePath);
            res.writeHead(200, { 'Content-Type': contentType });
            res.end(content);
            return;
          }
        } catch {
          // File not found — fall through to SPA fallback
        }

        // SPA fallback: serve index.html for all non-file routes
        const indexPath = join(distDir, 'index.html');
        const content = await readFile(indexPath);
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(content);
      } catch (err) {
        res.writeHead(500);
        res.end('Internal Server Error');
      }
    });

    server.on('error', (err) => {
      if (err.code === 'EADDRINUSE') {
        // Port already in use — another instance may be running
        resolve(false);
      } else {
        resolve(false);
      }
    });

    server.listen(port, '127.0.0.1', () => {
      _server = server;
      resolve(true);
    });
  });
}

/**
 * Stop the UI server if running.
 */
export function stopUiServer() {
  if (_server) {
    _server.close();
    _server = null;
  }
}
