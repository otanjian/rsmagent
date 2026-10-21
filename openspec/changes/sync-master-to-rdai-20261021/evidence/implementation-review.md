# Candidate integration review (Web acceptance pending)

Fixed source: `ee2dc0521714fc144e0c9923f363147fe865ad2f`; target: `c9bd4942261156f56c02ffd65e4d81c6153e1594`.

The 876 changed source paths are classified in `source-classification.json`. Files with conflicts also retain the original hunk decisions in `conflict-decisions.json`. Classification records integration disposition, not successful acceptance. The final local gate is bound to runtime digest `1305d2af4f6649de916faff7b9c96240b27cf8775536fefd7c2e6c33f93207da`; real-provider acceptance remains pending.

The shipped Web shell now loads master's split core/chat/view modules and templates. rdai retains the identity bootstrap, authoritative route registry, owner/tenant consumers, scoped preferences, business pages, controlled source updater and vendored assets. `console.js`, the old fork router and `console.css` remain historical source only and are forbidden in the loaded asset contract. Both namespaces remain independently mapped; editing a shared module requires a source-blob/candidate-SHA256-bound integration record.

The default business region is 业务工作台 / 業務工作台 / Business Workspace. Master has no built-in `view=workbench`; conversation stays `chat`, rdai Agent directory stays `agent-workbench`, SAP retains its own title. Custom branding values are not migrated.

Artifacts use the existing conversation database with exact tenant/owner/Agent/file-source predicates and a retry watermark. Menu JSON uses atomic replacement inside the member's existing private tenant application directory. No menu table or concurrent version protocol was introduced. OpenAI requests use IAM service-account keys and scoped conversation IDs. Shared API tokens and per-chat permission escalation remain disabled.

Skills retain resource identity and install qualification while adopting source file-tree/create/display metadata. Model discovery bounds redirects and body size. Context cards consume the existing snapshot and do not instantiate an Agent. TLS is applied at the actual fork startup before serving. Runtime model authorization fails closed if identity lookup fails.

Historical GAP-001–008 and partial capability/guest/Desktop work are retained. Their previous evidence does not certify this candidate. Desktop source conflicts are integrated; runtime/type/build/package/native checks remain deferred under task 12. GAP-007 and remote required-check blocking remain pending. A pre-existing missing desktop-remote-web-workbench spec was reproduced in an isolated target checkout; the invariant assertion remains intact.

Real provider Web/model/tool/file and two-service-account acceptance passed. See results/real-provider-status.json. Desktop and inherited CI enforcement remain unverified.

## Workstream consumers and tests

- W09: Startup configuration and deployment scripts; entry `config.py`; consumers `app.py`, `config-template.json`; checks `tests/test_web_tls.py`, `tests/test_http_policy.py`.
- W01: Web assembly, menu, artifacts, API, skills and TLS; entry `channel/web/route_registry.py`; consumers `channel/web/fork/runtime.py`, `channel/web/chat.html`; checks `tests/test_sync_master_web_contract.py`, `tests/test_sync_master_tls.py`, `tests/test_console_parity_browser.cjs`.
- W02: Desktop source integration; runtime validation deferred; entry `desktop/src/renderer/src/api/client.ts`; consumers `desktop/src/renderer/src/pages/SkillsPage.tsx`, `desktop/src/renderer/src/pages/settings/ChatApiCard.tsx`; checks `tests/test_desktop_auth_flow.py`.
- W03: Agent, memory, tools, scheduler and lifecycle; entry `agent/chat/service.py`; consumers `bridge/agent_bridge.py`, `channel/web/fork/handlers/chat.py`; checks `tests/test_chat_model_fallback.py`, `tests/test_artifact_index.py`.
- W04: Provider, Responses and voice transports; entry `models/openai_compatible_bot.py`; consumers `bridge/agent_bridge.py`, `voice/voice.py`; checks `tests/test_openai_chat_api.py`, `tests/test_chat_model_fallback.py`.
- W05: IM and attachment lifecycle with guest authorization retained; entry `channel/chat_channel.py`; consumers `channel/wecom_bot/wecom_bot_channel.py`; checks `tests/test_route_registry.py`.
- W06: CLI, atomic config, backup and shared dependencies; entry `config.py`; consumers `bridge/agent_initializer.py`; checks `tests/test_cli_backup.py`, `tests/test_http_policy.py`.
- W07: Upstream behavior assertions and regression fixtures; entry `tests/test_upstream_sync_gate.py`; consumers `scripts/check-upstream-sync.py`; checks `tests/test_upstream_sync_gate.py`.
- W08: Documentation, packaging and vendored resources; entry `docs/extensions/chat-api.mdx`; consumers `channel/web/chat.html`, `requirements.txt`; checks `tests/test_upstream_sync_gate.py`.

## Implementation outcome

See [implementation-status.md](implementation-status.md) for actual results, remaining real-provider acceptance, baseline findings and deferred Desktop scope. The initial classification records above are retained as implementation provenance, not a blanket acceptance claim.

Final follow-up review: reuse the existing personal directory resolver for artifact paths; let the existing API handler authenticate SAK requests; preserve explicit HTTP 403/409 errors. The full-app regression tests both authorized and denied requests without substituting the authentication path.
