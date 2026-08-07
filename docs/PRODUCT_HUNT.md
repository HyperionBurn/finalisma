# Product Hunt launch kit

This is copy for a design-partner launch. Replace bracketed fields with real
facts after the first live cohort. Do not manufacture social proof.

## Current platform notes (checked 2026-07-28)

Product Hunt's official launch guide says makers can launch their own product;
there is no requirement to find a third-party hunter. It also says to ask
people to visit and comment rather than directly asking for upvotes. Company
accounts are not allowed, so publish from the maker account and tag the actual
makers. Recheck the live guide immediately before submission:
[Product Hunt Launch Guide](https://www.producthunt.com/launch).

## Listing

**Name:** Weft

**Tagline:** One incident. Two agents. One account of what happened.

**Description:** Weft is a single-node coordination preview for bounded,
evidence-backed handoffs between MCP-capable agent hosts. One agent opens an
incident account; another previews the policy, consents, claims the scoped
work, and returns artifact-linked evidence. Agents keep their own model
credentials and approval UX. Nine host paths are documented; live Weft
host-pair validation is still open.

**Topics:** Developer Tools · Artificial Intelligence · Open Source · MCP ·
Productivity

## Maker first comment

We built Weft because moving work between AI agents still means copying
prompts, credentials, file lists, and progress by hand.

The first workflow is narrow: non-production incident mirrors and PR review for
teams already using two agent hosts. One agent creates a short-lived pairing
link. The other previews and consents. The task carries scope, leases, fencing
tokens, replayable events, and evidence before completion.

Today this is a dependency-free single-node preview—not a hosted SaaS. We are
recruiting eight engineering teams to run three bounded handoffs and tell us
whether the second one is meaningfully easier than copy-paste.

## Demo order

1. Show the 42-second credential-redacted coordinator video from `site/demo.html`.
2. Link the launch site and quickstart.
3. Answer “does it run my models?” with: no, hosts own execution and keys.
4. Answer “is it hosted?” with: not yet; trusted-network single-node preview.
5. Invite teams to the design-partner experiment, not to an imaginary free
   marketplace.

## Reply bank

**Does this work outside one provider?**

Weft is provider-neutral at the coordination layer. Official docs show
nine plausible MCP host paths, but this repository contains zero completed
host-pair validations. Other products need an adapter. Model routes are
recorded explicitly and never silently substituted.

**Why not Slack?**

Slack carries conversation. Weft carries a governed work object: identity,
scope, atomic claim, fencing token, ordered replay, and evidence before close.

**Can I expose this publicly?**

The current coordinator is intentionally single-node. Use localhost or a
trusted network for the preview. Hosted public traffic still needs the gates in
`docs/SECURITY_GATES.md`.

**What are you measuring?**

Time from link creation to the first evidence-gated handoff, followed by
whether the same workspace completes a second handoff within four weeks.

## Launch-day checklist

- [ ] Test every link in the listing and video description.
- [ ] Run the full test suite from a clean terminal.
- [ ] Have five real users ready to try the quickstart.
- [ ] Reply to technical questions with boundaries, not invented scale.
- [ ] Collect failed joins, unsupported hosts, and repeat-use evidence.
- [ ] Update the page only after a measured result exists.
