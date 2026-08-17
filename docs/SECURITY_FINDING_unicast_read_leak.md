# RESOLVED LOCALLY: unicast payloads are redacted for non-addressees

**Status: fixed in the local coordinator and hosted web read paths; production
deployment verification remains outstanding.** Found 2026-08-06 by running the
real service over HTTP, this contradicted the product's "scoped consent" claim
directly and was launch-blocking until the read paths were repaired.

The coordinator now persists the resolved recipient list and redacts targeted
`room.message` payloads for other non-addressees during replay. An authenticated
originator can audit its own send even when `exclude_sender` removes it from the
delivery target list. The hosted web poll and event-log paths apply the same
rule while leaving lifecycle events visible.
Regression coverage exists in `tests/test_room_stale_delivery.py` and
`tests/test_webapp_rooms.py`. The deployed service has not been updated or
re-probed for the current local commit, so this document makes no production
resolution claim.

## Historical observation

`room_send` records the recipient list but nothing enforces it on **read**. Any member of the
room can poll and receive the full body of a message addressed to someone else.

## Reproduction (verbatim, against `service.py` on 127.0.0.1:18788)

Three agents — `a1`, `a2`, `a3` — join the same room via the same link. `a1` sends a unicast
to `a2` only. Routing is correct:

```
send seq: 6   receipts -> ['a2']
```

Then `a3`, who was **not** a target, polls:

```
>>> a3 was NOT a target. a3's poll contains the secret? True
   a3 sees seq 6 payload: {"payload": {"text": "SECRET-ONLY-FOR-A2-9f3d"},
                           "target_spec": "a2", "targets": ["a2"]}
```

`a3` receives the plaintext body, plus the addressing metadata proving it was never meant
for them.

## Historical cause

- `room_send` (`src/weft_cloud/rooms.py`) appends via
  `_append_event(..., "room.message", {"payload": ..., "target_spec": ..., "targets": targets})`.
  The recipients live *inside the payload*, as data.
- `poll` (`src/weft_cloud/rooms.py`) reads:
  ```sql
  SELECT * FROM cloud_room_event_log
  WHERE tenant_id = ? AND room_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?
  ```
  and returns every row's full payload. **There is no predicate on `targets` anywhere in the
  read path.** Delivery intent is recorded, never enforced.

## Required fix

Filter on read. A non-target must never receive the body. Decide deliberately between:

- **Redacted envelope** — non-targets see that an event occurred at seq N (preserving the
  shared ordered timeline) with the body withheld; or
- **Full omission** — non-targets do not see the event at all.

The first keeps every agent's sequence consistent, which matters because ordered delivery is
also a product claim. The second is simpler but makes members' views of `seq` diverge — if you
choose it, make sure `next_seq`/`cursor_head` semantics still hold.

Enforce it in SQL or immediately around the query, not in a caller — every read path must
inherit it.

## Regression test (must fail before the fix)

`a1` unicasts a distinctive string to `a2`; `a3` polls; assert the string appears **nowhere**
in `a3`'s response. Add the group case too: a member outside the addressed group must not read
the body.

## Related

The nested shape `payload.payload.text` shown above is also why
`scripts/prove-multiagent.py` step 6 reports a false FAIL — it asserts `payload["text"]`
instead of `payload["payload"]["text"]`. The send itself was working correctly.
