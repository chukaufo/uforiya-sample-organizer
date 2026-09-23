// Electron main process: spawns the worker, owns the window, shuts down cleanly.
//
// This file holds no business logic. It starts a Python process, waits for it
// to announce its port, opens a window, and hands over. Every decision about
// files is made by the worker.
const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const path = require('path');

// Development runs the worker from source with a fixed port and token, so a
// code change is a worker restart rather than a re-freeze. Freezing on every
// edit would make iteration unbearable.
const IS_DEV = !app.isPackaged;
const DEV_PORT = 8787;
const DEV_TOKEN = 'dev';

// How long to wait for the worker's READY line before giving up. Generous:
// on Windows, antivirus can hold a freshly extracted binary for several
// seconds on first launch.
const READY_TIMEOUT_MS = 20000;

// Grace period between asking the worker to stop and killing it. The worker
// closes SQLite and checkpoints its WAL in that window.
const SHUTDOWN_GRACE_MS = 3000;

let mainWindow = null;
let worker = null;
let workerPort = null;
let workerToken = null;
let shuttingDown = false;


// ── Worker lifecycle ────────────────────────────────────────────────────────

function workerCommand() {
  // Packaged: the frozen binary sits in the app's resources, put there by
  // electron-builder's extraResources rule.
  if (!IS_DEV) {
    const exe = process.platform === 'win32' ? 'worker.exe' : 'worker';
    return {
      command: path.join(process.resourcesPath, 'worker', exe),
      args: ['--port', '0', '--token', workerToken],
    };
  }

  // Development: run main.py with the repo's virtualenv interpreter.
  const python = process.platform === 'win32'
    ? path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe')
    : path.join(__dirname, '..', '.venv', 'bin', 'python');

  return {
    command: python,
    args: [path.join(__dirname, '..', 'main.py'), '--port', String(DEV_PORT),
           '--token', DEV_TOKEN],
  };
}


function startWorker() {
  // A fresh token every launch, held only in these two processes' memory.
  // The worker can move files, so an unauthenticated local port is not an
  // acceptable thing to leave open.
  workerToken = IS_DEV ? DEV_TOKEN : crypto.randomBytes(32).toString('hex');

  const { command, args } = workerCommand();

  return new Promise((resolve, reject) => {
    // windowsHide stops a console window flashing up beside the app. Without
    // it the app looks broken on every launch.
    worker = spawn(command, args, {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let settled = false;
    let buffer = '';

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error('The worker did not start in time.'));
    }, READY_TIMEOUT_MS);

    // The handshake. The window is not created until this line is parsed,
    // because a renderer that loads before the backend exists fails its
    // first request and shows an error on a perfectly working app.
    worker.stdout.on('data', (chunk) => {
      buffer += chunk.toString();

      const line = buffer.split('\n').find((l) => l.startsWith('UFORIYA_READY'));
      if (!line || settled) return;

      try {
        const payload = JSON.parse(line.slice('UFORIYA_READY'.length).trim());
        workerPort = payload.port;
        settled = true;
        clearTimeout(timer);
        resolve();
      } catch (err) {
        settled = true;
        clearTimeout(timer);
        reject(new Error('The worker sent an unreadable ready signal.'));
      }
    });

    // Worker stderr goes to the main process's console, which Electron can
    // capture to a log file. Python tracebacks land here.
    worker.stderr.on('data', (chunk) => {
      process.stderr.write(`[worker] ${chunk}`);
    });

    worker.on('error', (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(err);
    });

    // A worker that dies mid-session is a crash, and it is shown as one.
    // Silently respawning would hide a crash loop, which is worse than a
    // visible failure.
    worker.on('exit', (code) => {
      if (shuttingDown) return;
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`The worker exited during startup (code ${code}).`));
        return;
      }
      if (mainWindow) {
        dialog.showErrorBox(
          'Uforiya Sample Organizer',
          'The background worker stopped unexpectedly. Please restart the app.'
        );
      }
    });
  });
}


async function stopWorker() {
  if (!worker) return;
  shuttingDown = true;

  // Ask first: the worker closes SQLite and checkpoints its WAL, so the next
  // launch does no recovery pass.
  try {
    await fetch(`http://127.0.0.1:${workerPort}/shutdown`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${workerToken}` },
    });
  } catch (err) {
    // Already gone, or wedged. Either way the kill below handles it.
  }

  await new Promise((resolve) => setTimeout(resolve, SHUTDOWN_GRACE_MS));

  if (worker.exitCode === null) {
    if (process.platform === 'win32') {
      // /T kills the process tree. Without it, PyInstaller's bootloader
      // child survives and holds the SQLite file open, and the next launch
      // fails in a way that looks random.
      spawn('taskkill', ['/pid', String(worker.pid), '/T', '/F'], {
        windowsHide: true,
      });
    } else {
      worker.kill('SIGKILL');
    }
  }

  worker = null;
}


// ── Window ──────────────────────────────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 760,
    minWidth: 720,
    minHeight: 480,
    backgroundColor: '#08080f',
    // Named BrowserWindow, but it is just the app's own window — own icon,
    // no address bar, no tabs, rendered by Electron's bundled Chromium.
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // The renderer gets no Node access. It talks to the worker over HTTP
      // and nothing else; anything privileged goes through preload's
      // narrow bridge.
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'index.html'));

  // Shown only once painted, so the window never appears empty.
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Links open in the user's browser rather than hijacking the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}


// ── Bridge ──────────────────────────────────────────────────────────────────

// The renderer needs the port and token to talk to the worker. They are
// passed through here rather than baked into the page.
ipcMain.handle('worker:info', () => ({
  port: workerPort,
  token: workerToken,
}));

// The one thing the worker cannot do itself: a native folder picker needs a
// window handle to parent the dialog to, and the worker has no window.
ipcMain.handle('dialog:pickFolder', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: 'Choose a sample library folder',
    properties: ['openDirectory'],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  // Forward slashes, matching the worker's own path normalisation.
  return result.filePaths[0].replace(/\\/g, '/');
});


// ── App lifecycle ───────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  try {
    await startWorker();
  } catch (err) {
    dialog.showErrorBox(
      'Uforiya Sample Organizer',
      `Could not start the background worker.\n\n${err.message}`
    );
    app.quit();
    return;
  }

  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  // Quit on every platform including macOS. This is a tool a producer opens
  // to do a job, not something that should linger in the dock with a live
  // worker process behind it.
  app.quit();
});

// Shutdown is where this usually breaks. The quit is deferred until the
// worker is actually gone, rather than leaving a zombie holding the index.
let cleanupDone = false;
app.on('before-quit', async (event) => {
  if (cleanupDone) return;
  event.preventDefault();
  await stopWorker();
  cleanupDone = true;
  app.quit();
});
