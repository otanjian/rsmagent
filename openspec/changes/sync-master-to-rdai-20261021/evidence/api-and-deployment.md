# Web API upgrade and deployment notes

Status: candidate instructions, not a production rollout record. Final Web model acceptance is still pending. Desktop runtime/build/package validation is deferred to task 12.

## Client migration

Database mode uses the existing IAM service-account API key. A shared Web password or upstream `external_api_token` is not an authentication fallback. A tenant administrator creates a key for an authorized service identity in the Web API settings; the plaintext is displayed only at issuance. Store it in the client's secret store. Listing/reloading does not reveal it; rotation invalidates the previous key immediately. The deployment enable switch is a platform action and does not grant a tenant, Agent, or model permission.

Use `Authorization: Bearer <service-account-key>` with `/v1/models` and `/v1/chat/completions`. `/v1/models` returns only the currently authorized catalog. `agent_id` in the body and `X-Agent-ID` must agree when both are supplied. Omission resolves the tenant's default Agent; aliases are normalized before locking and persistence. An explicit model must be authorized. Omitted models use the existing session/Agent/role/global defaults, intersected with the live model catalog; conflicting role defaults return 409, denied defaults 403, unavailable identity services 503. A failed authorization is never a reason to call a different provider.

Example payload (replace model and Agent with authorized values):

```json
{"agent_id":"primary","model":"deepseek-chat","messages":[{"role":"user","content":"Hello"}],"stream":true}
```

Omit `conversation_id` for stateless requests. For continuity, supply the same nonempty `conversation_id` and the intended Agent on later requests. The server namespaces history and locks by trusted tenant, service identity, Agent and conversation. **Breaking change:** the legacy `user` field no longer selects a persistent conversation; without `conversation_id`, each request uses only its supplied message history. Clients that previously reused `user` must migrate to an explicit `conversation_id` before upgrading. Old history is not reassigned by guessing an owner. Two accounts using the same external conversation name retain separate histories. SSE preserves text, reasoning, tool process and usage events; disconnect, timeout and cancellation release the request lock.

## Upgrade

1. Complete WB-03/WB-12 real-provider acceptance and the remaining Web checklist, then review the exact source/target/candidate digests before marking the PR ready or merging into `rdai`. An explicitly blocked draft PR may preserve the candidate and evidence beforehand; it is not acceptance or deployment authorization.
2. Back up the current release, configuration, IAM database, conversation databases and each tenant's application/data directories using the existing backup procedure. Record the corresponding versions. Pause writes when taking the consistent recovery snapshot.
3. Install the candidate's locked dependencies and actual Web resources. The separate RSM Code Web build is required when that integration is deployed; keep its build/source digest paired. Desktop packaging is not part of this release's evidence.
4. Start against the backed-up test copy first. Conversation schema extension retains existing rows and adds the artifact retry watermark. Existing artifact indexes gain trusted tenant/owner/file-source scope. Unknown legacy ownership stays isolated. Run migration again to verify idempotence; exercise interruption recovery before production writes.
5. Personal menus live in the existing private tenant application directory, keyed by the authenticated user. Do not copy an old global menu to every member. Missing or corrupt personal JSON falls back to defaults; an unsuccessful atomic save keeps the last valid file.
6. Keep existing deployment/authorization gates and platform qualifications. New menus do not confer resource permission. Preserve user-supplied branding; only the rdai default title changes to 业务工作台 / 業務工作台 / Business Workspace. SAP titles and master's shared pages keep their meaning.
7. For direct HTTPS, configure the existing certificate/key settings together and validate them before startup. Invalid/missing material must fail startup. Reverse-proxy deployment keeps its current origin and forwarded-header trust configuration; do not accept arbitrary proxy headers.
8. Smoke test login/tenant selection, navigation, chat and a real tool/file read, workspace save/download, artifact preview, knowledge/skills, scheduler notification and a service-account client after deployment. Record the deployed commit and result; local browser acceptance alone does not prove deployment.

## Recovery

Atomic config/model saves publish runtime values only after disk replacement; failures retain the last working credentials and routing. Artifact indexing retries committed messages without writing the chat twice. Neither renaming nor removing an artifact index deletes the source file.

If startup fails before any new writes, stop the candidate and restore the matching code/config/database snapshot together. If the candidate may have written messages, files, tasks or external side effects, do not overwrite those with an old snapshot. Quiesce writes, preserve both versions and use the existing recovery/migration path after checking side effects. A source-update health failure likewise must not restore a stale tree over possible new writes. Never delete another tenant's data or infer ownership while recovering.
