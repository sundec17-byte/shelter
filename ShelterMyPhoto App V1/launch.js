#!/usr/bin/env node
'use strict';

const { execSync, spawn } = require('child_process');
const { platform } = require('os');
const path = require('path');

const isWindows = platform() === 'win32';

function step(n, total, label) {
  console.log(`\n[${n}/${total}] ${label}`);
}
function ok(msg)   { console.log(` [OK]    ${msg}`); }
function fail(msg) { console.error(` [ERROR] ${msg}`); }
function info(msg) { console.log(` [INFO]  ${msg}`); }

function run(cmd) {
  try { return execSync(cmd, { stdio: 'pipe' }).toString().trim(); }
  catch { return null; }
}

function pipInstall(pythonCmd, pkg) {
  info(`Installing ${pkg}...`);
  try {
    execSync(`${pythonCmd} -m pip install ${pkg} --quiet`, { stdio: 'inherit' });
    ok(`${pkg} installed.`);
    return true;
  } catch {
    fail(`Could not install ${pkg}. Run manually: pip install ${pkg}`);
    return false;
  }
}

console.log('\n ========================================');
console.log('  AlphaPress Photo Tool');
console.log(' ========================================');

let errors = 0;

// [1/4] Node.js version check
step(1, 4, 'Checking Node.js version...');
const nodeMajor = parseInt(process.versions.node.split('.')[0], 10);
if (nodeMajor < 18) {
  fail(`Node.js 18 or later required (found ${process.version}). Download at https://nodejs.org/`);
  process.exit(1);
} else {
  ok(`Node.js ${process.version} — OK.`);
}

// [2/4] Python (optional — only needed for process.py)
step(2, 4, 'Checking for Python...');
const pythonCmd =
  run('python3 --version') ? 'python3' :
  run('python --version')  ? 'python'  :
  null;

if (!pythonCmd) {
  fail('Python not found. Download from https://www.python.org/  (needed for process.py)');
  errors++;
} else {
  ok(`${run(`${pythonCmd} --version`)} found.`);

  // Auto-install Pillow + piexif
  if (run(`${pythonCmd} -c "import PIL"`) !== null) {
    ok('Pillow already installed.');
  } else if (!pipInstall(pythonCmd, 'Pillow')) {
    errors++;
  }

  if (run(`${pythonCmd} -c "import piexif"`) !== null) {
    ok('piexif already installed.');
  } else if (!pipInstall(pythonCmd, 'piexif')) {
    errors++;
  }
}

if (errors > 0) {
  console.error(`\n [ERROR] ${errors} requirement(s) unresolved — fix the errors above then re-run.\n`);
  process.exit(1);
}

// [3/4] npm install
step(3, 4, 'Installing Node dependencies...');
try {
  execSync('npm install --prefer-offline', {
    stdio: 'inherit',
    cwd: __dirname,
  });
} catch {
  console.error('\n [ERROR] npm install failed. Check your internet connection and retry.\n');
  process.exit(1);
}

// [4/4] Start server
step(4, 4, 'Starting server...');
const server = spawn(
  isWindows ? 'node.exe' : 'node',
  [path.join(__dirname, 'server.js')],
  { stdio: 'inherit', cwd: __dirname }
);

server.on('exit', (code) => {
  console.log(`\n Server stopped (code ${code})`);
  process.exit(code ?? 0);
});

// Open browser after short delay
setTimeout(() => {
  const url = 'http://localhost:5000';
  const openCmd =
    isWindows ? `start "" "${url}"` :
    platform() === 'darwin' ? `open "${url}"` :
    `xdg-open "${url}"`;
  try { execSync(openCmd, { stdio: 'ignore' }); } catch { /* ignore */ }
}, 1500);

process.on('SIGINT', () => {
  server.kill('SIGINT');
  process.exit(0);
});
