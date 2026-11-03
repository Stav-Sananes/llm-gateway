# 07 — Argument-level tool policy with Cedar

**Goal:** decide, for every MCP tool call, whether it may run, must be refused, or must wait for a human, based on the tool **and its arguments**: `sql.query` only for `SELECT` on allowed schemas, `fs.write_file` only under `/sandbox`. This is the gateway's main difference from LiteLLM (SPEC §2).

Step 5 is split into three PRs: the audit chain (06), this policy engine, and the MCP aggregator that uses both.

## How a call is decided

```
ToolCall(tenant, server, tool, arguments)                          (policy/model.py)
   │
   ├─ derive_facts(arguments)                                       (policy/facts.py)
   │     paths.<arg>   path arguments, normalized: "/sandbox/../etc/passwd" → "/etc/passwd"
   │     sql           parsed with sqlglot: parsed, only_select, statements, tables, schemas
   │
   └─ CedarPolicyEngine.decide                                      (authz/cedar_engine.py)
         principal Tenant::"<id>" {name}   action Action::"call_tool"
         resource  Tool::"server__tool" {server, name}
         context   {arguments, paths, sql}
         ─► Allow / Deny from Cedar
             any evaluation error               → DENY
             Allow via a @decision("require_approval") permit → REQUIRE_APPROVAL
```

`policy/` holds the vocabulary and the facts; it imports no Cedar. `authz/` is the Cedar adapter.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Cedar, evaluated in-process** (cedarpy). | Policies are data a security reviewer can read, with deny-by-default and forbid-beats-permit built in; no network hop per tool call. | Hand-written Python rules: every rule is code, review needs a developer. OPA: a sidecar and a second language. |
| **Policies judge derived facts, not raw text.** Paths are normalized; SQL is parsed. | `like "/sandbox/*"` on the raw string lets `/sandbox/../etc/passwd` through; `SELECT 1; DROP TABLE t` starts with SELECT. | Regexes on arguments: always one bypass behind. |
| **`only_select` is strict**: every statement must be a SELECT (or UNION of SELECTs) with no write anywhere inside (CTE `DELETE`, `SELECT … INTO`, `COPY`), and the text must parse. | A policy that allows "SELECT" must mean "reads only". Anything the parser can't classify is not a read. | Checking the first keyword. |
| **Fail closed on evaluation errors.** | Cedar skips a policy that errors (e.g. reads a missing attribute). A skipped *forbid* would silently allow the call. | Trusting Cedar's skip: a typo in a forbid turns into an allow. |
| **`require_approval` is an annotation on a permit** (`@decision("require_approval")`); if any satisfied permit carries it, the call needs approval. | Cedar only answers Allow/Deny; the annotation keeps the third outcome in the same file, readable next to the rule. "Any" is the cautious reading when rules overlap. | A second policy set for approvals: two files to keep consistent. |
| **The policy file is checked at startup** (`llm-gateway policy check` beforehand); an empty or broken file stops the gateway. | A deploy with a typo should fail loudly, not deny every call at 3 a.m. | Loading lazily. |
| **Readable reasons**: `@id("…")` names every policy; decisions carry the ids that decided. | The audit log (5c) records *why* a call was allowed or denied. | Cedar's positional `policy0`, `policy1`. |

## The default policies

`src/llm_gateway/authz/default.cedar`, for the demo servers in 5c (replace with `GATEWAY_POLICY_FILE`):

- `sql-select-allowed-schemas`: `sql__query` when it only reads `analytics` and `public`.
- `fs-read`: read-only filesystem tools.
- `fs-write-sandbox`: write, edit, mkdir under `/sandbox`.
- `fs-move-needs-approval`: moves within `/sandbox`, each approved by a human (step 9).
- `fs-never-env-files`: forbid any `*.env` path, whatever else permits it.

## Known gaps (on purpose)

- **SELECT is not the same as harmless.** `SELECT pg_sleep(60)`, `SELECT set_config(…)` and similar functions pass `only_select`. The demo SQL server (5c) must also run every query in a `READ ONLY` transaction with a statement timeout; policy is one layer, not the only one.
- **No Cedar schema validation** yet: a typo in an attribute name becomes a runtime evaluation error (which denies), not a startup error.
- **Path facts only for known argument names** (`path`, `file`, `source`, `destination`, …); a server that calls its path argument something else needs a policy on `context.arguments` or a new name in `policy/facts.py`.
- Symlinks inside `/sandbox` pointing outside it are the filesystem server's job to refuse; normalization is purely textual.

## Interview questions to be ready for

1. *Why parse the SQL instead of checking that it starts with SELECT?*
2. *Cedar skips a policy that errors. Why is that dangerous, and what does the gateway do about it?*
3. *How do you express "allowed, but a human must approve" in a language that only says allow or deny?*
