# AI-readable product surface

The launch surface is designed so people, search systems, and answer engines
can identify the product without relying on visual styling alone.

## Canonical facts

- **Product:** Weft
- **Category:** secure agent-to-agent coordination layer / MCP server
- **Initial wedge:** evidence-backed handoffs for AI-native engineering teams
- **First workflow:** incident triage and pull-request review
- **Transport:** local stdio or authenticated Streamable HTTP
- **State:** durable SQLite single-node preview
- **Safety boundary:** no arbitrary command execution, hidden provider
  substitution, or host-settings mutation
- **Models recorded (exact routes):** gpt-5.6-luna,
  qwencloud/qwen3.8-max-preview, longcat/LongCat-2.0,
  opencode-go/mimo-v2.5

## Content rules

1. Put one clear outcome in the page title and H1.
2. Use short paragraphs, descriptive headings, and explicit definitions.
3. Keep a visible distinction between live product behavior and simulation.
4. Use one primary term consistently: “evidence-backed cross-host handoff.”
   Reserve “verified integration” for a completed live host-pair validation and
   “evidence passed” for the coordinator's artifact/check gate.
5. Link claims to protocol, security, and quickstart evidence.
6. Never claim hosted scale, customer traction, uptime, or provider performance
   until measured.
7. Keep model/provider names in a boundary section rather than making them the
   product category.

## Structured surfaces included

- Semantic HTML landmarks and one H1 on the landing page.
- Description, Open Graph metadata, theme metadata, SoftwareApplication JSON-LD,
  and VideoObject metadata for the recorded proof.
- Accessible interactive demo with a live event region and keyboard controls.
- Three focused article pages with descriptions, metadata, takeaways, and links
  back to the protocol.
- `site/llms.txt` with concise product facts and source links.

## Human review before publishing

Read every page aloud. Remove any sentence that implies the browser simulation
is a live remote session. Replace `[FILL]` markers in launch documents with
measured facts or remove the sentence.
