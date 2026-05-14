'use strict';

const express = require('express');
const cors = require('cors');
const path = require('path');
const { spawn, execSync } = require('child_process');
const fs = require('fs');

const app = express();

app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Serve the built React app
app.use(express.static(path.join(__dirname, 'public')));

// ─── Helpers ───────────────────────────────────────────────────────────────

function findPython() {
  for (const cmd of ['python3', 'python']) {
    try { execSync(`${cmd} --version`, { stdio: 'pipe' }); return cmd; }
    catch { continue; }
  }
  return null;
}

// ─── Health ────────────────────────────────────────────────────────────────

app.get('/api/healthz', (_req, res) => {
  res.json({ status: 'ok' });
});

// ─── IPTC helpers ──────────────────────────────────────────────────────────

const IMG_GET2_BASE = 'https://alphapress.photoshelter.com/img-get2';

const FETCH_HEADERS = {
  'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  Accept: 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
  'Accept-Language': 'en-GB,en;q=0.9',
  Referer: 'https://alphapress.photoshelter.com/',
};

const IPTC_FIELDS = {
  5: 'title', 15: 'category', 20: 'supplementalCategory', 25: 'keywords',
  40: 'specialInstructions', 55: 'dateCreated', 60: 'timeCreated',
  80: 'author', 85: 'authorTitle', 90: 'city', 101: 'province',
  103: 'jobRef', 110: 'credit', 115: 'source', 116: 'copyright',
  120: 'caption', 122: 'captionWriter',
};
const IPTC_REPEATABLE = new Set([25, 20]);

function extractIptc(buf) {
  const marker = Buffer.from([0x38, 0x42, 0x49, 0x4d, 0x04, 0x04]);
  const idx = buf.indexOf(marker);
  if (idx < 0) return {};
  const nameLenByte = buf[idx + 6];
  const paddedNameLen = nameLenByte % 2 === 0 ? nameLenByte + 1 : nameLenByte;
  const off = idx + 6 + 1 + paddedNameLen;
  const iptcLen = buf.readUInt32BE(off);
  const iptc = buf.subarray(off + 4, off + 4 + iptcLen);
  const raw = {};
  let i = 0;
  while (i < iptc.length) {
    if (iptc[i] !== 0x1c) { i++; continue; }
    const record = iptc[i + 1];
    const dataset = iptc[i + 2];
    const length = iptc.readUInt16BE(i + 3);
    const value = iptc.subarray(i + 5, i + 5 + length).toString('latin1');
    if (record === 2) { if (!raw[dataset]) raw[dataset] = []; raw[dataset].push(value); }
    i += 5 + length;
  }
  const meta = {};
  for (const [dsStr, values] of Object.entries(raw)) {
    const ds = Number(dsStr);
    const field = IPTC_FIELDS[ds];
    if (!field) continue;
    meta[field] = IPTC_REPEATABLE.has(ds) ? values : (values[0] ?? '');
  }
  return meta;
}

function formatDate(yyyymmdd) {
  if (yyyymmdd.length !== 8) return yyyymmdd;
  return `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6)}`;
}

function safeFilename(s) {
  return s.replace(/[^\w\- ]/g, '').trim().replace(/\s+/g, '_');
}

// ─── Metadata ──────────────────────────────────────────────────────────────

app.get('/api/metadata/:id', async (req, res) => {
  const { id } = req.params;
  if (!id || !/^I[0-9A-Za-z]{14,}$/.test(id)) return res.status(400).json({ error: 'Invalid image ID' });
  const url = `${IMG_GET2_BASE}/${id}/crop=999999x2040/image.jpg`;
  try {
    const resp = await fetch(url, { headers: FETCH_HEADERS });
    if (!resp.ok) return res.status(resp.status).json({ error: `Upstream returned ${resp.status}` });
    const buf = Buffer.from(await resp.arrayBuffer());
    const raw = extractIptc(buf);
    if (Object.keys(raw).length === 0) return res.status(404).json({ error: 'No IPTC metadata found' });
    const keywords = Array.isArray(raw.keywords) ? raw.keywords.join('; ') : raw.keywords ?? null;
    const supplementalCategory = Array.isArray(raw.supplementalCategory) ? raw.supplementalCategory.join('; ') : raw.supplementalCategory ?? null;
    const str = (k) => raw[k] ?? null;
    const title = str('title'), jobRef = str('jobRef');
    let outputFilename = `${id}_clean.jpg`;
    if (jobRef && title) outputFilename = `${jobRef}_${safeFilename(title)}_clean.jpg`;
    else if (jobRef) outputFilename = `${jobRef}_clean.jpg`;
    const dateCreated = str('dateCreated');
    res.set('Cache-Control', 'public, max-age=86400');
    return res.json({
      id, outputFilename, title,
      caption: str('caption'), copyright: str('copyright'), author: str('author'),
      credit: str('credit'), source: str('source'), keywords,
      keywordsList: Array.isArray(raw.keywords) ? raw.keywords : (keywords ? [keywords] : []),
      supplementalCategory, city: str('city'), province: str('province'), country: null,
      dateCreated: dateCreated ? formatDate(dateCreated) : null,
      timeCreated: str('timeCreated'), jobRef,
      specialInstructions: str('specialInstructions'), category: str('category'),
    });
  } catch (err) {
    console.error('[metadata] fetch failed:', err.message);
    return res.status(502).json({ error: 'Failed to fetch image metadata' });
  }
});

// ─── Proxy ─────────────────────────────────────────────────────────────────

const ALLOWED_HOST = 'alphapress.photoshelter.com';

app.get('/api/proxy', async (req, res) => {
  const rawUrl = req.query['url'];
  if (typeof rawUrl !== 'string' || !rawUrl) return res.status(400).json({ error: 'Missing url query parameter' });
  let parsed;
  try { parsed = new URL(rawUrl); } catch { return res.status(400).json({ error: 'Invalid URL' }); }
  if (parsed.hostname !== ALLOWED_HOST) return res.status(403).json({ error: `Only ${ALLOWED_HOST} URLs are allowed` });
  try {
    const upstream = await fetch(rawUrl, { headers: FETCH_HEADERS });
    if (!upstream.ok) return res.status(upstream.status).json({ error: `Upstream returned ${upstream.status}` });
    const contentType = upstream.headers.get('content-type') ?? 'image/jpeg';
    const buffer = Buffer.from(await upstream.arrayBuffer());
    res.set({ 'Content-Type': contentType, 'Content-Length': buffer.length, 'Cache-Control': 'public, max-age=3600', 'Access-Control-Allow-Origin': '*' });
    return res.send(buffer);
  } catch (err) {
    console.error('[proxy] fetch failed:', err.message);
    return res.status(502).json({ error: 'Failed to fetch upstream image' });
  }
});

// ─── Run process.py ────────────────────────────────────────────────────────

app.post('/api/run', (req, res) => {
  const { ids } = req.body;
  if (!Array.isArray(ids) || ids.length === 0) return res.status(400).json({ error: 'No image IDs provided' });
  const validIds = ids.filter((id) => typeof id === 'string' && /^I[0-9A-Za-z]{14,}$/.test(id));
  if (validIds.length === 0) return res.status(400).json({ error: 'No valid image IDs' });

  const pythonCmd = findPython();
  if (!pythonCmd) return res.status(500).json({ error: 'Python not found. Install from https://www.python.org/' });

  const scriptPath = path.join(__dirname, 'process.py');
  if (!fs.existsSync(scriptPath)) return res.status(500).json({ error: 'process.py not found in the application folder' });

  let stdout = '', stderr = '';
  const child = spawn(pythonCmd, [scriptPath, ...validIds], {
    cwd: __dirname,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
  });
  child.stdout.on('data', (d) => { stdout += d.toString(); });
  child.stderr.on('data', (d) => { stderr += d.toString(); });

  const timer = setTimeout(() => {
    child.kill();
    res.status(504).json({ error: 'process.py timed out after 5 minutes' });
  }, 5 * 60 * 1000);

  child.on('close', (code) => {
    clearTimeout(timer);
    res.json({ success: code === 0, code, output: stdout || stderr, stdout, stderr });
  });

  child.on('error', (err) => {
    clearTimeout(timer);
    res.status(500).json({ error: `Failed to start process.py: ${err.message}` });
  });
});

// ─── SPA fallback ──────────────────────────────────────────────────────────

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ─── Start ─────────────────────────────────────────────────────────────────

const PORT = Number(process.env.PORT) || 5000;
app.listen(PORT, '0.0.0.0', () => {
  console.log(`\n AlphaPress Photo Tool`);
  console.log(` Open in your browser: http://localhost:${PORT}`);
  console.log(` Press Ctrl+C to stop.\n`);
});
