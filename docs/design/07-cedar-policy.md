# 07 — Argument-level tool policy with Cedar

**Goal:** decide, for every MCP tool call, whether it may run, must be refused, or must wait for a human, based on the tool **and its arguments**: `sql.query` only for `SELECT` on allowed schemas, `fs.write_file` only under `/sandbox`. This is the gateway's main difference from LiteLLM (SPEC §2).

Step 5 is split into three PRs: the audit chain (06), this policy engine, and the MCP aggregator that uses both.

## How a call is decided

```
ToolCall(tenant, server, tool, arguments)                          (policy/model.py)
   │
   ├─ derive_facts(arguments)                                       (policy/facts.py)
   │     paths.<arg>       one path argument, normalized: "/sandbox/../etc/passwd" → "/etc/passwd"
   │     path_count, path_roots, path_extensions
   │                       sets over every path in every path argument (single or listed),
   │                       so rules can say "all under /sandbox", "none ends in .env"
   │     sql               parsed with sqlglot: parsed, only_select, statements, tables,
   │                       schemas, unknown_functions
   │     unsafe path (relative, control chars, backslash, surrounding spaces) → DENY, no Cedar
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
| **`only_select` is strict**: every statement must be a SELECT (or UNION of SELECTs) with no write anywhere inside (CTE `INSERT`/`UPDATE`/`DELETE`, `SELECT … INTO`, `COPY`), and the text must parse (including input too deeply nested to parse). Exactly one SQL argument, as a string; anything else is unparsed. | A policy that allows "SELECT" must mean "reads only". Anything the parser can't classify is not a read. Two SQL arguments: we can't know which one the server runs. | Checking the first keyword. |
| **Unknown functions are listed** (`unknown_functions`); the default SQL policy requires none. Unqualified tables get the schema `(unqualified)`, never `public`. | `SELECT dblink_exec(…, 'DROP TABLE …')` and `pg_read_file('/app/.env')` are SELECTs. Postgres resolves unqualified names through `pg_catalog` first, so `pg_authid` is not "public". | Trusting `only_select` alone; assuming the default search path. |
| **Path rules work on sets** (`path_roots`, `path_extensions`, `path_count`); every dotted part of a name counts as an extension, lowercased. **Paths that can't be judged are refused** before Cedar: relative, control characters (NUL, newline), backslashes, surrounding spaces. | A reviewer reached `.env` through a list argument (`paths`), a move's `destination`, `.ENV`, `.env.local` and `"/app/.env\n"`. A relative path resolves against the server's working directory, which the gateway can't see. | `context.paths.path like …`: one argument, one string, one bypass per missed shape. |
| **Fail closed on evaluation errors.** | Cedar skips a policy that errors (e.g. reads a missing attribute). A skipped *forbid* would silently allow the call. | Trusting Cedar's skip: a typo in a forbid turns into an allow. |
| **`require_approval` is an annotation on a permit** (`@decision("require_approval")`); if any satisfied permit carries it, the call needs approval. | Cedar only answers Allow/Deny; the annotation keeps the third outcome in the same file, readable next to the rule. "Any" is the cautious reading when rules overlap. | A second policy set for approvals: two files to keep consistent. |
| **A policy file is refused when loaded** if it doesn't parse, is empty, reuses an `@id`, has an unknown `@decision` value or puts `@decision` on a forbid; `llm-gateway policy check` runs the same checks before a deploy (the gateway loads it at startup (since 5c)). | Annotation mistakes would otherwise change decisions silently (`@decision("REQUIRE_APPROVAL")` would mean plain allow). | Loading lazily; ignoring unknown annotations. |
| **`decide()` never raises**: unsafe arguments, values Cedar can't represent and engine errors are all DENY with a reason. | The port's contract is "fail closed"; an exception would become a 500 in the aggregator, or worse, be caught as "no decision". | Letting errors propagate. |
| **Readable reasons**: `@id("…")` names every policy; decisions carry the ids that decided. | The audit log (5c) records *why* a call was allowed or denied. | Cedar's positional `policy0`, `policy1`. |

## The default policies

`src/llm_gateway/authz/default.cedar`, for the demo servers in 5c (replace with `GATEWAY_POLICY_FILE`):

- `sql-select-allowed-schemas`: `sql__query` when it only reads schema-qualified `analytics`/`public` tables with known functions.
- `fs-read`: read-only filesystem tools.
- `fs-write-sandbox`: write, edit, mkdir under `/sandbox`.
- `fs-move-needs-approval`: moves within `/sandbox`, each approved by a human (step 9).
- `fs-never-env-files`: forbid any path with a `.env` part (`.env`, `prod.ENV`, `.env.local`), in any argument, whatever else permits it.

## Known gaps (on purpose)

- **SELECT is not the same as harmless**, even with known functions only: a heavy join can still hurt. The demo SQL server (step 10) must run every query in a `READ ONLY` transaction with a statement timeout, as a role without `dblink`, `lo_*` or `pg_read_*`; policy is one layer, not the only one.
- **No Cedar schema validation** yet: a typo in an attribute name becomes a runtime evaluation error (which denies), not a startup error.
- **Path facts only for known argument names** (`path`, `paths`, `file`, `files`, `source`, `destination`, …). A write tool whose path argument has another name is denied (no path to check); a read tool is not, so a server with unusual names needs its own policy or a new name in `policy/facts.py`.
- Symlinks inside `/sandbox` pointing outside it are the filesystem server's job to refuse; normalization is purely textual.

## Interview questions to be ready for

1. *Why parse the SQL instead of checking that it starts with SELECT?*
2. *Cedar skips a policy that errors. Why is that dangerous, and what does the gateway do about it?*
3. *How do you express "allowed, but a human must approve" in a language that only says allow or deny?*
