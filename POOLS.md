# Compute Pools — "Uber for computes" spec

Lets an app run its sessions on a **pool** of machines instead of only its
owner's. cerver becomes a compute *provider* (like Vercel/Cloudflare), backed
by relays that other people opt to lend. Configured per app.

This is a build spec grounded in the current code. File:line refs are to
`api/cerver-api` (the gateway) and `p_69_cerver_relay` (the relay).

---

## 1. The one rule that makes it safe

**The pool is API-billing only. Subscription never leaves the owner's machine.**

- **API billing** (per-token vendor keys) → the vendor key lives in the
  gateway; the borrowed machine asks the gateway for completions over a
  short-lived token. The key never lands on a stranger's disk. ✅ shareable.
- **Subscription** (the owner's local `claude login` / `codex login` OAuth) →
  it's a personal account bound to one machine, can't be proxied, and sharing
  it is vendor-ToS account-sharing. ❌ owner-only, always.

`isShareable(session)` therefore starts as: `session.billing === "api"` **and**
the contributor opted the relay in. Subscription sessions are auto-excluded for
free.

**Residual (separate lock):** API-proxy hides the *key*, not the *workload*. A
borrowed machine still sees the code/files a session touches. That's handled by
clean-room execution (below) + limiting which workloads are pool-eligible — not
by the billing rule.

---

## 2. What already exists (the seams we reuse)

| Need | Already there | Where |
|---|---|---|
| Per-app compute policy | `cerver_apps.routing_policy` (jsonb) + `GET/PUT /v2/apps/:slug/policy` | `index.ts:1811-1823`, `v2/routing/policy.ts` |
| Provider catalog | `getProviderCatalog` / `listProviders` (vercel, e2b, cloudflare, cerver_local) | `v2/computes/service.ts:82` |
| Pool key on records | `cerver_records` has `org_id` + `project_id` columns | `runtime/registry.ts:84` |
| Gateway-side model calls (the proxy) | `POST /v2/sessions/:id/run-llm` runs the model call IN the gateway with "federated model keys" | `index.ts:2922`, `sessions/service.ts:536`, `harnesses/{anthropic,openai}.ts` |
| Ephemeral scoped credentials | `bootstrap_credential.ts` mints expiring JWT / revocable `ck_sandbox_*` (reaped) | `providers/sandbox_relays/bootstrap_credential.ts` |
| Leases | `lease_lock.ts`, `waitForLeaseLockReady` | `providers/sandbox_relays/` |
| Relay repoints a harness to another endpoint | GrokProvider sets `ANTHROPIC_API_KEY` so the Claude CLI talks to api.x.ai | relay `computer_runtime/cli_runtime.py:63-68` |
| Per-session env injection into the harness | `extra_env` layer = "secrets passed through from cerver session metadata", wins on conflict | relay `cli_runtime.py:70-71,116` |
| Workspace isolation | worktrees / workspaces | relay `computer_runtime/{workspace,worktree_ops}.py` |

The owner boundary that we deliberately open:

- `getRegistered(computeId, ownerId)` throws if `record.ownerId !== ownerId`
  — `computes/service.ts:164,203,223`.
- `listRegisteredComputeRecords(env, ownerId)` → `WHERE owner_id = $2`
  — `registry.ts:455,631`.
- Session compute-pick lists only the caller's own computes —
  `sessions/service.ts:515`.

`scope` today means **local vs cloud**, NOT mine-vs-theirs:
`private` = a registered machine (`computes/service.ts:70`), `shared` = a cloud
provider (`:95`). We add a third meaning: `pool`.

---

## 3. Data model

No migration needed for the pool key — reuse `cerver_records.org_id` as the
**pool id**.

- **Contributed compute:** its compute record gets `scope: "pool"` and
  `org_id = <pool_id>`, plus `shareable: true`. `owner_id` stays the contributor.
- **Pool membership:** new rows `resource = "pool_members"` in `cerver_records`
  (same generic table, no migration): `{ pool_id, account_id, role:
  "contribute" | "consume" | "both" }`.
- **App policy:** extend `cerver_apps.routing_policy` with a `compute` block:

```jsonc
routing_policy.compute = {
  consume:    { providers: ["cerver_local", "vercel", "cerver"], order: [...] },
  contribute: { enabled: true, relays: "all" | ["comp_…"], shareable_when_possible: true }
}
```

- **Attribution (capture now, pay later):** stamp each session with
  `executed_on_compute_id`, `provider_owner_id`, `compute_seconds` (same pattern
  as the `app_id` stamp already shipped). Roll up `provider_minutes_by_owner`.
  Payouts are out of scope; just keep the ledger correct.

---

## 4. Gateway changes

1. **`cerver` provider.** Add `cerver` to the provider catalog. Its "provision"
   isn't a cloud sandbox — it **leases a `scope:"pool"` relay** the caller may
   consume (reuse `lease_lock.ts`). Consume shape mirrors Vercel:
   `compute: { provider: "cerver" }`.

2. **Open the owner guard → membership check.** In the 3 guards
   (`computes/service.ts:164,203,223`) allow when:
   `ownerId === record.ownerId` **OR** (`record.scope === "pool"` **AND**
   `isPoolMember(ownerId, record.org_id, "consume")`).
   Add `listPoolComputeRecords(env, poolId)` → `WHERE resource='computes' AND
   org_id=$1 AND scope='pool'`. Default stays `private` — pure opt-in, zero
   change for existing users.

3. **App-policy-driven selection.** Sessions already carry `app_id` (shipped).
   At compute-pick (`sessions/service.ts:515`) read that app's
   `routing_policy.compute.consume` to build the provider/relay set and order.

4. **Pool sessions are API-only + proxied.** When the chosen compute is a pool
   relay:
   - reject if `session.billing !== "api"`.
   - **Do NOT** inject raw vendor keys (skip / override `injectAccountCredentials`,
     `sessions/service.ts:393`).
   - mint a per-session **ephemeral scoped token** (`bootstrap_credential.ts`).
   - pass the relay, via session metadata → `extra_env`:
     `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL = <gateway proxy>` and the ephemeral
     token as the harness's API key.

5. **Model proxy endpoint.** Expose the existing in-gateway model call as an
   Anthropic/OpenAI-compatible proxy (`POST /v2/proxy/anthropic/*`,
   `/v2/proxy/openai/*`) authenticated by the ephemeral token. It resolves the
   *consumer's* federated/account vendor key (the run-llm resolver already does
   this, `sessions/service.ts:1229-1290`), forwards, meters, bills the consumer.
   This is "expose what run-llm already does," not a new model layer.

---

## 5. Relay changes (`p_69`)

1. **Proxy: ~free.** `cli_runtime.py` already repoints a harness to a non-vendor
   endpoint (Grok→xAI). Pool sessions just receive `ANTHROPIC_BASE_URL` +
   ephemeral token through the existing `extra_env` channel. Confirm `codex` /
   `glm` honor a base-URL override the way `claude` does.

2. **Clean-room mode (the one real build).** Today the harness env is built from
   the contributor's **host `os.environ`** (`cli_runtime.py:73`) **+ their
   Infisical vault** (`:100-103`) — both would leak the contributor's secrets
   into a stranger's job. Add a `pool_session=true` execution mode that:
   - does **not** inherit host env (start from a minimal base, not
     `os.environ.copy()`),
   - does **not** layer the contributor's Infisical secrets,
   - runs in a **throwaway workspace** (reuse worktree/workspace isolation),
   - uses **only** the gateway-injected `extra_env` (base-URL + ephemeral token),
   - enforces time/resource caps + instant revoke (lease + reaper).

That flag is the gate between "runs a foreign job" (mechanically already true)
and "runs a foreign job safely."

---

## 6. Phasing

- **Phase 0 — plumbing, no stranger trust.** App-policy `compute` block, `cerver`
  provider stub, `shareable` flag, `isShareable` stub. Pool contains only the
  owner's *own* shareable relays. Proves app-level routing end-to-end.
- **Phase 1 — invited contributors.** Open the guard to pool members; API-only +
  ephemeral token + gateway proxy + relay clean-room mode; attribution capture.
  Trust boundary = invited accounts.
- **Phase 2 — open marketplace.** Attestation/reputation, workload-exposure
  hardening (ephemeral checkout, egress limits), payouts/settlement, capability
  matchmaking, dynamic pricing, the extended `isShareable` feasibility function.

---

## 7. Open questions

- **Workload exposure.** Even API-proxied, a borrowed machine sees the session's
  code/files. Phase 2 gating: ephemeral checkout, egress controls, or restrict
  pool-eligible workloads. (Key-safety ≠ workload-safety.)
- **codex/glm base-URL override.** Confirmed for `claude`; verify the others in
  the relay.
- **Long interactive sessions vs leases.** Lease semantics for a multi-hour
  interactive session vs a one-shot run.
- **Attestation.** How a consumer trusts a contributor's machine isn't snooping
  (Phase 2).

---

## TL;DR

The engine, the pool key (`org_id`), the app policy surface, the gateway model
proxy, and the relay's endpoint-repoint are **already there**. The net new work
is: a `cerver` provider + a membership check replacing the owner guard + a
least-loaded lease scheduler + **a relay clean-room mode** — all scoped to
API billing, with subscription staying owner-only.
