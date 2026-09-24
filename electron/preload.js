// The bridge between the page and Electron's privileged side.
//
// contextIsolation is on and nodeIntegration is off, so the page has no
// access to Node, the filesystem, or Electron's APIs. Whatever is exposed
// here is the complete list of what it can reach — which is why this file
// is deliberately tiny.
//
// Two things only: where the worker is listening, and the native folder
// picker. Everything else the app does goes over HTTP to the worker, which
// is the one place decisions about a producer's files are made.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('uforiya', {
  // Returns { port, token }. The renderer uses these to build its own
  // requests rather than having a client baked in here — keeping this file
  // free of app logic means a UI rewrite never touches the trust boundary.
  getWorkerInfo: () => ipcRenderer.invoke('worker:info'),

  // A native folder dialog needs a window handle to parent itself to, and
  // the worker has no window. This is the single exception to "the worker
  // owns everything".
  //
  // Returns the chosen path with forward slashes, or null if cancelled.
  pickFolder: () => ipcRenderer.invoke('dialog:pickFolder'),

  // The renderer has no Node access, so it can't read package.json itself.
  // Used by the info tab to show a version a bug report can be tied to.
  getAppVersion: () => ipcRenderer.invoke('app:version'),

  // The page's CSP permits loopback only, so the one outbound request the
  // app makes is done on the privileged side and handed back as data.
  checkForUpdate: () => ipcRenderer.invoke('app:checkUpdate'),
});