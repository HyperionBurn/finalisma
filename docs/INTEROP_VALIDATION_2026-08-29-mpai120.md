# MPAI-120 cold first-run journey — 2026-08-29

## Verdict

**PARTIAL — the room works, but the invited non-owner cannot leave through the
live UI.** A brand-new owner and a brand-new invited identity completed fresh
signup, room creation, invite acceptance, and two-way messaging against live
production. The journey stopped at the required leave step because the guest
room rendered no leave button or link. The owner-only `Close room` control was
used in bounded cleanup; it closes the room for everyone and is not an
equivalent guest leave action.

This is one cold browser journey, not a fixture or seeded account. No product
code or copy was changed during the observation.

## Timed journey

Durations are wall-clock automation time from this run, measured from each
action until its observable page state. They exclude the time a human would
spend reading, deciding, or typing; the total is therefore a lower bound for a
developer's real time-to-value.

| Step | Result | Duration |
| --- | --- | ---: |
| Owner cold Chrome profile | pass | 0.553 s |
| Owner open landing page | pass | 2.161 s |
| Owner find and follow “Create a free account” | pass | 0.114 s |
| Owner complete signup | pass | 0.849 s |
| Owner open “New room” | pass | 0.402 s |
| Owner fill room name, size, and lifetime | pass | 0.267 s |
| Owner create room and reveal invite | pass | 0.274 s |
| Guest cold Chrome profile | pass | 0.512 s |
| Guest open landing page | pass | 1.968 s |
| Guest find and follow “Create a free account” | pass | 0.118 s |
| Guest complete signup | pass | 0.850 s |
| Guest open invite | pass | 0.950 s |
| Guest accept “Join this room” | pass | 0.245 s |
| Guest room load | pass | 0.267 s |
| Owner open the room | pass | 0.931 s |
| Owner send first message | pass | 0.005 s |
| Guest receive first message | pass | 3.155 s |
| Guest send reply | pass | 0.006 s |
| Owner receive reply | pass | 0.520 s |
| Guest leave room | **blocked** | 0.002 s |

The measured journey elapsed `14.152 s` through the failed leave attempt. The
owner then closed the disposable room in `finally` cleanup; that cleanup was
successful but was not included in the elapsed value above. The room was cap 2
with a one-hour lifetime selected for bounded cleanup. The invite and account
identifiers were redacted; only their lengths and safe markers were retained.

## What worked

- The public landing CTA led to `/signup` for both fresh identities.
- Each fresh signup reached the authenticated `/app` dashboard.
- The owner created a real room and received a real join link. The form clearly
  explained that the owner consumes one seat and that the link acts as the
  authorization.
- The guest opened the real invite while signed into the second fresh account,
  chose **Join this room**, and reached the shared room.
- The owner sent `mpai120-owner-message`; the guest rendered it.
- The guest sent `mpai120-guest-message`; the owner rendered it.
- The owner then closed the room, invalidating the invite and cleaning up the
  disposable state. No background browser process remained.

## Ranked broken findings

### 1. P0 — a non-owner has no leave action

After joining and exchanging messages, the guest room's accessible snapshot had
an empty button list. Its details panel said only that the room owner can view
or copy the join link and should be asked to share it. There was no **Leave
room**, **Leave**, menu item, or other guest lifecycle control. The only visible
room-management action belongs to the owner: **Close room**, which disconnects
everyone, invalidates the invite, and destroys the shared room for all members.

This prevents the stated first-run journey from completing for a normal invited
developer. The run did not call an undocumented API or CLI route to hide the
gap; it records the UI failure exactly as encountered.

## Ranked confusing findings

### 1. P1 — the first screen can be visually ready before its React action exists

On the cold run, the server-rendered signup and room forms appeared before their
client islands had attached handlers. A pointer click during that window did
nothing and left the button enabled with no alert. Waiting for the hydrated
control made the same visible signup action work. The UI provides no loading or
“ready” signal, so a fast human click can look lost. The successful timings
above include the observable hydrated state, not an arbitrary sleep.

### 2. P1 — landing promise versus measured two-identity value

The landing page says **“7s from nothing to agents talking”**. This cold,
automated two-account journey reached the guest's first owner message in
`13.621 s` and ended at `14.152 s` before cleanup, without human reading or
typing. The headline may describe a different one-agent setup, but a developer
following this complete first-run path has no explanation for the difference.

### 3. P2 — room setup defaults are safe but require interpretation

The room form defaults to **15 places** and **7 days (recommended)**. The copy
does explain that the owner consumes one seat and that shorter windows suit
temporary work, and the four lifetime choices are legible. Still, a first-time
developer testing with one colleague must discover that the range slider—not a
numeric field—controls capacity and that the disposable-room choice is the
shortest lifetime. The run selected 2 places and 1 hour to avoid leaving a
long-lived room.

### 4. P2 — guest identity and room loading briefly look generic

Immediately after invite acceptance, the guest saw a truncated room identifier
and **“Loading this room…”** before the room name, members, and log appeared.
The state resolved quickly in this run, but the transient identifier makes it
unclear whether the invite opened the intended named room.

## Unaided-developer answer

**No—not all the way.** A developer who found Weft on Hacker News can reach a
working shared room and exchange messages without help, but an invited
non-owner cannot leave from the visible UI; they would need the owner to close
the room or know an undiscoverable API/CLI path.

## Method, safety, and scope

- Production origin:
  `https://weft.switzerlandnorth.cloudapp.azure.com`.
- Browser: two separate cold headless Chrome profiles with empty storage; no
  cookies, local storage, seeded accounts, or prior room state.
- Owner and guest used fresh account identities. The owner created one cap-2
  room, shared its real invite, and used a one-hour lifetime.
- The report records UI labels and safe message markers only. Account emails,
  passwords, room id, invite token, session data, and live links were not
  printed or committed.
- The browser owner closed the room during cleanup. This is a cleanup result,
  not a claim that the guest leave flow passed.
- No tests were added. Test-count delta is `0`. No build, site generation, or
  final gate ran.
