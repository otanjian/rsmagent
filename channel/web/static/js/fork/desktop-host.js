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
  /**
   * The methods a bridge must have to be usable *at all* (bridge major 1).
   *
   * Kept as the minimum rather than "everything this build knows": a console
   * shipped with a newer build must still work against an older container, so a
   * method that arrived later is detected from `capabilities().methods` instead
   * of being required here. That way the established surface stays usable and
   * only the newer feature reports itself unavailable.
   */
  var METHODS = [
    'getCapabilities',
    'suspendLocalContext',
    'saveArtifact',
    'openExternal',
    'onHostEvent'
  ];

  /**
   * The local *project source* surface (task 9.2).
   *
   * All six travel together: a bridge that offers only the listing cannot be
   * used for the panel, because a tree whose files cannot be opened is worse
   * than no tree at all.
   */
  var PROJECT_METHODS = [
    'projectSource',
    'projectTree',
    'projectSearch',
    'projectResolve',
    'projectRead',
    'projectWrite'
  ];

  /**
   * The system actions on a project file (task 9.4).
   *
   * Deliberately **not** part of `PROJECT_METHODS`: reading a project and
   * handing one of its files to the OS are different capabilities, and a
   * container that can do the first but not the second must still serve the
   * panel. The panel asks `canUseProjectActions()` before it draws a single
   * button, so an older host loses the buttons and keeps the panel.
   *
   * One table, two readings: the *action* the panel names (`CowProjectSource.act`
   * is called with `open`, `reveal`, `copyPath`, `saveAs` -- the vocabulary the
   * file card's buttons speak) and the preload method it means. Both directions
   * are derived from this object, so the capability this host declares and the
   * action it accepts cannot drift apart: a button is offered exactly when the
   * method behind it exists.
   */
  var PROJECT_ACTIONS = {
    open: 'projectOpenFile',
    reveal: 'projectRevealFile',
    copyPath: 'projectCopyPath',
    saveAs: 'projectSaveFileAs'
  };

  /** The preload methods behind those actions, in the order they are declared. */
  var PROJECT_ACTION_METHODS = Object.keys(PROJECT_ACTIONS).map(function (action) {
    return PROJECT_ACTIONS[action];
  });

  /**
   * The protected preview of a project file (task 9.5).
   *
   * Its own capability, for the same reason the actions have theirs: previewing a
   * generated HTML report and handing a spreadsheet to Excel are different
   * promises, and a host that can do one must not be asked for the other. The
   * panel asks `canUseProjectPreview()` before it renders anything for a local
   * file, and falls back to what it did before when the answer is no.
   */
  var PROJECT_PREVIEW_METHODS = [
    'projectPreviewFile'
  ];

  /** A reply both environments can be read the same way. */
  function refuse(code, message) {
    return { ok: false, code: code, message: message };
  }

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

    /**
     * Whether this host can end the account itself.
     *
     * Change ``fix-desktop-relogin-session-sync``, task 2.3. The console is
     * served into containers older than itself, so the method is *detected*
     * rather than required: a host that does not declare ``signOut`` answers
     * false, and the page prompts an upgrade instead of falling back to a plain
     * Web logout -- which is the very state this exists to remove.
     */
    canSignOut: function () {
      return getCapabilities().then(function (info) {
        if (!info.available) return false;
        return (info.methods || []).indexOf('signOut') >= 0;
      });
    },

    /**
     * End this container's account through the host.
     *
     * The native half of the sign-out the page cannot perform: detach the
     * container, drop the local resources it holds, and tell the broker to
     * revoke. ``{ok: true, signedOut, revoked}`` on success; a refusal by name
     * (``host_outdated``, ``no_host``, ``logout_incomplete``) otherwise, never a
     * thrown exception, so the caller can say which answer it got.
     *
     * It is deliberately *not* a Web logout with a different name: the caller
     * confirms the Web session first (see ``CowDesktopAccount``) and this
     * finishes afterwards.
     */
    signOut: function () {
      return withBridge(function (bridge) {
        if (typeof bridge.signOut !== 'function') {
          return refuse('host_outdated', 'this desktop build cannot end the session');
        }
        return Promise.resolve(bridge.signOut()).then(function (reply) {
          return {
            ok: true,
            signedOut: !!(reply && reply.signedOut),
            revoked: !!(reply && reply.revoked)
          };
        }).catch(function (err) {
          return refuse((err && err.code) || 'logout_incomplete',
            (err && err.message) || 'the desktop session could not be ended');
        });
      }, function () {
        return refuse('no_host', 'this environment cannot end a desktop session');
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
    chooseWorkspace: function (scope, purpose) {
      return withBridge(function (bridge) {
        if (typeof bridge.chooseWorkspace !== 'function') {
          return { activated: false, reason: 'feature_unavailable' };
        }
        // ``purpose`` is optional: omitted keeps the read-only reference the
        // page has always asked for. ``project-execution`` is the explicit
        // "open my project here" authorization (align-desktop-project-execution).
        return Promise.resolve(bridge.chooseWorkspace(scope || {}, purpose || ''));
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
    },

    /**
     * The local project this container already has open for one chat (task 9.3).
     *
     * The answer a *reloaded* page needs: its own state is gone, so the host is
     * asked what is actually open, and it re-verifies before saying so. A host
     * that predates the method answers `none`, which reads as "nothing to
     * resume" -- never as "the project is gone", so nothing is invented for it.
     */
    localContext: function (params) {
      return withBridge(function (bridge) {
        if (typeof bridge.localContext !== 'function') {
          return { state: 'none', reason: 'feature_unavailable' };
        }
        return Promise.resolve(bridge.localContext(params || {})).catch(function (err) {
          return { state: 'none', reason: (err && err.code) || 'refused' };
        });
      }, function () {
        return { state: 'none', reason: 'no_host' };
      });
    },

    /**
     * Whether the panel should read the project through the host (task 9.2).
     *
     * The single question the file panel has to ask before it chooses a source.
     * ``false`` means "use the backend's workspace API", which is the honest
     * answer for a plain browser *and* for a container whose session is not
     * bound to a directory on this machine. It never means "no project": it
     * means the project, if any, is the backend's own.
     */
    canUseProjectSource: function () {
      return getCapabilities().then(function (info) {
        if (!info.available || !info.localFiles) return false;
        var declared = info.methods || [];
        for (var i = 0; i < PROJECT_METHODS.length; i++) {
          if (declared.indexOf(PROJECT_METHODS[i]) < 0) return false;
        }
        return true;
      });
    },

    /**
     * One panel call against the local project source.
     *
     * Refusals keep the host's own vocabulary -- ``stale_context``,
     * ``source_read_only``, ``device_offline``, ``invalid_path`` -- and never
     * become a thrown exception, so the panel can say *which* answer it got.
     * With no host (or no project surface) the call is refused by name rather
     * than silently sent to the backend: a local project and a server directory
     * can hold files with the same name, and answering with the wrong one is
     * exactly the confusion this surface exists to end.
     */
    project: function (method, params) {
      if (PROJECT_METHODS.indexOf(String(method || '')) < 0) {
        return Promise.resolve(refuse('invalid_request', 'unknown project method'));
      }
      var payload = params || {};
      return withBridge(function (bridge) {
        if (typeof bridge[method] !== 'function') {
          return refuse('feature_unavailable', 'this host does not read a local project');
        }
        return Promise.resolve(bridge[method](payload)).catch(function (err) {
          return refuse((err && err.code) || 'device_error',
            (err && err.message) || 'the local project call was refused');
        });
      }, function () {
        return refuse('no_host', 'there is no local project host in this environment');
      });
    },

    /**
     * Whether this host can hand a project file to the system (task 9.4).
     *
     * All four actions or none: a panel offering "open" but not "copy path"
     * would be a panel that has to explain a half-present feature, and the four
     * are one capability -- acting on a file the user can already see.
     */
    canUseProjectActions: function () {
      return getCapabilities().then(function (info) {
        if (!info.available || !info.localFiles) return false;
        var declared = info.methods || [];
        for (var i = 0; i < PROJECT_ACTION_METHODS.length; i++) {
          if (declared.indexOf(PROJECT_ACTION_METHODS[i]) < 0) return false;
        }
        return true;
      });
    },

    /**
     * One system action on one project file.
     *
     * The caller names the *action* (`open`, `reveal`, `copyPath`, `saveAs`),
     * the same word the file card's button carries and the same word
     * `CowProjectSource.act` documents; which preload method that means is this
     * host's business, and `PROJECT_ACTIONS` is the only place that knows.
     *
     * The reply says *what happened*, never where the file is: `{opened: true}`
     * for open and reveal, `{copied: true}` for copy-path, `{saved: true, name}`
     * for save-as. A refusal keeps the host's own code -- `not_found` (the file
     * is gone), `stale_context` (the project is no longer authorized), `no_application`
     * (this machine has nothing that opens this kind of file), `changed` (the
     * file moved on since the panel read it), `cancelled` (the user dismissed
     * the dialog) -- so the panel can say the true reason instead of guessing.
     */
    projectAction: function (method, params) {
      var action = String(method === undefined || method === null ? '' : method);
      // `hasOwnProperty`, not `PROJECT_ACTIONS[action]`: the table is a plain
      // object, so `constructor` and `toString` would otherwise be *names* this
      // host accepts and then look for a preload method that does not exist.
      var preloadMethod = Object.prototype.hasOwnProperty.call(PROJECT_ACTIONS, action)
        ? PROJECT_ACTIONS[action] : '';
      if (!preloadMethod) {
        return Promise.resolve(refuse('invalid_request', 'unknown project action'));
      }
      var payload = params || {};
      return withBridge(function (bridge) {
        if (typeof bridge[preloadMethod] !== 'function') {
          return refuse('feature_unavailable', 'this host cannot act on a local project file');
        }
        return Promise.resolve(bridge[preloadMethod](payload)).catch(function (err) {
          return refuse((err && err.code) || 'device_error',
            (err && err.message) || 'the local project action was refused');
        });
      }, function () {
        return refuse('no_host', 'this environment cannot open a local file');
      });
    },

    /**
     * Whether this host can preview a local file's content (task 9.5).
     *
     * Without this the panel keeps the behaviour it had before: a bounded text
     * read for markup, and "open it with an application" for bytes it cannot
     * carry. The panel never guesses -- it asks, and then draws.
     */
    canUseProjectPreview: function () {
      return getCapabilities().then(function (info) {
        if (!info.available || !info.localFiles) return false;
        var declared = info.methods || [];
        for (var i = 0; i < PROJECT_PREVIEW_METHODS.length; i++) {
          if (declared.indexOf(PROJECT_PREVIEW_METHODS[i]) < 0) return false;
        }
        return true;
      });
    },

    /**
     * The protected preview of one project file.
     *
     * The answer is a refusal the panel shows by name (`unsupported_type`,
     * `not_found`, `stale_context`, `limit_exceeded`) or
     * `{ok: true, name, kind, size, url, expires_at}`: a URL the panel may
     * **embed**, and must not keep. It is short-lived on purpose, it names one
     * file, and the host serves it under a policy the page cannot relax.
     */
    projectPreview: function (params) {
      var payload = params || {};
      return withBridge(function (bridge) {
        if (typeof bridge.projectPreviewFile !== 'function') {
          return refuse('feature_unavailable', 'this host cannot preview a local project file');
        }
        return Promise.resolve(bridge.projectPreviewFile(payload)).catch(function (err) {
          return refuse((err && err.code) || 'device_error',
            (err && err.message) || 'the local preview was refused');
        });
      }, function () {
        return refuse('no_host', 'there is no local preview host in this environment');
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
