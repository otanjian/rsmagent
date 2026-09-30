/*
 * Browser/desktop environment adapter for the console.
 *
 * Change add-desktop-remote-web-workbench, task 5.1.
 *
 * The same console is served to a plain browser and into a desktop container.
 * The difference between the two is *not* a feature the business pages should
 * each test for: it is one environment question -- "is there a native host
 * behind me, and what may I ask it for?" -- and it belongs in one module. Pages
 * ask `CowDesktopHost`, never `window.desktopHost`, so:
 *
 *   * a browser gets a complete, working default: every method exists, the
 *     answers are honest ("not available"), and nothing throws;
 *   * a container gets the narrow bridge the main process installed, checked
 *     for shape and protocol major *before* it is used;
 *   * a page that assigns `window.desktopHost` itself changes nothing, because
 *     the bridge is validated rather than trusted;
 *   * no desktop branch is spread through console.js or the page views.
 *
 * Phase 1 creates no local-file grant: `localFiles()` reports unavailable in
 * both environments, and the module exposes no path or handle API at all.
 *
 * Loaded before `console.js` (it attaches one namespace and mounts nothing).
 */
(function (global) {
  'use strict';

  var BRIDGE_MAJOR = 1;
  var METHODS = [
    'getCapabilities',
    'suspendLocalContext',
    'saveArtifact',
    'openExternal',
    'onHostEvent'
  ];

  /** A download in the browser is the closest thing to "save as" without a host. */  function browserSave(name, content, doc) {
    try {
      var encoded = String(content || '');
      var binary = global.atob ? global.atob(encoded) : '';
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      var blob = new global.Blob([bytes], { type: 'application/octet-stream' });
      var url = global.URL.createObjectURL(blob);
      var anchor = doc.createElement('a');
      anchor.href = url;
      anchor.download = String(name || 'download');
      doc.body.appendChild(anchor);
      anchor.click();
      doc.body.removeChild(anchor);
      global.setTimeout(function () { global.URL.revokeObjectURL(url); }, 30000);
      return { saved: true, via: 'download' };
    } catch (_) {
      // No Blob/atob (an exotic frame): report it rather than pretending.
      return { saved: false, reason: 'unavailable' };
    }
  }

  /**
   * The browser's own `window.open`, even after the adapter wrapped it.
   *
   * `installWindowOpen` replaces `window.open` so a container routes popups
   * through the host. The fallback below must not call that wrapper again: a
   * bridge that is present but reports itself unavailable would otherwise
   * re-enter `openExternal` forever. The wrapper parks the real function on
   * itself, so a page that never had a bridge still gets its ordinary popup.
   */
  function browserOpen(url) {
    var current = global.open;
    var fn = (current && current.__cowBrowserOpen) || current;
    return fn.call(global, url, '_blank', 'noopener');
  }

  /**
   * The bridge, or null.
   *
   * Shape-checked, not merely present: a page that defines `window.desktopHost`
   * itself, or a host that speaks another protocol major, gets the browser
   * default instead of half-working native calls.
   */
  function resolveBridge() {
    var bridge = global.desktopHost;
    if (!bridge || typeof bridge !== 'object') return null;
    for (var i = 0; i < METHODS.length; i++) {
      if (typeof bridge[METHODS[i]] !== 'function') return null;
    }
    return bridge;
  }

  var capabilities = null;

  var localFilesSnapshot = {
    available: false,
    reason: 'feature_unavailable',
    candidates: [],
    connected: null,
    transfers: [],
    note: 'directories are never auto-uploaded; choose and bind per session'
  };

  function resetLocalFilesSnapshot(available) {
    localFilesSnapshot = {
      available: !!available,
      reason: available ? '' : 'feature_unavailable',
      candidates: [],
      connected: null,
      transfers: [],
      note: 'directories are never auto-uploaded; choose and bind per session'
    };
  }

  function getCapabilities() {
    if (capabilities) return capabilities;
    var bridge = resolveBridge();
    if (!bridge) {
      resetLocalFilesSnapshot(false);
      capabilities = Promise.resolve({
        available: false,
        environment: 'browser',
        bridge: '',
        methods: [],
        localFiles: false
      });
      return capabilities;
    }
    capabilities = Promise.resolve(bridge.getCapabilities())
      .then(function (reply) {
        var major = reply && reply.bridge ? String(reply.bridge).split('.')[0] : '';
        var declared = (reply && Array.isArray(reply.methods)) ? reply.methods : [];
        var usable = major === String(BRIDGE_MAJOR);
        var localFiles = !!(usable && reply && reply.localFiles);
        resetLocalFilesSnapshot(localFiles);
        return {
          available: usable,
          environment: usable ? 'desktop' : 'browser',
          bridge: reply && reply.bridge ? String(reply.bridge) : '',
          methods: usable ? declared : [],
          localFiles: localFiles,
          // An unknown major is reported so the caller can say *why* it fell
          // back, instead of silently behaving like a browser.
          reason: usable ? '' : 'bridge_incompatible'
        };
      })
      .catch(function () {
        resetLocalFilesSnapshot(false);
        return {
          available: false, environment: 'browser', bridge: '', methods: [],
          localFiles: false, reason: 'bridge_unavailable'
        };
      });
    return capabilities;
  }

  function withBridge(fn, fallback) {
    var bridge = resolveBridge();
    if (!bridge) return Promise.resolve(fallback());
    return getCapabilities().then(function (info) {
      if (!info.available) return fallback();
      return fn(bridge);
    });
  }

  var host = {
    /** The bridge major this console speaks. */
    BRIDGE_MAJOR: BRIDGE_MAJOR,

    /** True only when a validated native bridge is present. */
    isDesktop: function () {
      return resolveBridge() !== null;
    },

    /** 'browser' | 'desktop' -- the validated answer, not the raw guess. */
    environment: function () {
      return getCapabilities().then(function (info) { return info.environment; });
    },

    /** Never rejects: a caller that only needs "is there a host" gets an answer. */
    capabilities: getCapabilities,

    /**
     * Tell the host that local context is gone (tenant switch, reload, logout).
     *
     * The host uses this to stop presenting stale local state. In a browser it
     * is a no-op that still resolves, so callers need no branch of their own.
     */
    suspendLocalContext: function (reason) {
      return withBridge(function (bridge) {
        return Promise.resolve(bridge.suspendLocalContext(String(reason || '')))
          .then(function () { return { suspended: true }; });
      }, function () {
        return { suspended: false, reason: 'no_host' };
      });
    },

    /**
     * Save inline bytes. A browser downloads them (it has no native dialog to
     * offer and must not pretend it wrote a file); a container asks the main
     * process, which owns the dialog and the path.
     */
    saveArtifact: function (name, content) {
      return withBridge(function (bridge) {
        return Promise.resolve(bridge.saveArtifact(String(name || ''), String(content || '')));
      }, function () {
        if (typeof global.document === 'undefined') return { saved: false, reason: 'unavailable' };
        return browserSave(name, content, global.document);
      });
    },

    /** Open an http(s) link outside the application. */
    openExternal: function (url) {
      var target = String(url || '');
      if (!/^https?:\/\//i.test(target)) return Promise.resolve({ opened: false, reason: 'refused' });
      return withBridge(function (bridge) {
        return Promise.resolve(bridge.openExternal(target));
      }, function () {
        // A browser's own answer to "open externally" is a new tab; a popup
        // blocked by the browser reports itself through the return value.
        try {
          var opened = browserOpen(target);
          return { opened: !!opened, via: 'window' };
        } catch (_) {
          return { opened: false, reason: 'blocked' };
        }
      });
    },

    /** Subscribe to host events. The browser has none, so this is inert. */
    onHostEvent: function (listener) {
      if (typeof listener !== 'function') return function () {};
      var bridge = resolveBridge();
      if (!bridge) return function () {};
      try {
        var off = bridge.onHostEvent(listener);
        return typeof off === 'function' ? off : function () {};
      } catch (_) {
        return function () {};
      }
    },

    /**
     * Local file access surface (task 12.1).
     *
     * Synchronous snapshot of the last ``capabilities()`` answer. Call
     * ``capabilities()`` / ``canChooseWorkspace()`` first when the live host
     * view matters; until then both browser and closed deployments answer
     * unavailable -- never a path or handle.
     */
    localFiles: function () {
      return localFilesSnapshot;
    },

    /**
     * True when a validated host reports local-files open (or lists
     * ``chooseWorkspace``). Browsers and closed deployments stay false.
     */
    canChooseWorkspace: function () {
      return getCapabilities().then(function (info) {
        if (!info.available) return false;
        if (info.localFiles) return true;
        return (info.methods || []).indexOf('chooseWorkspace') >= 0;
      });
    },

    /** Ask the host to pick a directory (refused while the switch is closed). */
    chooseWorkspace: function (scope) {
      return withBridge(function (bridge) {
        if (typeof bridge.chooseWorkspace !== 'function') {
          return { activated: false, reason: 'feature_unavailable' };
        }
        return Promise.resolve(bridge.chooseWorkspace(scope || {}));
      }, function () {
        return { activated: false, reason: 'no_host' };
      });
    },

    /**
     * Confirm a picked directory so the server knows the binding, or report
     * that there is no host to confirm it with (task 2.2). Never throws in a
     * browser: the caller decides what to tell the user.
     */
    bindContext: function (params) {
      return withBridge(function (bridge) {
        if (typeof bridge.bindContext !== 'function') {
          return { ok: false, code: 'feature_unavailable', message: 'the host cannot confirm a local directory' };
        }
        return Promise.resolve(bridge.bindContext(params || {}));
      }, function () {
        return { ok: false, code: 'no_host', message: 'no host to confirm the local directory with' };
      });
    },

    /** Drop the active grant for the given scope (or all). */
    disconnectWorkspace: function (scope) {
      return withBridge(function (bridge) {
        if (typeof bridge.disconnectWorkspace !== 'function') {
          return { revoked: 0, reason: 'feature_unavailable' };
        }
        return Promise.resolve(bridge.disconnectWorkspace(scope || {}));
      }, function () {
        return { revoked: 0, reason: 'no_host' };
      });
    }
  };

  // A reload or a window close takes the page away without any view code
  // running, so the "local context is gone" hint is sent from here (task 5.2).
  // Best-effort by design: the guarantee that the pairing ends is server-side
  // (revoking one side revokes the other), and the main process tears the
  // container down on quit -- this only shortens the window in which a stale
  // context could be reused in the *same* document.
  if (typeof global.addEventListener === 'function') {
    global.addEventListener('pagehide', function () {
      try {
        host.suspendLocalContext('page-unload');
      } catch (_) {
        /* nothing left to do while unloading */
      }
    });
  }

  /**
   * Route `window.open` through the host when there is one (task 5.6).
   *
   * The container opens no windows: `setWindowOpenHandler` denies every popup
   * and hands the URL to the main process instead. Left alone, that denial is
   * *silent* -- the pages that open a preview or a documentation link in a new
   * window would simply do nothing in the container while working in a browser.
   * Rather than adding a desktop branch to each of those pages, the one seam
   * they all go through is replaced: `openExternal` decides (the system browser
   * for a foreign link, the isolated no-bridge window for a same-origin
   * document), and the caller gets the same "a window was opened" answer it
   * would have got from the browser -- or `null`, exactly as a browser answers
   * a blocked popup. Non-web schemes keep the browser's own refusal.
   */
  function installWindowOpen() {
    if (typeof global.open !== 'function') return;
    var browserOpen = global.open;
    var installed = function (url, name, features) {
      var target = String(url || '');
      if (!/^https?:\/\//i.test(target)) return null;
      var opened = { closed: false, focus: function () {}, close: function () {} };
      host.openExternal(target).then(function (result) {
        if (!result || !result.opened) opened.closed = true;
      }, function () { opened.closed = true; });
      // The name/features are the page's; the host owns the window, so they are
      // never forwarded (a page cannot ask for a specific native window).
      void name;
      void features;
      return opened;
    };
    installed.__cowBridged = true;
    installed.__cowBrowserOpen = browserOpen;
    try {
      global.open = installed;
    } catch (_) {
      /* a frozen window: the popup is denied, which is the same outcome */
    }
  }

  if (resolveBridge() !== null) installWindowOpen();

  global.CowDesktopHost = host;
})(typeof window !== 'undefined' ? window : this);
