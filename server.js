const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const PORT = 3000;

function parseRawRequest(raw) {
  const text = raw.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const blank = text.indexOf('\n\n');
  const headerSection = blank >= 0 ? text.slice(0, blank) : text;
  const body = blank >= 0 ? text.slice(blank + 2) : '';
  const lines = headerSection.split('\n');
  const parts = (lines[0] || '').trim().split(/\s+/);
  const method = parts[0] || 'GET';
  const reqPath = parts[1] || '/';
  const headers = {};
  for (let i = 1; i < lines.length; i++) {
    const colon = lines[i].indexOf(':');
    if (colon > 0) {
      const k = lines[i].slice(0, colon).trim().toLowerCase();
      const v = lines[i].slice(colon + 1).trim();
      if (headers[k]) {
        headers[k] = headers[k] + ', ' + v;
      } else {
        headers[k] = v;
      }
    }
  }
  return { method, path: reqPath, headers, body };
}

function decompress(buf, enc) {
  return new Promise((resolve, reject) => {
    const e = (enc || '').toLowerCase().trim();
    if (e === 'gzip') zlib.gunzip(buf, (err, r) => err ? reject(err) : resolve(r));
    else if (e === 'deflate') zlib.inflate(buf, (err, r) => err ? reject(err) : resolve(r));
    else if (e === 'br') zlib.brotliDecompress(buf, (err, r) => err ? reject(err) : resolve(r));
    else resolve(buf);
  });
}

async function doRequest(host, rawRequest) {
  const p = parseRawRequest(rawRequest);

  let url;
  if (p.path.startsWith('http://') || p.path.startsWith('https://')) {
    url = new URL(p.path);
  } else {
    const base = host.replace(/\/+$/, '');
    url = new URL(base + (p.path.startsWith('/') ? p.path : '/' + p.path));
  }

  const isHttps = url.protocol === 'https:';
  const lib = isHttps ? https : http;

  // Override host header to match target
  const hostHeader = url.hostname + (url.port ? ':' + url.port : '');
  const reqHeaders = { ...p.headers, host: hostHeader };

  // Remove content-encoding/accept-encoding to avoid compressed responses we can't handle
  delete reqHeaders['accept-encoding'];

  const opts = {
    hostname: url.hostname,
    port: url.port ? +url.port : (isHttps ? 443 : 80),
    path: url.pathname + (url.search || ''),
    method: p.method,
    headers: reqHeaders,
    rejectUnauthorized: false,
    timeout: 30000
  };

  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const req = lib.request(opts, async (res) => {
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', async () => {
        try {
          const elapsed = Date.now() - t0;
          const rawBuf = Buffer.concat(chunks);
          const dec = await decompress(rawBuf, res.headers['content-encoding']).catch(() => rawBuf);
          const body = dec.toString('utf-8');

          const headerLines = [`HTTP/${res.httpVersion} ${res.statusCode} ${res.statusMessage}`];
          for (const [k, v] of Object.entries(res.headers)) {
            headerLines.push(`${k}: ${Array.isArray(v) ? v.join(', ') : v}`);
          }
          const rawResponse = headerLines.join('\n') + '\n\n' + body;

          resolve({
            statusCode: res.statusCode,
            statusMessage: res.statusMessage,
            httpVersion: res.httpVersion,
            headers: res.headers,
            body,
            rawResponse,
            bytes: dec.length,
            millis: elapsed,
            redirectUrl: res.headers['location'] || null
          });
        } catch (e) { reject(e); }
      });
      res.on('error', reject);
    });

    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timed out after 30s')); });
    if (p.body) req.write(p.body);
    req.end();
  });
}

const srv = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const urlPath = req.url.split('?')[0];

  const pageRoutes = {
    '/': 'index.html',
    '/index.html': 'index.html',
    '/intruder-positions': 'intruder-positions.html',
    '/intruder-positions.html': 'intruder-positions.html',
    '/intruder-attack': 'intruder-attack.html',
    '/intruder-attack.html': 'intruder-attack.html',
  };

  if (req.method === 'GET' && pageRoutes[urlPath]) {
    try {
      const html = fs.readFileSync(path.join(__dirname, pageRoutes[urlPath]), 'utf-8');
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.writeHead(200);
      res.end(html);
    } catch (e) { res.writeHead(500); res.end('Error loading ' + pageRoutes[urlPath] + ': ' + e.message); }
    return;
  }

  if (req.method === 'POST' && urlPath === '/save-screenshot') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      try {
        const { data, filename } = JSON.parse(body);
        const base64 = data.replace(/^data:image\/png;base64,/, '');
        const buf = Buffer.from(base64, 'base64');
        const outPath = path.join(__dirname, 'screenshots', filename);
        if (!fs.existsSync(path.join(__dirname, 'screenshots'))) {
          fs.mkdirSync(path.join(__dirname, 'screenshots'));
        }
        fs.writeFileSync(outPath, buf);
        res.setHeader('Content-Type', 'application/json');
        res.writeHead(200);
        res.end(JSON.stringify({ saved: outPath }));
        console.log('  📸 Screenshot saved:', outPath);
      } catch (e) {
        res.writeHead(500);
        res.end(JSON.stringify({ error: e.message }));
      }
    });
    return;
  }

  if (req.method === 'POST' && urlPath === '/send') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
      try {
        const { request, host } = JSON.parse(body);
        if (!request || !host) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'Missing request or host' }));
          return;
        }
        const result = await doRequest(host, request);
        res.setHeader('Content-Type', 'application/json');
        res.writeHead(200);
        res.end(JSON.stringify(result));
      } catch (e) {
        res.setHeader('Content-Type', 'application/json');
        res.writeHead(500);
        res.end(JSON.stringify({ error: e.message }));
      }
    });
    return;
  }

  res.writeHead(404);
  res.end('Not found');
});

srv.listen(PORT, () => {
  console.log('\x1b[32m');
  console.log('  ██████╗ ██╗   ██╗██████╗ ██████╗ ███████╗██╗  ██╗ ██████╗ ████████╗');
  console.log('  ██╔══██╗██║   ██║██╔══██╗██╔══██╗██╔════╝██║  ██║██╔═══██╗╚══██╔══╝');
  console.log('  ██████╔╝██║   ██║██████╔╝██████╔╝███████╗███████║██║   ██║   ██║   ');
  console.log('  ██╔══██╗██║   ██║██╔══██╗██╔═══╝ ╚════██║██╔══██║██║   ██║   ██║   ');
  console.log('  ██████╔╝╚██████╔╝██║  ██║██║     ███████║██║  ██║╚██████╔╝   ██║   ');
  console.log('  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚═╝   ');
  console.log('\x1b[0m');
  console.log(`  \x1b[1mBurpShot\x1b[0m  →  \x1b[36mhttp://localhost:${PORT}\x1b[0m`);
  console.log('  Press Ctrl+C to stop\n');
});
