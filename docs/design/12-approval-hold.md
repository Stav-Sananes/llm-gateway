# 12 — The approval hold

**Goal:** when a Cedar policy answers `require_approval`, the tool call waits for a human instead of being refused. An admin sees exactly what would run, then approves or rejects it. The call runs only after an approval, at most once, and every step is on the tenant's audit chain.

This is build step 9.

## How a held call goes

```
MCP tools/call fs__move_file {source, destination}
  Cedar: fs-move-needs-approval → require_approval     audit tool.call.decided
  ApprovalHold.wait:
     hold: INSERT approvals (pending, expires_at = now + 120 s)
           (refused at once if the tenant already has 10 pending)
     poll every 0.5 s ─────────────────────────────┐
                                                   │   admin: llm-gateway approvals list
                                                   │          llm-gateway approvals show <id>
                                                   │          llm-gateway approvals approve <id>
                                                   │   UPDATE … SET status='approved'
                                                   │          WHERE id = … AND status = 'pending'
     sees approved ◄───────────────────────────────┘
  audit tool.call.approval {approval_id, status, decided_by, note}
  approved → upstream → audit tool.call.completed → result to the client
  rejected → "Rejected by dana: <note>"     expired → "Nobody approved this call within 120 seconds."
```

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **The client's request waits; there is no callback.** | MCP's `tools/call` is a request and its response. Keeping the request open is the only way an unmodified MCP client gets the result of an approved call. | Returning "pending" and having the client poll a second tool: every client would need to know the protocol. |
| **Held calls are parked in Postgres, and every move out of `pending` is one conditional `UPDATE … WHERE status = 'pending'`.** | The admin's CLI and the waiting gateway are different processes. When the admin approves just as the hold times out, exactly one update wins, and both read back the same outcome. So a call never runs after the client was told it expired, and an approval is never lost. A test races 8 decisions against each other on real Postgres. | Redis keys with a TTL: a second source of truth next to the audit chain, which lives in Postgres. |
| **The waiting request polls (0.5 s).** | Simple, it survives a dropped connection, and it costs one indexed primary-key read per held call per half second. | `LISTEN/NOTIFY`: lower latency, but a dedicated connection per gateway and a missed notification to handle. |
| **The stored arguments are exactly what runs.** The admin reads them with `approvals show`; the waiting gateway runs the call it holds in memory, with the same arguments, and nothing else. | Approving one thing and running another would make the approval worthless. | Storing a summary: the admin would approve what they can't see. |
| **The approval is audited before the call runs** (`tool.call.approval`: id, status, who, note), as the policy decision was. If the audit log is down, the call doesn't run. | The chain has to show who let each held call through. | Auditing from the CLI: then the CLI would need to be trusted with the chain, and the chain would get an entry even when nothing ran. |
| **Holds end in `expired` (nobody decided in time) or `abandoned` (the client left, or the store failed while waiting).** Both are set by the gateway (`decided_by = gateway`). After that, an approval is refused with "already abandoned" or "expired". | An approval nobody waits for runs nothing, and the admin must not be told it worked. | Leaving the row pending: it would be listed, approved, and nothing would happen. |
| **A pending row past its `expires_at` can't be approved.** The desk marks it expired. Such rows (their gateway crashed before closing them) don't count against the tenant's limit and aren't listed. | The gateway that would have run it is gone. | Trusting `status` alone: a crashed gateway would leave approvable ghosts. |
| **At most 10 calls per tenant wait at once** (configurable). The count and the insert run under a per-tenant advisory lock. | Each held call keeps a client request and a gateway task open for up to two minutes. Without a limit, one tenant could hold open as many as it likes. With the lock, concurrent holds can't all slip under the limit (tested with 10 at once against a limit of 3). | No limit. A global limit: one tenant could use it all. |
| **The decider is named:** `--by` (the OS login by default). `gateway` and an empty name are refused. | "Who approved this?" must have an answer on the chain. | Anonymous decisions. |
| **CLI only, no HTTP approval API.** | Tenants authenticate with API keys, and a tenant must not approve its own held calls. The gateway has no admin identity yet. The CLI runs where the operator already has database credentials. | An admin endpoint behind a shared static token: a new credential to leak, guarding the most sensitive action there is. |
| **Timeout 120 s by default (at most 3,600), poll 0.5 s.** | Long enough for someone watching to act. The MCP client's request timeout has to be longer, or the client gives up first and the hold is abandoned. | Hours: a client request can't sensibly stay open that long. |

## Configuration

```bash
GATEWAY_APPROVAL_TIMEOUT_S=120
GATEWAY_APPROVAL_POLL_S=0.5
GATEWAY_APPROVAL_MAX_PENDING_PER_TENANT=10
```

## Known gaps

- **Nobody is notified that a call is waiting.** The admin has to look (`approvals list`). A webhook, or a Slack message with the approval's id, would be the next step.
- **No HTTP API or UI for approvals**: see the decision above. It needs an admin identity separate from tenant keys.
- **A gateway restart ends every held call it was serving**, and their clients get an error. The held calls live in Postgres, but the waiting requests don't. A clean shutdown marks them abandoned. After a crash they stay pending until they expire, and approving one in that window approves nothing: the CLI says "approved", but the chain gets no `tool.call.approval` and nothing runs.
- **Stored arguments aren't redacted.** The admin needs to see them, so they sit in the `approvals` table in full, as they do in the audit chain unless secret keys are configured.
- **No progress notifications to the client while it waits.** A client with a short request timeout gives up first.

## Interview questions to be ready for

1. *The admin approves at the same moment the hold times out. What does the client see, and does the call run?*
2. *Why does the gateway, not the CLI, write the approval to the audit chain?*
3. *How do you stop one tenant from tying up the gateway with held calls?*
