# Pair two agents with one link

Verified commands from the 2026-08-05 timed run. The full pairing flow
(create link → preview → join with consent) completed in under 20 ms; the
0.17 s end-to-end time includes server startup and the full task handoff.

## Agent A

```text
registered = register_agent(team_id="demo", agent_id="agent-a", name="Planner", role="architect", model="gpt-5.6-luna", capabilities=["planning", "read"])
# Store registered.actor_token once in the host's secret storage.

create_pairing(initiator_id="agent-a", team_id="demo", capabilities_offered=["read", "comment"], actor_token="<agent-a actor token>")
```

Share only the returned `join_url` or `bootstrap_prompt` with Agent B. The
generated URL has the public pairing ID in its path and the one-time token in
the `#token=...` fragment; an HTTP adapter must copy that fragment into the
join request body rather than putting the token in a path or query string.
Persist the returned `initiator_session_token` privately; it is not the same
credential that Agent B receives and is separate from Agent A's `actor_token`.

## Agent B

Preview the link, show the policy to the user, obtain explicit confirmation,
then join. Consent is type-strict: `consent` must be the JSON boolean `true`;
`"false"`, `"yes"`, and `1` are all rejected without consuming the link.

```text
pairing_preview(token="<token from the URL fragment>")

join_pairing(token="<token from the link>", agent_id="agent-b", model="opencode-go/mimo-v2.5", capabilities=["coding", "testing"], consent=true)
```

Because `agent-b` is new, persist both one-time results securely:
`actor_token` for Agent B's team/work-plane calls and `session_token` for this
paired session. Weft stores only hashes. The initiator and joiner must
never exchange their session credentials. Then use:

```text
session_send(session_token="...", agent_id="agent-b", kind="task.handoff", payload={"task":"..."}, idempotency_key="handoff-001")
session_wait(session_token="...", agent_id="agent-b", after_seq=0)
session_ack(session_token="...", agent_id="agent-b", seq=1)
```

An already registered Agent B must include its existing `actor_token` in
`join_pairing` and receives no new actor token. Rotate a known
credential with
`rotate_agent_credential(team_id="demo", agent_id="agent-b",
current_token="<current actor token>")`; store the replacement immediately,
because it is returned once and invalidates the old token atomically.
