/*
 * The local project as the file panel's *source*.
 *
 * Change align-desktop-project-execution-with-master, task 9.2.
 *
 * The panel has always read `/api/workspace/*`, which resolves a path against
 * the *backend's* filesystem. That is the right answer when the files are the
 * backend's and the wrong one when the session is bound to a project on the
 * user's own machine: the backend has no such path, so a request either fails
 * or -- worse -- answers about a different file that happens to share a name.
 *
 * So there are two sources and one place that chooses between them. This module
 * is that place. It translates the panel's existing requests into the local
 * project surface the shell exposes (`CowDesktopHost.project*`) and shapes the
 * replies exactly the way the panel already consumes them, so no view grows a
 * desktop branch:
 *
 *   * `/api/workspace/{tree,resolve,read,search}` -> `project{Tree,Resolve,Read,Search}`
 *   * `/api/workspace/write`                      -> `projectWrite`
 *   * open / reveal / copy path / save-as          -> `project{OpenFile,RevealFile,CopyPath,SaveFileAs}`
 *     (task 9.4: the four actions only the *system* can carry out, which the
 *     panel asks for by path and is answered about by name)
 *   * preview of generated or binary content       -> `projectPreviewFile`
 *     (task 9.5: the host reads the bytes and hands back a short-lived protected
 *     URL for the panel to *embed*; the panel never receives the bytes, and no
 *     local file gets a permanent URL of any kind)
 *
 * The rule for choosing is one question -- "is this session bound to a project
 * on *this* machine, and can this build serve it?" -- answered by
 * `applies()`. When the answer is no, every method returns `null` and the caller
 * makes the original request untouched: a backend source keeps working exactly
 * as before, which is what "后端来源继续走原 API" requires.
 *
 * Two properties are deliberate:
 *
 *   * **A reply never carries a directory.** Local entries are marked
 *     `local: true` and get no `abs_path`, `raw_url` or `preview_url`; the shell
 *     is the only party that knows where the project is, and it re-checks that on
 *     every call.
 *   * **A refusal is an answer.** `source_read_only`, `device_offline`,
 *     `stale_context` and friends travel back as `{status:'error', code, ...}`,
 *     which is what the panel's error paths already understand. Nothing is
 *     papered over with an empty listing, and nothing falls back to the server
 *     filesystem.
 *
 * Loaded after `fork/desktop-host.js` and before `workspace.js`; it attaches one
 * namespace and mounts nothing.
 */
(function (global) {
  'use strict';

  /** Console kinds the panel offers an editor for (mirrors WS_EDITABLE). */
  var DEFAULT_EDITABLE = ['html', 'markdown', 'csv', 'code', 'text'];

  /** Console kinds the panel can render in place (mirrors WS_PREVIEWABLE). */
  var DEFAULT_PREVIEWABLE = ['html', 'markdown', 'image', 'video', 'audio',
    'pdf', 'csv', 'code', 'text'];

  /** The requests this adapter answers, in the panel's own vocabulary. */
  var ROUTES = {
    '/api/workspace/tree': 'tree',
    '/api/workspace/resolve': 'resolve',
    '/api/workspace/read': 'read',
    '/api/workspace/search': 'search'
  };

  /**
   * How the adapter classifies an entry.
   *
   * Provided by the panel rather than duplicated here: `workspace.js` already
   * owns the extension -> kind table the icons, the preview and the editor
   * agree on, and a second copy would be a second answer to the same question.
   * The defaults keep this module testable on its own.
   */
  var classify = {
    kindOf: function (name) {
      var ext = String(name || '').split('.').pop().toLowerCase();
      for (var i = 0; i < DEFAULT_PREVIEWABLE.length; i++) {
        if (DEFAULT_PREVIEWABLE[i] === ext) return ext;
      }
      return ext === 'htm' ? 'html' : 'file';
    },
    editable: function (kind) { return DEFAULT_EDITABLE.indexOf(kind) >= 0; },
    previewable: function (kind) { return DEFAULT_PREVIEWABLE.indexOf(kind) >= 0; }
  };

  /**
   * The binding the panel must read from, or `null`.
   *
   * Read live on every call, never cached: the interesting case is precisely the
   * one where the binding *stopped* being valid (the user closed the project, the
   * grant was revoked, the session moved), and a cached answer would keep serving
   * a directory the user no longer authorized.
   */
  var bindingProvider = function () { return null; };

  function binding() {
    var ctx = null;
    try {
      ctx = bindingProvider();
    } catch (_) {
      return null;
    }
    if (!ctx || !ctx.workspace_id) return null;
    return ctx;
  }

  /** The host bridge, or `null` when this page has none. */
  function host() {
    var h = global.CowDesktopHost;
    return (h && typeof h.project === 'function') ? h : null;
  }

  function ok(payload) {
    return Object.assign({ status: 'success' }, payload);
  }

  /**
   * A refusal the panel can act on, in the shape its error paths already read.
   *
   * Used for a request the *adapter itself* refuses (a count it cannot parse):
   * that request must not fall back to the backend API, because the backend
   * would answer about a same-named file on the server.
   */
  function fail(code, message) {
    return { status: 'error', code: code || 'device_error', message: message || 'the local project call failed' };
  }

  /** One bridge call for the current binding. */
  function call(method, params) {
    var ctx = binding();
    if (!ctx) return Promise.resolve(null);
    var bridge = host();
    if (!bridge) return Promise.resolve(null);
    var payload = Object.assign({ workspace_id: String(ctx.workspace_id) }, params || {});
    return bridge.project(method, payload).then(function (reply) {
      if (reply && reply.ok === false) return { refusal: reply };
      return { data: reply || {} };
    });
  }

  /** The refusal shape the panel's error paths already understand. */
  function refusalStatus(reply, fallbackMessage) {
    var r = (reply && reply.refusal) || {};
    return {
      status: 'error',
      code: r.code || 'device_error',
      message: r.message || fallbackMessage || 'the local project call was refused'
    };
  }

  /**
   * How one refusal travels.
   *
   * ``feature_unavailable`` means this build or deployment cannot serve a local
   * project *at all* (no project-execution channel, no bridge surface) -- the
   * honest answer there is the backend's own API, which resolves the session's
   * project server-side when it can and says so when it cannot. Every other code
   * is about *this* project (revoked, read-only, offline, unreadable) and must be
   * reported: falling back on those would silently show different files under the
   * same names.
   */
  function refusalOrFallback(reply, fallbackMessage) {
    if (((reply && reply.refusal) || {}).code === 'feature_unavailable') return null;
    return refusalStatus(reply, fallbackMessage);
  }

  /** One local entry, in the shape `/api/workspace/*` entries have. */
  function entryOf(row) {
    var name = String((row && row.name) || '');
    var isDir = String((row && row.kind) || '') === 'dir';
    var kind = isDir ? 'directory' : classify.kindOf(name);
    var size = (row && row.size === null) || (row && row.size === undefined)
      ? 0 : Number(row.size);
    return {
      name: name,
      path: String((row && row.path) || ''),
      is_dir: isDir,
      kind: kind,
      size: isDir ? 0 : size,
      mtime: Number((row && row.modified) || 0),
      previewable: !isDir && classify.previewable(kind),
      // Local, and marked so: the panel must not hand this entry to a server
      // endpoint (`/api/file`, `@` upload) that would look for the same relative
      // path on the backend.
      source: 'desktop',
      local: true
    };
  }

  /**
   * A page-supplied count (`limit`, `offset`, `bytes`), read strictly.
   *
   * The bridge refuses anything that is not a non-negative integer, and the
   * query string only ever holds strings -- so a page that asks for
   * `?offset=100` would otherwise be refused as malformed. Converting here is
   * the adapter's job; *widening* is not: a `limit` this function cannot read
   * is a refusal the panel can show, never a silent drop that would make the
   * listing answer with more than the caller asked for.
   *
   * Returns `{ value }` (with `value === undefined` meaning "not requested"),
   * or `{ refusal }`.
   */
  function count(url, key) {
    var raw = url.searchParams.get(key);
    if (raw === null || raw === '') return { value: undefined };
    if (!/^(0|[1-9][0-9]*)$/.test(raw)) {
      return { refusal: fail('invalid_request', 'the requested ' + key + ' is not a count') };
    }
    return { value: Number(raw) };
  }

  /** Copy a `count()` result onto `params`, or answer with its refusal. */
  function withCount(params, url, key) {
    var read = count(url, key);
    if (read.refusal) return read;
    if (read.value !== undefined) params[key] = read.value;
    return { params: params };
  }

  function tree(url) {
    var shaped = withCount({ path: url.searchParams.get('path') || '' }, url, 'limit');
    if (shaped.refusal) return Promise.resolve(shaped.refusal);
    return call('projectTree', shaped.params).then(function (reply) {
      if (!reply) return null;
      if (reply.refusal) return refusalOrFallback(reply, 'the project could not be listed');
      var data = reply.data || {};
      return ok({
        path: String(data.path || ''),
        // No `root`: the backend's listing names an absolute directory, and a
        // local reply must not. The panel shows the label the user chose.
        entries: (data.entries || []).map(entryOf),
        truncated: !!data.truncated,
        next_cursor: data.next_cursor === undefined ? null : data.next_cursor
      });
    });
  }

  function resolve(url) {
    return call('projectResolve', { path: url.searchParams.get('path') || '' })
      .then(function (reply) {
        if (!reply) return null;
        if (reply.refusal) return refusalOrFallback(reply, 'the file could not be resolved');
        var data = reply.data || {};
        var row = {
          name: (String(data.path || '').split('/').pop()) || '',
          path: String(data.path || ''),
          kind: data.kind,
          size: data.size,
          modified: data.modified,
          is_dir: String(data.kind || '') === 'dir'
        };
        var entry = entryOf(row);
        return ok({ file: entry });
      });
  }

  function read(url) {
    var shaped = withCount({ path: url.searchParams.get('path') || '' }, url, 'offset');
    if (shaped.refusal) return Promise.resolve(shaped.refusal);
    var ranged = withCount(shaped.params, url, 'bytes');
    if (ranged.refusal) return Promise.resolve(ranged.refusal);
    return call('projectRead', ranged.params).then(function (reply) {
      if (!reply) return null;
      if (reply.refusal) return refusalOrFallback(reply, 'the file could not be read');
      var data = reply.data || {};
      var name = (String(data.path || '').split('/').pop()) || '';
      var kind = classify.kindOf(name);
      var truncated = !!data.truncated;
      return ok({
        path: String(data.path || ''),
        content: String(data.text || ''),
        truncated: truncated,
        // The shell decodes with the requested encoding and replaces undecodable
        // bytes, so a page's text is never a lossy read here -- the helper would
        // have refused a non-text file before this point.
        lossy: false,
        size: Number(data.total || 0),
        mtime: Number(data.mtime || 0),
        // A partial read must not be editable: saving it would truncate the tail.
        editable: classify.editable(kind) && !truncated,
        source: 'desktop',
        local: true
      });
    });
  }

  function search(url) {
    var query = url.searchParams.get('q') || '';
    var shaped = withCount({ query: query, mode: 'name' }, url, 'limit');
    if (shaped.refusal) return Promise.resolve(shaped.refusal);
    return call('projectSearch', shaped.params).then(function (reply) {
      if (!reply) return null;
      if (reply.refusal) return refusalOrFallback(reply, 'the project could not be searched');
      var data = reply.data || {};
      return ok({
        query: query,
        results: (data.hits || []).map(function (hit) {
          return entryOf({
            name: String((hit && hit.path) || '').split('/').pop(),
            path: hit && hit.path,
            kind: hit && hit.kind,
            size: hit && hit.size,
            modified: hit && hit.modified
          });
        }),
        truncated: !!data.truncated
      });
    });
  }

  var handlers = { tree: tree, resolve: resolve, read: read, search: search };

  /**
   * Answer one panel request locally, or `null` to keep the original API.
   *
   * @param {string} path - the same `/api/workspace/...` URL the panel builds.
   * @returns {Promise<object|null>} a panel-shaped payload, or `null`.
   */
  function handle(path) {
    var target = String(path || '');
    var queryAt = target.indexOf('?');
    var route = ROUTES[queryAt < 0 ? target : target.slice(0, queryAt)];
    if (!route) return Promise.resolve(null);
    if (!binding()) return Promise.resolve(null);
    if (!host()) return Promise.resolve(null);
    var url;
    try {
      url = new global.URL(target, 'http://local.invalid');
    } catch (_) {
      return Promise.resolve(null);
    }
    return handlers[route](url).then(function (reply) {
      return reply === undefined ? null : reply;
    });
  }

  /**
   * Save one file the user edited, or `null` to keep the original API.
   *
   * The conflict check the editor relies on is made here rather than at the
   * shell, because the shell's write frame deliberately carries no expectation:
   * a `write` tool call has no baseline, a panel edit does. Comparing the file's
   * current version against the one the editor loaded keeps "the agent rewrote it
   * while you were typing" from silently discarding someone's work.
   *
   * @returns {Promise<object|null>} the write reply, or `null`.
   */
  function write(body) {
    if (!binding() || !host()) return Promise.resolve(null);
    var params = body || {};
    var path = String(params.path || '');
    if (!path) return Promise.resolve(null);
    var expected = params.expected_mtime;
    // ``expected_mtime: null`` is the editor's explicit "overwrite anyway", so
    // only a real baseline is compared. Anything within a millisecond counts as
    // the same version: the shell reports whole seconds.
    var check = Promise.resolve(false);
    if (typeof expected === 'number' && isFinite(expected)) {
      check = call('projectResolve', { path: path }).then(function (meta) {
        var current = Number(((meta && meta.data) || {}).modified || 0);
        return !!current && Math.abs(current - expected) > 0.001;
      });
    }
    return check.then(function (changed) {
      if (changed) {
        return {
          status: 'error',
          code: 'conflict',
          message: 'the file changed on disk since it was read'
        };
      }
      return call('projectWrite', { path: path, content: String(params.content || '') })
        .then(function (reply) {
          if (!reply) return null;
          if (reply.refusal) return refusalOrFallback(reply, 'the file could not be saved');
          // The frame reports that the write completed; the editor also wants the
          // version it just wrote, so the file is resolved once after the save.
          return call('projectResolve', { path: path }).then(function (meta) {
            var data = (meta && meta.data) || {};
            return ok({
              path: path,
              size: Number(data.size === null || data.size === undefined ? 0 : data.size),
              mtime: Number(data.modified || 0),
              source: 'desktop',
              local: true
            });
          });
        });
    });
  }

  /** Whether the panel should read from the local project at all. */
  function applies() {
    if (!binding() || !host()) return Promise.resolve(false);
    return host().canUseProjectSource().catch(function () { return false; });
  }

  /** A refusal shaped like the bridge's own: `{ok:false, code, message}`. */
  function actionRefusal(code, message) {
    return { ok: false, code: code || 'device_error', message: message || '' };
  }

  /**
   * Refuse an action on a file that belongs to *another* project (task 9.6).
   *
   * The panel's *live* cards carry a path inside the project it has open, so the
   * binding's own `workspace_id` is the right project for them. A **replayed**
   * card is different: it names the project the run produced the file in, which
   * may not be the one open now (the user switched directory, or is looking at a
   * different project's history). Sending the old relative path with the *new*
   * workspace id would resolve it against whatever project is open -- a
   * same-named file in another project, silently the wrong bytes.
   *
   * So the owner travels with the request, and a mismatch is a refusal rather
   * than a re-resolution. `expectedWorkspaceId` is absent for live cards and for
   * callers that never learned the owner, which keeps the previous behaviour.
   */
  function wrongProject(expectedWorkspaceId) {
    if (!expectedWorkspaceId) return null;
    var ctx = binding();
    if (!ctx) return null;
    if (String(ctx.workspace_id) === String(expectedWorkspaceId)) return null;
    return actionRefusal(
      'wrong_project',
      'this file was produced in another project, which is not the one open now');
  }

  /** The host's own action surface, or `null` when there is none. */
  function actionHost() {
    var bridge = host();
    return (bridge && typeof bridge.projectAction === 'function') ? bridge : null;
  }

  /**
   * One *system* action on one local file (task 9.4).
   *
   * `open`, `reveal`, `copyPath` and `saveAs` are the four things a page cannot
   * do itself, and all four go through this one call so the panel has one place
   * to ask and one shape to read. The `workspace_id` is added here, from the
   * live binding -- the panel knows a *path*, never which project it belongs to.
   *
   * The answer is the host's: `{opened}`, `{copied}`, `{saved}` on success, and
   * `{ok:false, code, message}` on refusal. Nothing is thrown: a file that is
   * gone (`not_found`), a project whose grant was revoked (`stale_context`), a
   * machine with no application for this kind of file (`no_application`) and a
   * dismissed dialog (`cancelled`) are all *answers* the panel shows as they are.
   *
   * @param {string} action - open | reveal | copyPath | saveAs
   * @param {string} path - the project-relative path the panel is showing.
   * @param {{expectedMtime?: number, acceptCurrent?: boolean,
   *   expectedWorkspaceId?: string}} [options]
   */
  function act(action, path, options) {
    var ctx = binding();
    var bridge = actionHost();
    var opts = options || {};
    if (!ctx || !bridge) {
      return Promise.resolve(actionRefusal('no_host', 'there is no local project in this session'));
    }
    var other = wrongProject(opts.expectedWorkspaceId);
    if (other) return Promise.resolve(other);
    var target = String(path || '');
    if (!target) return Promise.resolve(actionRefusal('invalid_request', 'a system action needs a file path'));
    var params = { workspace_id: String(ctx.workspace_id), path: target };
    // The version the panel read, so a copy of a file that changed since the
    // user last saw it is refused rather than taken silently.
    if (typeof opts.expectedMtime === 'number' && isFinite(opts.expectedMtime)) {
      params.expected_mtime = opts.expectedMtime;
    }
    if (opts.acceptCurrent === true) params.accept_current = true;
    return bridge.projectAction(action, params).catch(function (err) {
      return actionRefusal((err && err.code) || 'device_error',
        (err && err.message) || 'the local project action was refused');
    });
  }

  /** Whether the host can hand a local file to the system at all. */
  function canAct() {
    if (!binding() || !actionHost()) return Promise.resolve(false);
    return host().canUseProjectActions().catch(function () { return false; });
  }

  /** The host's own preview surface, or `null` when there is none. */
  function previewHost() {
    var bridge = host();
    return (bridge && typeof bridge.projectPreview === 'function') ? bridge : null;
  }

  /**
   * The protected preview of one local file (task 9.5).
   *
   * The panel knows a *path*; the host owns the grant and the disk. The answer is
   * the host's: `{ok: true, name, kind, size, url, expires_at}` when the file may
   * be previewed, and `{ok: false, code, message}` when it may not
   * (`unsupported_type` for a kind this surface cannot render, `not_found` for a
   * file that is gone, `stale_context` for a project whose grant was revoked,
   * `limit_exceeded` for a file too large to pull into the main process).
   *
   * `url` is short-lived and single-file. It is not a path and not a token the
   * page can mint again, so the panel must not store it: it embeds the URL in
   * the element that renders the file and asks again next time.
   *
   * @param {string} path - the project-relative path the panel is showing.
   * @param {{expectedWorkspaceId?: string}} [options]
   */
  function preview(path, options) {
    var ctx = binding();
    var bridge = previewHost();
    if (!ctx || !bridge) {
      return Promise.resolve(actionRefusal('no_host', 'there is no local project in this session'));
    }
    var other = wrongProject((options || {}).expectedWorkspaceId);
    if (other) return Promise.resolve(other);
    var target = String(path || '');
    if (!target) return Promise.resolve(actionRefusal('invalid_request', 'a preview needs a file path'));
    return bridge.projectPreview({ workspace_id: String(ctx.workspace_id), path: target })
      .catch(function (err) {
        return actionRefusal((err && err.code) || 'device_error',
          (err && err.message) || 'the local preview was refused');
      });
  }

  /** Whether the host can preview a local file's content at all. */
  function canPreview() {
    if (!binding() || !previewHost()) return Promise.resolve(false);
    return host().canUseProjectPreview().catch(function () { return false; });
  }

  /** Every string in a host event's list, defensively. */
  function paths(value) {
    if (!Array.isArray(value)) return [];
    return value.map(function (item) { return String(item || ''); });
  }

  /**
   * Follow the local project for changes (task 9.3).
   *
   * The host emits on the same event channel the shell already listens on; this
   * translates the two event kinds the panel acts on and drops everything else
   * -- including an event from a *different* binding. That last filter is the
   * interesting one: a re-picked project produces a new binding id, and a report
   * that is still in flight from the old watch describes a directory the user
   * has already replaced. Applying it would refresh the wrong listing.
   *
   * @param {function({ended: boolean, reason: string, changed: string[],
   *   removed: string[], truncated: boolean})} handler
   * @returns {function} unsubscribe
   */
  function watch(handler) {
    if (typeof handler !== 'function') return function () {};
    var bridge = host();
    if (!bridge || typeof bridge.onHostEvent !== 'function') return function () {};
    var off = bridge.onHostEvent(function (payload) {
      var event = payload || {};
      var ctx = binding();
      if (!ctx) return;
      if (event.type === 'projectWatchEnded') {
        if (String(event.workspace_id || '') !== String(ctx.workspace_id)) return;
        handler({ ended: true, reason: String(event.reason || 'stopped'), changed: [], removed: [], truncated: false });
        return;
      }
      if (event.type !== 'projectChanged') return;
      if (String(event.workspace_id || '') !== String(ctx.workspace_id)) return;
      if (ctx.binding_id && String(event.binding_id || '') !== String(ctx.binding_id)) return;
      handler({
        ended: false,
        reason: '',
        changed: paths(event.changed),
        removed: paths(event.removed),
        truncated: !!event.truncated
      });
    });
    return (typeof off === 'function') ? off : function () {};
  }

  var api = {
    /** Wire the panel's own classifiers and the current desktop binding. */
    configure: function (options) {
      var o = options || {};
      if (typeof o.binding === 'function') bindingProvider = o.binding;
      if (typeof o.kindOf === 'function') classify.kindOf = o.kindOf;
      if (typeof o.editable === 'function') classify.editable = o.editable;
      if (typeof o.previewable === 'function') classify.previewable = o.previewable;
    },

    /**
     * The directory the panel opens on, or `null` when the backend decides.
     *
     * A local project has no `agents/<id>/` and no per-member subtree: its root
     * *is* the project the user opened, so the landing is that root and the
     * Agent-folder arithmetic must not run.
     */
    landing: function () {
      return binding() ? '' : null;
    },

    applies: applies,
    handle: handle,
    write: write,
    watch: watch,
    act: act,
    canAct: canAct,
    preview: preview,
    canPreview: canPreview,
    binding: binding
  };

  global.CowProjectSource = api;
})(typeof window !== 'undefined' ? window : this);
