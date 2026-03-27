import { createServer } from 'node:http';
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

/**
 * Start a static file server for web/dist/.
 * @param {string} distDir - Absolute path to web/dist/
 * @param {number} port - Port to listen on
 * @returns {Promise<boolean>} true if started, false if already running or failed
 */
export async function startUiServer(distDir, port) {
  if (_server) return false;

  // Check if dist dir exists
  try {
    await stat(join(distDir, 'index.html'));
  } catch {
    return false;
  }

  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      try {
        let urlPath = new URL(req.url, `http://localhost:${port}`).pathname;
        if (urlPath === '/') urlPath = '/index.html';

        const filePath = join(distDir, urlPath);

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
