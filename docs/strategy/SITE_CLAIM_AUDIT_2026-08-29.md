# Live-site factual-claim audit — 2026-08-29 (MPAI-123)

Status: **Delta 0. Audit only; no site or source copy changed.**

This audit covers the checked-in publish artifact under `site/` at `hive/land`
(HEAD `9d3bf98`), including the landing page, `/app/connect`, `/docs/`, and the
pricing sections. The generated HTML is treated as the live copy under review.
Linked demo, legal, blog, and authenticated application routes are out of
scope unless a covered page makes a claim about them.

## Method and classification

Each material factual statement is recorded verbatim or as a contiguous quoted
group. Repeated landing claims are grouped by their locations. `BACKED` means
the cited evidence supports the exact scope, date, and qualification stated;
dated/local evidence remains explicitly dated or scope-limited. `UNBACKED`
means the statement is plausible but no matching evidence was found.
`CONTRADICTED` means executable code, a negative transcript, an internal page
inconsistency, or another committed record conflicts with the wording.

No `npm run build`, public-edge probe, or `scripts/final-verify.sh` was run.
The audit therefore does not assert that the checked-in artifact is the
currently deployed Vercel/Azure response. Source and local-suite evidence is
not silently upgraded into production availability proof.

## Ranked buyer-damage findings

1. **P0 — privacy promise is false as written.** Landing `/index.html` says
   “Private stays private” and “Only the agent it was addressed to can read
   it.” The room filter deliberately returns the full payload to the room
   owner before the recipient check (`src/weft_cloud/rooms.py:1033-1053`), and
   `/docs/security.html` documents owner administrative visibility. A buyer
   testing an unicast can observe that the owner can read it. This is a direct
   landing/security contradiction. Human decision and copy correction are
   tracked separately as MPAI-124; this audit does not edit it.
2. **P0 — published hosted tool count is stale and internally inconsistent.**
   The landing/docs pairing/protocol/quickstart pages say 14 hosted tools and
   list 14 names. Current source exports `room_restore_member` in a 15-tool
   `HOSTED_TOOLS` catalog (`src/weft_cloud/mcp.py:341-503`); the committed
   design and bridge docs also say 15. A client following the page can receive
   a different catalog from the one promised.
3. **P1 — security controls are assigned to the wrong deployment scope.**
   `/docs/security.html` places “workspace containment; artifact hashing;
   secret-pattern scanning” under “Enforced on the hosted service.” The
   protocol page correctly scopes those controls to the self-hosted
   coordinator, while hosted is room-only. This is a material security-scope
   contradiction even though the self-hosted controls exist.
4. **P1 — proof figures are presented as measured current facts without
   reproducible methods.** The landing says “Every number here came from a real
   run” and “The method sits under each figure,” yet no method/evidence link is
   present under the four figures. The exact 0/15, 1/ordering, and 7-second
   current journey claims have no matching measurement in the cited evidence;
   the 7-second headline conflicts with a later cold first-run audit measuring
   13.621 seconds to the first guest message and 14.152 seconds before cleanup.
5. **P1 — compatibility language exceeds host evidence.** The marquee names
   eight hosts under “Speaks MCP,” while the current records show Claude Code
   and Codex verified, OpenCode only partial/page-unverified, and VS Code
   negative; Cursor remains unverified. `/app/connect` additionally promises
   that “your agent” will join within “a second or two,” with no such
   cross-host timing evidence.
6. **P1 — commercial promise conflicts with product status.** Landing Team
   pricing presents `$39 per seat, per month` plus “Support from the people who
   built it.” The pilot page and roadmap state that Pro pricing is a
   hypothesis, billing/checkout is not implemented, and no support-response
   commitment is published. Code limits do not establish a paid offer.
7. **P2 — dated historical ledgers are surfaced as if current in one docs
   card.** `/docs/index.html` says “one historical host run, zero current
   host-pair validations,” but newer committed transcripts record Claude Code
   and Codex production round trips and a partial OpenCode run. The dated
   `/docs/compatibility.html` snapshot is honest as of 5 August, but its
   summary card is easy to read as current.

## Claim-by-claim inventory

### Landing — `site/index.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “Share one link. Every agent joins the same room and sees the same messages, in the same order. Nothing gets lost.” (hero, meta, OG, closing CTA) | **BACKED, scope-limited** | Ordered replay/no-loss is covered by `docs/ROOMS_DESIGN.md`, room tests, and `src/weft_cloud/rooms.py:2066-2160`; the exact live-production guarantee is not established. Treat “nothing gets lost” as local/protocol evidence, not SLA. |
| “Speaks MCP” and the names “Claude Code, Codex, Cursor, Windsurf, OpenCode, Zed, Cline, and VS Code.” | **BACKED only as protocol/documentation names; UNBACKED as compatibility** | `docs/INTEROP_VALIDATION_2026-08-28-claude-code.md` and `...codex-verified.md` verify named versions; `...opencode.md` is partial, `...vscode.md` is negative, and no current Cursor/Windsurf/Zed/Cline proof exists. The page’s disclosure says names are not partners/endorsements, which is backed. |
| “MCP is the tool protocol these hosts already use.” | **BACKED, qualified** | MCP host documentation and `docs/compatibility.html` support documented transport paths; transport support is not a Weft integration guarantee, as the compatibility page itself says. |
| “Agents cannot see each other. There is no shared record, no order, and no way to prove what any of them was told. When one drops out, whatever it missed is simply gone.” | **UNBACKED** | Broad comparative/product-market assertions have no measured competitor or baseline evidence in the repository. Weft’s own room mechanism does not prove the universal counterclaim. |
| “One shared record, in one order.” / “Every agent reads the same list, numbered the same way, at its own pace.” | **BACKED, local/protocol scope** | `docs/ROOMS_DESIGN.md`, `docs/INTEROP_VALIDATION_2026-08-15.md`, and room cursor/replay implementation support ordered per-member replay; no current hosted-scale guarantee. |
| “Simulated account · no credentials · no live session” and “The room below is a static preview” | **BACKED** | The rendered page labels the room as simulated/static; `docs/DEMO_VIDEO.md` and the docs index distinguish simulation from coordinator evidence. |
| Simulated transcript: “opened a room · up to 15 agents · link expires in 24h” | **CONTRADICTED if read as the default product behavior** | The scene is labeled simulated, but current source defaults room TTL to 7 days (`src/weft_cloud/rooms.py:212`, `1167-1169`), not 24h. A caller can choose a shorter TTL; the unqualified simulated default is misleading. |
| Simulated transcript: “41,208 requests checked · 3,914 too slow · 3,802 from that one service.” | **UNBACKED as product evidence** | No matching committed performance or incident measurement was found. The surrounding room is a labeled static preview, so these are illustrative figures, not proof. |
| Simulated transcript: “If that service does not answer within a second, we use the last known price instead.” | **UNBACKED** | No matching current service, timeout, or fallback measurement/evidence was found; the static preview does not establish a real integration. |
| Simulated transcript: “changed 1 file · all 1,313 tests still pass.” | **UNBACKED / contradicted by current count** | No 1,313-test run supports this artifact. `docs/RELEASE_EVIDENCE.md` records a dated 1,389 discovered / 1,373 passed / 16 skipped run, and the current tree has since changed. The static label is not a current test report. |
| Simulated transcript: “2 messages replayed · nothing missing” and “It returns and catches up. Nothing lost, nothing repeated.” | **BACKED, local/protocol scope** | Room replay and cursor tests/design cover no-loss/no-duplicate reconnect semantics (`docs/ROOMS_DESIGN.md`; `src/weft_cloud/rooms.py:2066-2160`). The scene itself remains simulated. |
| Simulated transcript: “not in this room · nothing recorded” / “Without the link there is no room — and no trace of the attempt.” | **BACKED for no access; UNBACKED for no trace** | Link-gated room access and member-only operations are covered by room tests and `docs/ROOMS_DESIGN.md`. “No trace” is broader than the tested room event log: access/web/proxy logs are deployment-dependent and were not proved absent. |
| “Measured, not claimed” / “Every number here came from a real run.” | **CONTRADICTED as presented** | The four figure-specific methods and evidence references are absent from the generated page. `docs/PERFORMANCE.md` contains a real dated hero-video measurement, not these room figures. |
| “0” — “messages lost across fifteen agents” / “15 agents · one link · one room” | **UNBACKED** | No matching 15-agent loss measurement was found in the cited evidence. Local replay/no-loss tests do not establish this exact run or number. |
| “1” — “ordering, agreed by every agent” / “no disagreement, not one” | **UNBACKED** | Ordered sequence behavior is locally implemented/tested, but no exact 15-agent measurement with unanimous agreement was found. |
| “199” — “agents on a single link” / “joined, then all read the same list” | **BACKED, historical/local scope** | `docs/DOGFOOD_FINDINGS_2026-08-07.md:171` records 199 agents joining one link in 4.9s with a single identical event ordering; `docs/PRODUCT_ROADMAP.md:103` repeats the dated dogfood record. It is not current hosted capacity proof. |
| “7s” — “from nothing to agents talking” / “download, sign up, first message” | **CONTRADICTED for the unqualified complete first-run journey** | `docs/INTEROP_VALIDATION_2026-08-29-mpai120.md:94-102` records 13.621s to the first guest message and 14.152s through cleanup for a cold two-account browser journey. A different abbreviated setup could differ, but the page gives no qualification. |
| “The method sits under each figure so you can repeat it.” | **CONTRADICTED** | The rendered figure blocks contain captions only; no method, command, date, or evidence link appears beneath them. |
| “Today Weft runs as a single-node service... no multi-region failover yet.” | **BACKED** | `docs/PERFORMANCE.md`, `docs/CLOUD_SPINE_DESIGN.md`, and `docs/LIVE_DEPLOYMENT.md` describe the single-node/SQLite-WAL deployment and lack of multi-region failover. This does not validate the figures above. |
| “One file. No install.” / “Download a single file... nothing to package, nothing to run.” | **BACKED, bridge scope** | `docs/STDIO_BRIDGE.md:69-72,111-112` and `src/weft_cloud/web/copy.py` document the stdlib-only downloadable bridge; Python itself remains a runtime prerequisite. |
| Terminal “18.3 kB · no dependencies” and the one-file `curl` command | **BACKED** | `docs/STDIO_BRIDGE.md` documents the dependency-free file; the Claude Code transcript records the downloaded bridge HTTP 200 and 18,302-byte artifact. |
| “connected · joined incident-4471 · 6 agents in the room” | **UNBACKED as a live result** | This is part of the static simulated terminal; no matching current room transcript or public probe is tied to that room. |
| “Everyone sees the same order” / “Not eventually. The same numbers, for every agent, at the same time.” | **BACKED for order; UNBACKED for same-time delivery** | Ordered event IDs/replay are covered by room tests/design. No simultaneous-delivery measurement supports “at the same time.” |
| “Private stays private” / “Only the agent it was addressed to can read it.” | **CONTRADICTED** | `src/weft_cloud/rooms.py:1033-1053` returns the full payload to the room owner before the targets check; `site/docs/security.html` explicitly documents owner administrative visibility. Non-owner non-addressees receive redaction. |
| “Dropping out costs nothing” / “An agent that disconnects picks up exactly what it missed.” | **BACKED, local/protocol scope** | Room cursor/replay tests and `docs/ROOMS_DESIGN.md` support reconnect without loss/duplicates; no hosted availability/SLA claim is established. |
| “Strangers get nothing” / “Without the link there is no room” | **BACKED, access scope** | Link resolution, tenant/member checks, and room tests reject callers without a valid link/member capability. |
| “...and no trace of the attempt.” | **UNBACKED** | See the no-trace limitation above: room event absence is not proof that every web/access/proxy log is absent. |
| “Free while you prove it works.” / “Enough to run a real team of agents and see the whole thing working.” | **UNBACKED** | Free limits exist in source, but current hosted availability is repeatedly marked as requiring a fresh probe; “real team” is a qualitative sufficiency claim without capacity/customer evidence. |
| Free “15 agents in a room” and “5 rooms at once” | **BACKED, source/local scope** | `src/weft_cloud/quotas.py:39-52` defines free limits and `docs/quickstart.html` documents 409 enforcement; current public-host behavior still needs a fresh probe. |
| Free “The full ordered record” | **BACKED, source/local scope** | Room event log, ordered cursors, and replay are implemented/tested (`src/weft_cloud/rooms.py:2066-2160,2642-2670`; `docs/ROOMS_DESIGN.md`). |
| Free “Private messages and replay” | **CONTRADICTED as an absolute privacy promise; BACKED for replay** | Replay is supported, but owner visibility means private unicast is not readable only by the addressed agent. |
| Team “$39 per seat, per month” / “For teams running agents against work that matters.” | **CONTRADICTED as an active offer** | `site/docs/pilot.html` says Pro pricing is proposed/hypothetical; `docs/PRODUCT_ROADMAP.md:169` leaves billing/checkout incomplete; `docs/LIVE_DEPLOYMENT.md` says no billing. The second sentence is marketing positioning, not an independently evidenced fact. |
| Team “50 agents in a room” / “50 rooms at once” | **BACKED only as unbilled Pro code limits** | `src/weft_cloud/quotas.py` contains Pro limits, but those values do not create a commercial plan or public capacity guarantee. |
| Team “Longer history” | **UNBACKED** | No implemented/history-retention contract or measured Pro behavior was found. |
| Team “Support from the people who built it” | **CONTRADICTED as a commitment** | `site/docs/pilot.html` explicitly says no support-response commitment is published; no support SLA or operational contract was found. |
| Closing “One link. One record. Nothing lost.” | **BACKED, local/protocol scope; UNBACKED as a current production guarantee** | Same evidence as hero/no-loss row; no live availability or loss-rate proof. |

### `/app/connect` — `site/app/connect/index.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “Two steps” | **CONTRADICTED** | The same page visibly contains “Step one,” “Step two,” and “Step three.” `web/src/pages/app/connect.astro` repeats the inconsistent heading. |
| “Download one file, then paste a config into your agent. There is nothing to install and no package to add — bridge is a single standalone script.” | **BACKED, bridge scope** | `docs/STDIO_BRIDGE.md` and `src/weft_cloud/web/copy.py` document the stdlib-only bridge; a Python runtime and host configuration are still required. |
| “one file · no dependencies · nothing to install” and the `curl` download | **BACKED** | `docs/STDIO_BRIDGE.md:69-72` and the bridge artifact/tests support this exact bridge property. |
| “Existing agent keys cannot be retrieved: Weft shows each raw key only once, at creation.” | **BACKED** | `src/weft_cloud/service.py` key issuance/listing paths, `docs/AGENT_KEYS.md`, and onboarding tests enforce one-time raw-key reveal. |
| “No configuration is shown until a fresh key is revealed... its raw value is never retrievable.” | **BACKED** | Connect/key UI source and key-management tests keep raw values session-only and unrecoverable after reveal. |
| “Replace path... restart your agent... The key is a credential — treat the file like a password.” | **BACKED as procedural/security guidance** | The generated config contains the credential environment field; `docs/STDIO_BRIDGE.md` and `docs/AGENT_KEYS.md` recommend secret storage/revocation. |
| “Your agent will have the room tools available.” | **UNBACKED/CONTRADICTED as universal host promise** | Claude Code and Codex generated-config paths are verified in dated transcripts; OpenCode page onboarding is partial, VS Code failed to expose callable tools, and several marquee hosts remain unverified. |
| “...it appears in that room's member list within a second or two.” | **UNBACKED** | No matching cross-host timing measurement was found; VS Code’s negative path contradicts a universal “your agent” success implication. |
| “If the tools do not show up at all, the agent has not reloaded its config yet.” | **UNBACKED/overbroad** | Reload can be one cause, but the VS Code transcript shows a registration/name failure after config negotiation; the page does not distinguish host incompatibility or execution-mode restrictions. |

### `/docs/index.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “The website demo is simulated.” | **BACKED** | `docs/DEMO_VIDEO.md` and `demo-transcript.json` identify the browser/demo surface as simulated or credential-redacted proof, not a live room. |
| “These guides separate the documented hosted profile from the self-hosted coordinator and show the proof boundary for each.” | **BACKED** | The quickstart, protocol, security, and compatibility pages make that hosted/self-hosted distinction repeatedly. |
| “43-second recorded proof” / “A credential-redacted real coordinator run with pairing, replay, ownership, and evidence.” | **BACKED, historical/self-hosted scope** | `docs/DEMO_VIDEO.md` records the 43.04-second video and real coordinator path; it is not a live hosted integration or customer-data proof. |
| “Probe the exact hosted release before credentials, or run the self-hosted coordinator and replay one ordered room log now.” | **BACKED as guidance** | `site/docs/quickstart.html` and `docs/LIVE_DEPLOYMENT.md` explicitly require a fresh edge probe and provide the local proof path. |
| “Identity, consent, leases, replay, cancellation, evidence, and transport boundaries.” | **BACKED, split by deployment scope** | `site/docs/protocol.html` and `docs/PROTOCOL.md` describe these semantics; leases/evidence are self-hosted, while hosted is room-only. |
| “What the hosted service enforces today — and what is still not claimed.” | **BACKED as page intent; scope error in the security list is separately contradicted** | Security page has explicit not-claimed caveats, but one hosted enforcement bullet incorrectly bundles self-hosted controls (P1 finding). |
| “Nine documented MCP paths, one historical host run, zero current host-pair validations.” | **CONTRADICTED as a current summary** | Newer committed transcripts verify Claude Code and Codex and partially verify OpenCode (`docs/INTEROP_VALIDATION_2026-08-28-*.md`). It can only be read as a pre-2026-08-28 historical snapshot, but the card does not date itself. |
| “Interop Profile 0.1 preview · MIT licensed.” | **BACKED** | Protocol preview is identified in `site/docs/protocol.html`; repository `LICENSE` is MIT. |

### `/docs/compatibility.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “Evidence snapshot · 5 August 2026.” | **BACKED** | It is explicit page metadata and matches the dated historical ledger. |
| “Nine plausible MCP connection paths are documented. One host is verified, four connection tiers are validated with committed transcripts, and the remaining eight hosts stay documented-unverified.” | **BACKED as a dated 2026-08-05 snapshot; stale if read as current** | `docs/INTEROP_VALIDATION_2026-08-05.md`, `INTEROP_VALIDATION_2026-08-15.md`, and the page’s own dated ledger support the snapshot. Later host transcripts change current status. |
| “A host becomes verified only when the repository contains its version, redacted configuration, pairing and consent transcript, ordered reconnect, cancellation, evidence gate, and successful close.” | **BACKED as the project’s verification rule** | The compatibility page and `docs/INTEROP_BRIDGE_2026-08-05.md` define this rule; it is a policy criterion, not a claim that every listed host satisfies it. |
| “OpenCode 1.18.13 — verified 5 August 2026” and the listed full lifecycle | **BACKED, historical** | `docs/INTEROP_VALIDATION_2026-08-05.md` records the exact host/version and lifecycle. |
| “Four ways an agent reaches the coordinator... three more now have committed transcripts; the stdio tier also has a real-host run.” | **BACKED, historical/transport scope** | `docs/INTEROP_VALIDATION_2026-08-05.md`, `INTEROP_HTTP_2026-08-05.md`, `INTEROP_BRIDGE_2026-08-05.md`, and `INTEROP_SDK_2026-08-05.md` provide the dated transcripts. |
| The MCP stdio, Streamable HTTP, bridge-adapter, and SDK tier descriptions | **BACKED, historical/local scope** | Each bullet names its committed transcript and script; the page correctly says tier validation is not a third-party host-product claim. |
| “Tier validation proves the wire path works; it does not claim a specific third-party host product... integrates.” | **BACKED** | This qualification is consistent with the cited tier records and negative host transcripts. |
| The nine “Documented MCP paths” vendor entries | **BACKED as documentation-path inventory; not host compatibility** | The page labels these paths documented and links host transport documentation; no entry should be read as a verified Weft integration. |
| “Claude Code + Cursor is the proposed next validation pair... That is a test plan, not a completed integration claim.” | **BACKED only as a dated 5-August statement; stale** | Claude Code now has a verified 2026-08-28 transcript, while Cursor remains unverified. The exact historical wording is preserved but needs a date/context to avoid current misreading. |
| “Every status change must cite a fresh repository artifact...” and the Verified/Tier-validated/Documented-unverified/Adapter-required/Unsupported definitions | **BACKED as governance rule** | The page defines its own upgrade rule and it matches the project’s interop ledger discipline. |
| “1 historical verified host run... 3 additional tiers validated.” | **BACKED, historical** | Matches the 5-August evidence snapshot; not the current host-status total. |

### `/docs/pairing-ux.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “create_pairing still exists and works for a one-to-one handoff between exactly two agents.” | **BACKED, self-hosted scope** | `docs/PAIRING_UX.md` and pairing tests cover the two-agent self-hosted flow. |
| “It is part of the self-hosted coordinator surface (59 tools) — the hosted POST /mcp endpoint exposes only the 14 room tools.” | **CONTRADICTED on hosted count** | Self-hosted historical 59-tool evidence is backed by `docs/INTEROP_VALIDATION_2026-08-15.md`; current hosted source has 15 tools including `room_restore_member` (`src/weft_cloud/mcp.py:341-503`). |
| “For the room flow (one link admitting many agents), start with the quickstart.” | **BACKED** | Hosted room design/source and `site/docs/quickstart.html` document multi-use room links. |
| “Agent A creates a link, Agent B sees exactly what will be shared, explicitly accepts, and both agents receive one governed session. A pasted link is not consent.” | **BACKED, self-hosted pairing scope** | `docs/PAIRING_UX.md`, pairing tests, and protocol source cover preview, literal consent, and governed session creation. |
| “The two-minute pairing promise...” | **UNBACKED as an end-to-end user-time guarantee** | The page calls it a promise, but the 0.166s measurement excludes human reading/pasting/host approval; no full human journey under two minutes is measured. |
| “A timed end-to-end run... recorded 0.166 s... startup 0.134 s, 81%... No step failed.” | **BACKED, dated local measurement** | `docs/PAIRING_UX.md:22-52` records the clean-temp-workspace run and timing decomposition. It is not a hosted or human-time guarantee. |
| Pairing URL is a bearer capability; share only with Agent B; token in `#token=`; “reverse proxies never log it”; token-bearing paths rejected. | **BACKED for protocol controls; UNBACKED as an absolute logging guarantee** | `docs/PAIRING_UX.md`, source, and tests support bearer/fragment/path rejection. Whether a reverse proxy or other layer logs a URL is deployment-dependent, so “never” is too absolute. |
| Preview shows inviter, expiry, policy, capabilities; explicit confirmation; only JSON `true`; invalid values do not consume the link. | **BACKED, self-hosted pairing scope** | Pairing source/tests and `docs/PAIRING_UX.md` cover the fields and literal-boolean guard. |
| “Weft stores only its SHA-256 hash and will not return the raw token again.” | **BACKED** | Pairing/key storage source and tests hash actor credentials and make raw reveal one-time. |
| Separate actor/session tokens, reconnect from cursor, rotation invalidates the old token atomically | **BACKED, self-hosted scope** | `docs/PAIRING_UX.md` and credential rotation tests/source cover these invariants. |
| “It cannot install MCP into a product that does not support it or an HTTP connector.” | **BACKED as a limitation** | This is a protocol boundary explicitly documented in `docs/PAIRING_UX.md`; the VS Code negative transcript demonstrates the practical relevance. |
| “It cannot grant filesystem, shell, browser, or provider credentials... bypass approval... expose previous conversation history or another team's membership.” | **BACKED as intended protocol boundaries** | Pairing capability checks/docs support this, with the separate room-owner administrative visibility exception documented on the security page. |
| “Measured 2026-08-05 · 0.17 s to first verified handoff · no provider credentials cross the coordinator.” | **BACKED, dated/local scope** | `docs/PAIRING_UX.md` supports the timing and credential boundary; no hosted production SLA follows. |

### `/docs/pilot.html` and landing pricing

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “The pilot is retired. The free tier replaced it.” / “There is no application, no deposit, and no 30-day window.” | **BACKED as the recorded pricing decision** | The page is dated “Pricing decision 2026-08-07”; related product status docs describe free-tier/self-hosted proof and no pilot workflow. |
| “A permanent free tier beats a time-boxed pilot on every axis...” | **UNBACKED** | Comparative value judgment has no measured customer/adoption evidence. |
| “Five rooms and fifteen agents per room is enough to run something real.” | **UNBACKED** | Quota values are backed, but sufficiency for a real team is not measured. |
| “Free — 15 members per room, 60 messages / min per room, 20 signups / IP / 15 min. Starts through signup.” | **BACKED, source/local scope** | `src/weft_cloud/quotas.py:39-52` and quota/auth tests support the values; current public availability needs a fresh probe. |
| “Pro (proposed pricing) — $39 / seat / month, 50 members per room... price is a hypothesis, not an active billing plan.” | **BACKED as status** | The page itself, `docs/PRODUCT_ROADMAP.md:169`, and source quota values distinguish proposed limits from billing. This conflicts with the landing’s active-looking Team card. |
| “Enterprise — custom message rate and room scale. Arranged by conversation.” | **BACKED as proposal/status** | The page marks it a conversation-based proposal; no active enterprise contract is evidenced. |
| “There is no billing system yet... Pro and Enterprise begin with a conversation, not a checkout.” | **BACKED** | Product roadmap keeps billing/checkout open; `docs/LIVE_DEPLOYMENT.md` records no billing. |
| “No SLA, uptime, or support-response commitment is published.” | **BACKED** | `docs/LIVE_DEPLOYMENT.md`, `site/docs/security.html`, and the pilot page explicitly disclaim these commitments. |
| “Our own local test (not customer traffic) joined 40 independent agents to one room in 4.5 seconds with a single identical event ordering.” | **UNBACKED** | Repository search found no matching committed 40-agent / 4.5-second measurement. Do not substitute the separate dated 199-agent/4.9-second dogfood record. |
| “The free tier is not a trial, and it does not expire.” | **BACKED as policy/status** | Pilot page and free-plan documentation state no trial/expiry; hosted availability is separately caveated. |
| “The self-hosted proof path is reproducible now.” | **BACKED, local scope** | `docs/STDIO_BRIDGE.md`, `docs/INTEROP_VALIDATION_2026-08-15.md`, and `scripts/weft-smoke.py` provide a reproducible local path. |
| “Public multi-tenant SaaS, hosted SLA, SSO, enterprise compliance, universal host compatibility, or production-data readiness.” under “Not claimed” | **BACKED as an explicit non-claim** | The security/quickstart/pilot pages and source status consistently disclaim these properties. |

### `/docs/protocol.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “This is a product protocol preview, not an industry standard. MCP provides the tool transport; Weft adds a bounded coordination contract.” | **BACKED** | Protocol docs/source identify MCP as transport and explicitly disclaim ratified/industry-standard status. |
| “The hosted profile is room-only; task and evidence semantics belong to the larger self-hosted coordinator.” | **BACKED** | `src/weft_cloud/mcp.py`, `docs/HOSTED_MCP_DESIGN.md`, and self-hosted tool catalog scope enforce this boundary. |
| Hosted account identity via `fss_`/`agk_`, tenant confinement, session-only key management, and self-hosted declared team/model route without provider credentials | **BACKED, source scope** | `src/weft_cloud/service.py`, `src/weft_cloud/mcp.py`, `docs/IDENTITY_DESIGN.md`, and `docs/HOSTED_MCP_DESIGN.md` support the identity/tenant/key claims. |
| Hosted room tool set shown as “room_create ... room_event_log” (14 names) | **CONTRADICTED as a complete current catalog** | The page omits `room_restore_member`; current `HOSTED_TOOLS` exports 15 names (`src/weft_cloud/mcp.py:341-503`). |
| Hosted links multi-use; self-hosted pairing one-use; literal JSON `true`; owner administrative visibility and per-recipient redaction | **BACKED** | `src/weft_cloud/rooms.py:1033-1053`, room/link source, pairing tests, and `site/docs/security.html` support the stated split. |
| “The 59-tool self-hosted coordinator moves tasks through pending, assigned, in-progress, review, evidence-gated, and done states...” | **BACKED, self-hosted scope** | `docs/INTEROP_VALIDATION_2026-08-15.md`, `src/weft_mcp`, task/evidence tests, and the self-hosted catalog support this. |
| “The hosted 14-tool room profile does not expose this task surface.” | **CONTRADICTED on count; BACKED on task-surface boundary** | Hosted is room-only, but the current profile is 15 tools. |
| Monotonic sequence numbers, idempotency keys, at-least-once delivery, cursor replay/deduplication | **BACKED, room protocol scope** | `src/weft_cloud/rooms.py`, `docs/ROOMS_DESIGN.md`, and room tests. |
| Explicit/auditable cancellation, expired-link re-consent, disconnect reusing active session/cursor | **BACKED, source/local scope** | Protocol/source tests and `docs/ROOMS_DESIGN.md` support these invariants. |
| “On the self-hosted coordinator, Weft hashes declared artifacts, rejects workspace escapes, scans for high-confidence secret signatures, and requires submitted checks to pass.” | **BACKED, self-hosted scope** | Self-hosted artifact/evidence implementation and `docs/INTEROP_VALIDATION_2026-08-15.md` support it; the page correctly says hosted room replay does not imply it. |
| Hosted POST/mcp, local stdio bridge, self-hosted SQLite coordinator transport options | **BACKED** | `docs/STDIO_BRIDGE.md`, `docs/HOSTED_MCP_DESIGN.md`, and source transport entry points. |
| “No universal installer, no host credential custody, no industry-standard status, and no A2A Protocol conformance.” | **BACKED as explicit non-claims** | Protocol/security docs and source boundary statements make these limits explicit. |

### `/docs/quickstart.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “This guide separates a documented hosted configuration from a locally reproducible proof.” / hosted origin is historical/unverified; fresh probe required | **BACKED** | The page is explicit about the deployment/evidence boundary; `docs/LIVE_DEPLOYMENT.md` and `docs/RELEASE_EVIDENCE.md` agree. |
| “The self-hosted proof path” and `weft-smoke.py` prints `evidence_passed: true` | **BACKED, local scope** | Script/docs provide the local smoke path; this is not hosted deployment proof. |
| Azure origin is a documented configuration example, not a current availability guarantee | **BACKED** | Page and `docs/LIVE_DEPLOYMENT.md` explicitly require fresh `/healthz` and agent-card probes. |
| “A room is one multi-use link that admits up to cap agents. Every member replays the same ordered event log from its own cursor... every member is its own identity...” | **BACKED, source/protocol scope** | Room source/tests/design support multi-use link, ordered replay, cursor, and distinct agent-key identity; current public behavior remains probe-dependent. |
| Signup/signin returns session tokens; signup creates tenant automatically and does not accept `org_name` | **BACKED, source/test scope** | `src/weft_cloud/service.py` auth routes and onboarding tests support the documented request/response contracts; no live-edge probe was run. |
| `finalisma.vercel.app` serves docs/bridge, “is not the MCP service”; bridge rejects cleartext non-loopback and IP TLS | **BACKED, deployment/source scope** | Static route/deployment docs and bridge source validate this boundary; public edge state itself was not probed in this audit. |
| `/.well-known/agent-card.json` must return 200 before hosted use; no credentials needed | **BACKED as deployment gate** | Agent-card route/source and quickstart/deploy docs define this preflight. |
| Session-only agent-key management; raw key returned once; no expiry clock but revocable; reset/membership invalidation; role re-derived | **BACKED** | `src/weft_cloud/service.py`, `docs/AGENT_KEYS.md`, security tests, and identity source. |
| Owner auto-joins on room create; creating `agk_` key is a distinct identity and must redeem the link before poll/send | **BACKED** | `src/weft_cloud/rooms.py:1167-1263` and onboarding tests implement the account-owner/key distinction. |
| “MCP hosts such as Claude Desktop, Cursor, Claude Code, Codex and OpenCode launch servers as command + args... cannot dial POST /mcp directly.” | **BACKED, host-transport scope** | `docs/STDIO_BRIDGE.md` documents stdio vs hosted HTTP; the exact list is not a compatibility guarantee. |
| Bridge forwards every message with bearer authorization; token in environment, never args; `PYTHONUTF8=1` safeguard | **BACKED** | `docs/STDIO_BRIDGE.md` and bridge source/tests cover transparent forwarding and credential placement. |
| “one file, nothing to install” / published dependency-free bridge | **BACKED, bridge scope** | `docs/STDIO_BRIDGE.md:69-72,111-112`; package/source-checkout caveat is separately documented. |
| Module form is source-checkout only; repository publishes no package index entry | **BACKED** | Quickstart text, `src/weft_cloud/web/app.py`, and package/install docs state this boundary. |
| OpenCode has two native config contracts; generated shapes covered by local suite, not fresh host validation | **BACKED as stated** | Current page/source has OpenCode v1/v2 shape handling; `docs/INTEROP_VALIDATION_2026-08-28-opencode.md` confirms production wrapper success but no page tab, so the qualification remains important. |
| “The tool set the bridge exposes is exactly the hosted set — tools/list returns 14 tools” and the 14-name list; “A client showing 14 is correct” | **CONTRADICTED** | `src/weft_cloud/mcp.py:341-503` and `docs/HOSTED_MCP_DESIGN.md:75` define 15, including `room_restore_member`; `docs/STDIO_BRIDGE.md:111-112` also says 15. |
| “Each additional agent redeems the identical link_token... one account can run its whole fleet. A second account joining via the same link works exactly the same way.” | **BACKED, room-source/local scope** | Room link resolution, account/key identity, and cross-tenant tests/docs support this; public availability remains caveated. |
| Consent must be JSON boolean `true`; other listed values rejected | **BACKED** | MCP/REST input validation and tests. |
| Machine-readable `/j/<token>` descriptor and agent-card pointer | **BACKED** | `src/weft_cloud/web/app.py` and route tests cover descriptor shape and pointer. |
| “Every member sees the same sequence numbers in the same order” and the dated three-account public-host probe | **BACKED, historical/conditional** | Quickstart cites the dated public probe and local identity script; it explicitly says the public result is not a current production claim while deployment drift is unresolved. |
| `room_wait` blocks/wakes, timeout empty result normal, default 20s/max 30s | **BACKED, source/test scope** | Hosted MCP source/tests and quickstart docs. |
| Dated hosted `room_wait` wake in 2.37s | **BACKED, historical** | Quickstart and dated hosted transcript record it; it is not a current latency/SLA claim. |
| Free cap above 15 returns 409 `quota_exceeded`; 5 rooms/15 members; nothing silently clamps | **BACKED, source/local scope** | `src/weft_cloud/quotas.py`, service routes, quota tests, and quickstart; fresh public probe still required. |
| “No SLA, uptime, SSO, SOC/ISO, or billing system exists yet...” | **BACKED** | Product/deployment status docs and source roadmap. |
| Self-hosted SQLite/stdio/HTTP coordinator on own disk, no account, separate/unreachable hosted rooms | **BACKED, self-hosted scope** | `docs/STDIO_BRIDGE.md`, `docs/DEPLOY.md`, source storage/transport paths. |
| Local coordinator “full 59-tool surface” and `actor_token` returned exactly once | **BACKED, historical/local scope** | `docs/INTEROP_VALIDATION_2026-08-15.md` records 59 tools; key source/tests enforce one-time raw token. |
| “...including a 199-agent join of one link with a single identical event ordering” | **BACKED, historical/local scope** | `docs/DOGFOOD_FINDINGS_2026-08-07.md:171` and `docs/PRODUCT_ROADMAP.md:103`; not current hosted capacity proof. |
| Sessions default 24h; agent keys no expiry clock but revocable | **BACKED** | `src/weft_cloud/service.py`, `docs/AGENT_KEYS.md`, and quickstart contract. |
| “Weft coordinates declared work; it does not need a model provider key.” | **BACKED as coordinator boundary** | Source does not handle provider credentials; protocol/security docs explicitly state that provider keys remain outside Weft. |

### `/docs/security.html`

| Claim (verbatim) | Status | Evidence / reason |
|---|---|---|
| “The hosted source implements a multi-tenant control plane with isolation at the storage boundary.” | **BACKED, source/local scope** | `src/weft_cloud/service.py`, `rooms.py`, tenancy guards, and security tests. The page itself requires a fresh deployment probe before treating it as live. |
| Bearer `fss_`/`agk_` separation, SHA-256 credential hashes, scrypt password hashes/salts, tenant-scoped writes | **BACKED** | `src/weft_cloud/service.py`, `src/weft_cloud/rooms.py:71`, account/storage tests, and security docs. |
| Auth on protected `/v1` and `POST /mcp`; generic errors; separate signup/signin; session-only key management | **BACKED, source/test scope** | Auth/MCP routes and security tests. |
| Structural tenant isolation and role re-derivation | **BACKED** | `TenantContext`, service authorization, and tenancy/security tests. |
| Atomic plan quotas/rate limits; cap above 15 or sixth room returns 409 | **BACKED, source/local scope** | `src/weft_cloud/quotas.py:39-52`, `rooms.py`, quota tests; page correctly says public behavior needs a fresh probe. |
| Explicit boolean consent, scoped capabilities, member-only operations | **BACKED** | MCP/room source and tests. |
| “Room owner administrative visibility... non-owner third-party agents receive per-recipient redaction...” | **BACKED** | `src/weft_cloud/rooms.py:1033-1053,2066-2160,2642-2670` and this page itself. This is the evidence that contradicts the landing privacy line. |
| “Idempotent, ordered events; workspace containment; artifact hashing; secret-pattern scanning.” under “Enforced on the hosted service” | **CONTRADICTED / scope-confusing** | Ordered/idempotent room events are hosted-room behavior, but workspace containment, artifact hashing, and secret scanning are self-hosted evidence controls (`site/docs/protocol.html` explicitly scopes them there). No hosted task/artifact/evidence surface exists in the current hosted catalog. |
| “No SLA, uptime, SSO, SOC/ISO certification, billing, or A2A Protocol conformance exists.” | **BACKED as non-claim/status** | Product/deployment status docs and protocol boundary. |
| “There is no production operating history beyond our own test traffic.” | **BACKED as the recorded evidence boundary** | `docs/RELEASE_EVIDENCE.md` explicitly disclaims hosted-release/deployment/third-party proof; dated production transcripts are controlled validation runs, not customer operating history. |
| Monthly event cap “10,000 free / 100,000 pro” enforced by committed hosted send path, with current public behavior requiring fresh probe | **BACKED, source/local scope** | `src/weft_cloud/quotas.py:187-209`, room send path, and security page; no live quota probe was run. |
| Self-hosted shared-bearer/trusted-actor model, SQLite-WAL on own disk, not multi-tenant, trusted-network warning | **BACKED** | `docs/DEPLOY.md`, `docs/STDIO_BRIDGE.md`, storage source, and security docs. |
| “Bearer auth, structural tenancy, enforced quotas, and role re-derivation are covered by the local hosted-service suite. Verify the exact deployment...” | **BACKED** | Matches the source/test evidence and the page’s deployment caveat. |

## Evidence register and handoff

- Hosted catalog: `src/weft_cloud/mcp.py:341-503`; `docs/HOSTED_MCP_DESIGN.md`; `docs/STDIO_BRIDGE.md:111-112`. Current count is 15, including `room_restore_member`.
- Room privacy/order: `src/weft_cloud/rooms.py:1033-1053,2066-2160,2642-2670`; `docs/ROOMS_DESIGN.md`; `site/docs/security.html`.
- Quotas and TTL: `src/weft_cloud/quotas.py:39-52,187-209`; `src/weft_cloud/rooms.py:212,1167-1169`.
- Bridge/pairing: `docs/STDIO_BRIDGE.md`; `docs/PAIRING_UX.md`; pairing/key tests and source.
- Host evidence: `docs/INTEROP_VALIDATION_2026-08-28-claude-code.md` (verified), `...codex-verified.md` (verified), `...opencode.md` (partial), `...vscode.md` (not verified), plus the dated 5-August/15-August ledgers.
- Performance/first-run evidence: `docs/PERFORMANCE.md` (hero-video measurement only), `docs/PAIRING_UX.md` (0.166s local pairing), `docs/INTEROP_VALIDATION_2026-08-29-mpai120.md` (13.621s/14.152s cold browser journey).
- Scale evidence: `docs/DOGFOOD_FINDINGS_2026-08-07.md:171` and `docs/PRODUCT_ROADMAP.md:103` support 199-agent historical/local claim; no matching 40-agent/4.5-second record was found.
- Release boundary: `docs/RELEASE_EVIDENCE.md` says its 1,389/1,373/16 count is dated local evidence, not hosted-release approval.

No copy was edited. No tests/build/gate were run. The only intended new file is
this audit report.
