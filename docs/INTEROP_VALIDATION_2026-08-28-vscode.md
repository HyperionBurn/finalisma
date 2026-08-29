# MPAI-118 VS Code production transcript — 2026-08-28

## Verdict

**NOT VERIFIED — the VS Code Copilot Agent Host did not complete a room
round-trip from the page-generated configuration.** VS Code `1.134.0` and its
bundled Copilot Agent Host `1.0.81-0` were available locally, so this was tested
against live production. The exact page JSON reached the host, but the host did
not register the Weft tools. An additive `type` diagnosis was also attempted;
it did not make the tools callable. No browser marker was received from any
failed host run.

This is a host/config-layer negative result, not evidence that the bridge or
room service is broken.

## Layered diagnosis

1. **Production UI and generated config:** a cold headless Chrome session signed
   up, created a disposable cap-2 room, minted a key, and captured the hydrated
   `/app/connect` JSON. The document was 402 bytes and contained `python`, six
   bridge arguments, the production origin, `WEFT_TOKEN` under `env`,
   `PYTHONUTF8=1`, and the explicit bridge-path placeholder. The exact strict
   run substituted only that local path placeholder.
2. **Bridge command:** the downloaded production bridge was HTTP `200`, 18,302
   bytes, SHA-256
   `468620983c9755c3f277288fad8ef77bc94c782178066ab733eb8ee8042cf9e8`. Running
   the generated command directly negotiated MCP `2025-11-25`; `tools/list`
   returned 14 tools, including all six room tools.
3. **Host config loading:** Copilot reported the configured server as loaded.
   The exact page JSON has no `type` field. In the strict run, Copilot's tool
   attempts were recorded as `weft` and `rg`; the Weft calls were not registered
   as callable MCP tools and no room call reached the bridge.
4. **Additive host diagnosis:** adding `type: "local"`—the only host-field
   change—still failed. With the original underscore prompt, Copilot reported
   `Tool 'room_join' does not exist`; with a prompt using the host's documented
   server-qualified hyphen form, it reported `Tool 'weft' does not exist` and
   `Tool 'weft_room_join' does not exist`.
5. **Host isolation:** a local one-tool MCP fixture received `initialize`,
   `notifications/initialized`, and `tools/list` while Copilot reported the
   server connected. Copilot nevertheless repeatedly attempted bare `echo` and
   returned `Tool 'echo' does not exist`; the fixture never received a
   `tools/call`. This reproduces the registration/name failure without Weft or
   production state.

The failure is therefore above the bridge: the page's generic JSON is not a
working VS Code Copilot tool configuration in this installed host, and the
tested additive type did not repair registration. The direct bridge and live
room layers passed independently.

## Environment and safety

- Host: VS Code `1.134.0`; bundled Copilot Agent Host `1.0.81-0`.
- Production origin:
  `https://weft.switzerlandnorth.cloudapp.azure.com`.
- Browser: cold headless Chrome CDP profile performed signup, room creation, key
  minting, owner messaging, and cleanup through the production UI.
- Room: disposable cap-2 room with a short TTL. The owner queued a marker before
  each host attempt.
- Host launch was bounded and isolated with the generated config supplied via
  Copilot's `--additional-mcp-config`; unrelated custom instructions and
  built-in/remote MCPs were disabled.
- The generated key, join token, and temporary config paths were not printed;
  all temporary files and child processes were torn down. Each failed room was
  closed by the browser owner or retained only for its selected short TTL.

## Redacted host evidence

Strict page-config attempt (path placeholder only):

```text
Copilot Agent Host → exit 1
loaded server: weft (host reported server presence)
recorded tool names: weft, rg
report: none
browser owner marker: not rendered
```

Additive `type: "local"` diagnosis, original tool-name prompt:

```text
Copilot Agent Host → exit 1
recorded tool names: weft, weft_room_join, room_join, multi_tool_use
host error: Tool 'room_join' does not exist.
browser owner marker: not rendered
```

Additive `type: "local"` diagnosis, server-qualified-name prompt:

```text
Copilot Agent Host → exit 1
recorded tool names: weft, weft_room_join
host errors: Tool 'weft' does not exist.; Tool 'weft_room_join' does not exist.
browser owner marker: not rendered
```

The local fixture's server log recorded `initialize`,
`notifications/initialized`, and `tools/list` requests, but no `tools/call`.
It is diagnostic evidence only and is not counted as production interoperability.

## Page judgment and recommendation

VS Code cannot be counted as a fourth verified production host from this run.
OpenCode's companion transcript records a full production round-trip through a
native wrapper, but the current page also has no OpenCode tab, so it is not a
strict page-onboarding proof either. The marquee's eight names therefore
currently oversell what this evidence supports if a buyer reads “Speaks MCP” as
“Weft has verified this host.”

Recommended wording: retain the names with the existing public-protocol
disclosure, but add a nearby qualification that Weft has verified only the
host/version/config combinations documented in linked production transcripts;
do not describe the unverified names as tested Weft integrations. Alternatively,
add host-specific connect tabs and rerun this production standard before making
that stronger claim. This document does not edit landing copy.

No tests were added. Test-count delta is `0`. No build or final gate ran.
