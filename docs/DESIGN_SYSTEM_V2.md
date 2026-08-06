# DESIGN SYSTEM V2 — Wave D2: Finalisma Marketing Site

> **Status:** ART-DIRECTION lane deliverable. Authoritative spec for implementation.
> **Supersedes:** The "FIELD NOTES" editorial framing (Wave A/C). That framing is
> deliberately retired. This document defines the replacement — a product landing
> page, not a magazine feature.
> **Scope:** `site/index.html`, `site/styles.css`, `site/app.js` (+ new canvas engine
> module). Does NOT touch `src/finalisma_mcp/`.
> **Tone:** Engineering-premium restraint. Linear / Vercel / Resend / Stripe tier.
> Near-black graphite base. One confident accent. ASSERT/PROVE semantic colours carry
> product meaning. Motion is mandatory and disciplined.

---

## 0. Design Principles (non-negotiable)

1. **Product, not magazine.** No "Field Notes", no "In this dispatch", no "Reported
   from the incident queue", no folios, no numbered-article sidebar, no magazine
   columns. Product voice throughout.
2. **Show, don't tell.** The hero IS the live product — an animated, interactive
   agent-graph canvas. Every claim gets visual proof: real terminal output, a real
   generated+copied link, live event streams, the four connection tiers as switchable
   copy-paste config blocks, refusal cases visibly firing.
3. **Evidence-backed, always.** Every number measured. Use real strings from the
   INTEROP docs (paths §7). No fabricated customers, logos, testimonials, or metrics.
4. **Alive within 2s.** The hero canvas mounts and animates within 2 seconds of load.
   Playable: hover a node, click to send, watch it propagate.
5. **Restraint at scale.** One strong display face, tightened hard. Mono for anything
   technical. Depth via layering, subtle gradients, soft glow, glass/translucency where
   earned, real shadow. High density, product scale — not a 120px serif drop-head.
6. **Accessibility is a feature.** WCAG 2.2 AA. Full keyboard operability. Visible
   focus. `prefers-reduced-motion` honoured completely. Canvas has a meaningful text
   alternative. Core content works without JS.
7. **Performance budget.** Lighthouse ≥95 all categories at 1440×900 and 390×844.
   Animate transform/opacity only.

---

## 1. Palette

Near-black graphite base. NOT sepia, NOT brown, NOT oxblood. One confident accent
(electric violet) for energy/interaction. ASSERT/PROVE semantic colours carry product
meaning across both dark and light surfaces.

### 1.1 Core tokens

| Token | Hex | Role |
| --- | --- | --- |
| `--ink` | `#0E0F12` | Primary base (near-black graphite). Page background on dark sections. |
| `--ink-2` | `#16181D` | Elevated surface 1 (cards, panels on base). |
| `--ink-3` | `#1E2128` | Elevated surface 2 (nested, inputs). |
| `--ink-4` | `#2A2E38` | Hairline border on dark surfaces (subtle). |
| `--surface` | `#F7F8FA` | Light surface (body, problem/pricing bands). |
| `--surface-2` | `#FFFFFF` | Elevated light surface (cards on light). |
| `--text` | `#0E0F12` | Text on light surfaces. |
| `--text-inv` | `#F2F3F5` | Text on dark surfaces. |
| `--text-muted` | `#6B7280` | Muted text on light (4.55:1 on `--surface` ✓ — measured 2026-08-06). |
| `--text-muted-inv` | `#9BA1AD` | Muted text on dark (7.39:1 ✓). |
| `--hairline` | `rgba(14,15,18,0.12)` | Hairline border on light surfaces. |
| `--hairline-inv` | `rgba(242,243,245,0.14)` | Hairline border on dark surfaces. |
| `--accent` | `#5B3DF0` | ONE confident accent — electric violet, LIGHT-surface-safe (5.86:1 on `--surface`). CTAs, links, interactive energy. |
| `--accent-2` | `#5434E8` | Accent fill for buttons/large elements on light (white on it = 6.92:1). |
| `--accent-ink` | `#FFFFFF` | Text on accent-2 fill (6.92:1 ✓). |
| `--accent-dark` | `#9D82FF` | Accent on dark surfaces (6.45:1 ✓). |

### 1.2 Semantic role colours

| Token | Hex (dark bg) | Hex (light bg) | Meaning |
| --- | --- | --- | --- |
| `--assert` | `#F4756B` | `#BE2B1B` | ASSERT role — claimed, declared. Warm coral-red. |
| `--prove` | `#E0B34A` | `#7C5708` | PROVE role — evidenced, verified. Deep ochre (light-safe). |
| `--success` | `#4ADE80` | `#15803D` | Success / passed / verified. Green. |
| `--refuse` | `#F87171` | `#B91C1C` | Refusal / rejected / gated. Red. |
| `--success-ink` | `#0E0F12` | `#FFFFFF` | Text on success fill. |
| `--refuse-ink` | `#0E0F12` | `#FFFFFF` | Text on refuse fill. |

### 1.3 Contrast verification (every text-on-surface pair that renders)

> All ratios below were RE-MEASURED by the orchestrator on 2026-08-06 using the
> WCAG 2.2 relative-luminance formula. The art-direction lane's original light-surface
> values FAILED AA (accent 4.09:1, prove 3.07:1, assert 4.33:1) and were corrected
> here before implementation.

| Pair | Foreground | Background | Ratio | Pass |
| --- | --- | --- | --- | --- |
| Body text on light | `#0E0F12` | `#F7F8FA` | **18.04:1** | ✓ AA (≥4.5) |
| Body text on surface-2 | `#0E0F12` | `#FFFFFF` | **18.98:1** | ✓ AA |
| Muted text on light | `#6B7280` | `#F7F8FA` | **4.55:1** | ✓ AA (≥4.5) |
| Muted text on surface-2 | `#6B7280` | `#FFFFFF` | **4.70:1** | ✓ AA |
| Text on dark base | `#F2F3F5` | `#0E0F12` | **18.20:1** | ✓ AA |
| Muted on dark | `#9BA1AD` | `#0E0F12` | **7.39:1** | ✓ AA |
| Accent on dark | `#9D82FF` | `#0E0F12` | **6.45:1** | ✓ AA |
| Accent on light | `#5B3DF0` | `#F7F8FA` | **5.86:1** | ✓ AA (≥4.5) |
| Accent fill (btn) with white | `#FFFFFF` | `#5434E8` | **6.92:1** | ✓ AA |
| Assert on light | `#BE2B1B` | `#F7F8FA` | **5.57:1** | ✓ AA (≥4.5) |
| Prove on light | `#7C5708` | `#F7F8FA` | **6.13:1** | ✓ AA (≥4.5) |
| Assert on dark | `#F4756B` | `#0E0F12` | **6.94:1** | ✓ AA |
| Prove on dark | `#E0B34A` | `#0E0F12` | **9.78:1** | ✓ AA |
| Success on dark | `#4ADE80` | `#0E0F12` | **11.04:1** | ✓ AA |
| Refuse on dark | `#F87171` | `#0E0F12` | **7.64:1** | ✓ AA |
| Success on light | `#15803D` | `#F7F8FA` | **4.72:1** | ✓ AA |
| Refuse on light | `#B91C1C` | `#F7F8FA` | **6.09:1** | ✓ AA |
| Large display (≥24px/700) on dark | `#F2F3F5` | `#0E0F12` | **18.20:1** | ✓ AAA (≥3:1) |
| Section heading (≥18px bold) muted | `#6B7280` | `#FFFFFF` | **4.70:1** | ✓ AA |

> Every normal-text pair clears 4.5:1; every large-text pair clears 3:1. No exceptions.

---

## 2. Type Scale

Self-hosted fonts only (SIL OFL). Reuse the existing set. The display face is
**Archivo** (variable, 88KB) — tightened hard for product scale. Body is Archivo
at text weight for readability. Mono is IBM Plex Mono for terminal/code/config.
Big Shoulders Display is retired from body/label use; retained only as an optional
legacy fallback (not loaded by default). Fraunces is retired (serif = old magazine
voice); kept on disk but NOT `@font-face`'d in V2.

### 2.1 Font families

| Token | Family | Source | Licence |
| --- | --- | --- | --- |
| `--display` | `'Archivo', 'Arial Narrow', Arial, sans-serif` | `assets/fonts/archivo-var-latin.woff2` | SIL OFL (`OFL-archivo.txt`) |
| `--body` | `'Archivo', system-ui, sans-serif` | same file | SIL OFL |
| `--mono` | `'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` | `plex-mono-400/500-latin.woff2` | SIL OFL (`OFL-plex-mono.txt`) |

> **Rationale:** Archivo is a condensed grotesque with a variable weight axis
> (100–900) and variable width — ideal for a tightened product display face. It gives
> the impact the brief demands without the 120px-serif-magazine voice. IBM Plex Mono
> handles all technical content (terminal, code, config blocks, event log). No new
> fonts proposed; the existing SIL-OFL set covers every role.

### 2.2 Type roles

| Role | Family | Size | Weight | Leading | Tracking | Use |
| --- | --- | --- | --- | --- | --- | --- |
| `--t-display` | display | `clamp(40px, 6.5vw, 88px)` | 800 | 0.95 | -0.02em | Hero headline. Compact, high-impact. |
| `--t-section` | display | `clamp(28px, 3.6vw, 48px)` | 700 | 1.02 | -0.01em | Section headings. |
| `--t-sub` | body | `clamp(19px, 2vw, 24px)` | 400 | 1.35 | 0 | Sub-headlines, lede paragraphs. |
| `--t-body` | body | `17px` (16–18 fluid) | 400 | 1.6 | 0 | Body copy. |
| `--t-mono` | mono | `13px` | 400 | 1.5 | 0 | Terminal, code, config, event log. |
| `--t-label` | display | `11px` | 700 | 1.2 | 0.14em | Uppercase labels, kicker, nav, stat labels. |
| `--t-stat` | display | `clamp(34px, 4vw, 56px)` | 800 | 1.0 | -0.01em | Stat strip figures. |
| `--t-micro` | mono | `11px` | 500 | 1.4 | 0.02em | Timestamps, sequence numbers, cursors. |

> **Product scale rule:** The giant serif drop-head is gone. Display maxes at 88px
> (vs the old 116px Fraunces) and is set in a tightened condensed grotesque. Section
> headings are confident but compact. Body is a comfortable 17px for reading density.
> Mono is small and disciplined for technical content.

---

## 3. Spacing & Layout Tokens

| Token | Value | Use |
| --- | --- | --- |
| `--shell` | `1440px` | Max content width. |
| `--shell-narrow` | `1100px` | Reading-width sections (problem, pricing). |
| `--pad` | `clamp(20px, 4vw, 64px)` | Section horizontal padding. |
| `--pad-inline` | `clamp(16px, 3vw, 40px)` | Inner component padding. |
| `--gut` | `clamp(16px, 2vw, 32px)` | Grid column gap. |
| `--rhythm` | `clamp(56px, 8vw, 120px)` | Section vertical rhythm (padding-block). |
| `--rhythm-tight` | `clamp(32px, 4vw, 64px)` | Tight rhythm for stacked sub-sections. |
| `--header-h` | `60px` | Sticky header height. |
| `--radius` | `10px` | Card/panel border radius. |
| `--radius-sm` | `6px` | Button/input radius. |
| `--radius-lg` | `16px` | Hero canvas, large panels. |

### 3.1 Breakpoints

| Name | Width | Behaviour |
| --- | --- | --- |
| `--bp-wide` | `≥1440` | Full 12-column grid, side-by-side layouts. |
| `--bp-desktop` | `1024–1439` | Slightly reduced shell, grid adapts. |
| `--bp-tablet` | `768–1023` | 8-column grid, some stacks. |
| `--bp-mobile` | `390–767` | Single column, mobile nav, canvas scales. |
| `--bp-narrow` | `≤320` | Minimal padding, smallest fluid sizes. |

### 3.2 Density rules

- Dark base sections (`--ink`) alternate with light surface bands (`--surface`).
- No section exceeds one viewport height **unless** it is interactive (hero canvas,
  live demo, connect tiers). Interactive sections may be taller but must have clear
  internal scroll/step structure.
- Grid: 12-col on wide, 8-col on tablet, 1-col on mobile. Container queries for
  card-level responsiveness.

---

## 4. Motion Language

Motion is mandatory and disciplined. All animations use transform/opacity only.
Scroll-driven reveals, continuous agent-graph animation, micro-interactions
everywhere. FULLY honours `prefers-reduced-motion`.

### 4.1 Animation primitives

| Primitive | Easing | Duration | Use |
| --- | --- | --- | --- |
| `--ease-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | — | Standard exit/reveal. |
| `--ease-in-out` | `cubic-bezier(0.65, 0, 0.35, 1)` | — | Continuous/toggled motion. |
| `--ease-snap` | `cubic-bezier(0.34, 1.56, 0.64, 1)` | — | Micro-bounce on interaction. |
| `--dur-fast` | — | `150ms` | Hover, focus, toggle. |
| `--dur-mid` | — | `300ms` | Reveal, expand, tab switch. |
| `--dur-slow` | — | `600ms` | Section reveal, large transitions. |
| `--dur-crawl` | — | `900ms` | Gate-refusal flash sequence. |

### 4.2 Scroll-driven reveal

```css
/* Base: visible by default (no-JS / reduced-motion). */
.reveal { opacity: 1; transform: none; }

/* JS-enhanced: hide until observed. */
.js .reveal { opacity: 0; transform: translateY(18px); transition: opacity var(--dur-slow) var(--ease-out), transform var(--dur-slow) var(--ease-out); }
.js .reveal.in { opacity: 1; transform: none; }

/* Stagger children via transition-delay set from JS or :nth-child. */
```

- Reveal triggers via `IntersectionObserver` with `rootMargin: "0px 0px -80px 0px"`.
- Hard timeout (belt-and-braces): if observer hasn't fired in 1.5s, force `.in`.
- Stagger: 60ms between siblings.

### 4.3 Continuous animations (transform/opacity only)

| Animation | Element | Spec |
| --- | --- | --- |
| Agent-graph node pulse | `[data-agent-canvas] .node` | `scale(1→1.06→1)`, `opacity(0.85→1)`, `2.4s ease-in-out infinite`, per-node phase offset. |
| Message travel | `.msg-particle` | `translateX/Y` along edge path, `opacity 0→1→1→0`, `900ms ease-out`. |
| Presence ring | `.node::after` | `scale(1→1.8)`, `opacity(0.5→0)`, `2s ease-out infinite`. |
| Event log append | `[data-agent-events] .evt` | `translateY(8px→0)`, `opacity(0→1)`, `200ms ease-out`. |
| Gate-refusal flash | `[data-gate].is-refusing` | `translateX(0→-4→4→-2→0)`, `background-color` to refuse tint, `600ms`. |
| Stat count-up | `[data-count-to]` | Number interpolation over `800ms ease-out` (JS-driven). |
| Link copy ripple | `[data-copy].is-copied` | `scale(1→1.04→1)`, `accent glow`, `300ms`. |
| Scroll progress | `[data-scroll-progress]` | `width: 0→100%` driven by `scroll` event (transform of inner bar). |
| Ticker | `[data-ticker]` | `translateX` loop, `30s linear infinite`. |

### 4.4 `prefers-reduced-motion` rules (EXACT)

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
  .reveal, .reveal.in { opacity: 1 !important; transform: none !important; }
}
```

- **Canvas behaviour under reduced motion:** The hero canvas FREEZES to a legible
  static state — NOT blank. All nodes drawn at final positions, edges visible,
  last state of the event log rendered. A static "play" affordance is replaced by
  a caption: "Animation paused (reduced motion)." The text-alternative event list
  (§6.5) is always visible and carries the full content.
- **No parallax, no infinite loops, no auto-advancing** under reduced motion.

---

## 5. Component Inventory

Each component: purpose, structure, key classes/data-attributes, and the NEW
data-attribute hooks the QA harness will target.

### 5.1 Navigation

- **Purpose:** Sticky top nav. Brand, section links, CTA, mobile toggle.
- **Structure:**
  ```html
  <header class="site-header" data-header>
    <a class="brand" href="/">FINALISMA</a>
    <nav class="nav-links" aria-label="Primary">
      <a href="#problem">Problem</a>
      <a href="#how">How it works</a>
      <a href="#demo">Live demo</a>
      <a href="#connect">Connect</a>
      <a href="#pricing">Pricing</a>
    </nav>
    <a class="btn btn-accent" href="#pricing">Start free pilot</a>
    <button class="nav-toggle" aria-expanded="false" aria-controls="mobile-nav" aria-label="Open menu"><span></span></button>
  </header>
  ```
- **Hooks:** `[data-header]`, `.nav-toggle`, `#mobile-nav`, `.nav-links`.

### 5.2 Mobile nav

- **Purpose:** Full-screen mobile menu.
- **Structure:**
  ```html
  <div class="mobile-nav" id="mobile-nav" aria-hidden="true" data-mobile-nav>
    <button class="mobile-close" aria-label="Close menu">&times;</button>
    <a href="#problem">Problem</a> ...
    <a class="btn btn-accent" href="#pricing">Start free pilot</a>
  </div>
  ```
- **Hooks:** `#mobile-nav` (retained — harness targets `#mobile-nav`),
  `.mobile-close`, `[data-mobile-nav]`.

### 5.3 Skip link

- **Purpose:** Accessibility skip link.
- **Structure:** `<a class="skip-link" href="#main">Skip to content</a>`
- **Hook:** `.skip-link` (retained).

### 5.4 Hero (live product canvas)

- **Purpose:** Above-the-fold animated interactive agent graph. The hero IS the
  product.
- **Structure:**
  ```html
  <section class="hero" id="hero" aria-label="Finalisma — live product preview">
    <div class="hero-copy">
      <p class="kicker reveal">Agent coordination · evidence-gated</p>
      <h1 class="headline reveal" aria-label="One link. Many agents. All governed.">
        One link.<span class="accent">Many agents.</span><span class="accent-2">All governed.</span>
      </h1>
      <p class="lede reveal">Open one room, share it with every agent on the task,
        and they all communicate — with ordered delivery, scoped consent, and an
        evidence gate that refuses stale or out-of-scope work. Play it live below.</p>
      <div class="hero-ctas reveal">
        <a class="btn btn-accent" href="#connect">Connect an agent</a>
        <a class="btn btn-ghost" href="#pricing">Start free pilot</a>
      </div>
      <ul class="hero-stats" data-stat-strip>
        <li><b data-count-to="9">0</b><span>Documented MCP paths</span></li>
        <li><b>1</b><span>Verified host (OpenCode 1.18.13)</span></li>
        <li><b>0</b><span>Provider credentials stored</span></li>
      </ul>
    </div>
    <div class="hero-canvas" data-agent-canvas-wrap>
      <canvas data-agent-canvas aria-hidden="true"></canvas>
      <!-- Static text alternative: -->
      <div class="canvas-fallback" data-agent-fallback aria-hidden="false">
        <ol class="evt-fallback" data-agent-events-fallback>
          <!-- Populated by JS; static seed rendered server-side for no-JS -->
        </ol>
      </div>
      <div class="canvas-readout" role="status" aria-live="polite" data-agent-readout>Ready</div>
    </div>
  </section>
  ```
- **Hooks:** `[data-agent-canvas]`, `[data-agent-canvas-wrap]`,
  `[data-agent-events-fallback]`, `[data-agent-readout]`, `[data-stat-strip]`,
  `[data-count-to]`.

### 5.5 Problem beat

- **Purpose:** One tight beat on the problem (pasted prompts, no record, no
  governance).
- **Structure:** `.section.problem` with a 2-col layout: copy + a visual "before"
  (a styled pasted-prompt card showing the chaos).
- **Hooks:** `#problem`, `.problem-card`.

### 5.6 How it works (stepped, animated)

- **Purpose:** Visual stepped explanation. Animated as you scroll.
- **Structure:**
  ```html
  <section class="section how" id="how" aria-labelledby="how-title">
    <h2 id="how-title">How it works</h2>
    <ol class="steps" data-steps>
      <li class="step" data-step="1">
        <span class="step-num">01</span>
        <h3>Open a room</h3>
        <p>One multi-use link. N agents join up to the plan cap.</p>
      </li>
      ... (02 consent + scope, 03 addressing, 04 evidence gate)
    </ol>
  </section>
  ```
- **Hooks:** `#how`, `[data-steps]`, `[data-step]`, `.step-num`.

### 5.7 Live interactive demo

- **Purpose:** Show the product working — real terminal output, a real link
  generated+copied, live event streams, the refusal visibly firing.
- **Structure:**
  ```html
  <section class="section demo" id="demo" aria-labelledby="demo-title">
    <h2 id="demo-title">See it run</h2>
    <div class="demo-shell" data-demo-shell>
      <pre class="terminal" data-terminal role="log" aria-live="polite"></pre>
      <div class="demo-output">
        <div class="link-out" data-link-output>
          <code data-generated-link>finalisma.test/r/...</code>
          <button class="btn btn-sm" data-copy="link">Copy link</button>
        </div>
        <div class="gate" data-gate>
          <span class="gate-label">Evidence gate</span>
          <span class="gate-state" data-gate-state>armed</span>
        </div>
      </div>
    </div>
  </section>
  ```
- **Hooks:** `[data-demo-shell]`, `[data-terminal]`, `[data-link-output]`,
  `[data-generated-link]`, `[data-copy]`, `[data-gate]`, `[data-gate-state]`.

### 5.8 Four ways to connect (tabbed, real config)

- **Purpose:** The four connection tiers as switchable copy-paste config blocks.
- **Structure:**
  ```html
  <section class="section connect" id="connect" aria-labelledby="connect-title">
    <h2 id="connect-title">Four ways to connect</h2>
    <div class="tier-tabs" role="tablist" data-tier-tabs>
      <button class="tier-tab" role="tab" aria-selected="true" data-tier-tab="stdio">MCP stdio</button>
      <button class="tier-tab" role="tab" data-tier-tab="http">Streamable HTTP</button>
      <button class="tier-tab" role="tab" data-tier-tab="bridge">Bridge adapters</button>
      <button class="tier-tab" role="tab" data-tier-tab="sdk">SDK</button>
    </div>
    <div class="tier-panels">
      <div class="tier-panel" role="tabpanel" data-tier-panel="stdio">
        <pre class="code-block" data-code-block><code>...real config...</code></pre>
        <button class="btn btn-sm" data-copy="stdio">Copy config</button>
      </div>
      ... (one per tier)
    </div>
  </section>
  ```
- **Hooks:** `[data-tier-tabs]`, `[data-tier-tab]`, `[data-tier-panel]`,
  `[data-code-block]`, `[data-copy]`.

### 5.9 Proof / evidence

- **Purpose:** Evidence-backed claims. Real numbers, real strings, real refusals.
- **Structure:**
  ```html
  <section class="section proof" id="proof" aria-labelledby="proof-title">
    <h2 id="proof-title">Proof, not promises</h2>
    <ul class="proof-list" data-proof-list>
      <li class="proof-item" data-proof-item>
        <b class="proof-stat">9</b>
        <span class="proof-label">Documented MCP paths</span>
      </li>
      <li class="proof-item" data-proof-item>
        <b class="proof-stat">1</b>
        <span class="proof-label">Verified host integration (OpenCode 1.18.13)</span>
      </li>
      <li class="proof-item" data-proof-item>
        <b class="proof-stat">0</b>
        <span class="proof-label">Provider credentials stored</span>
      </li>
      <li class="proof-item" data-proof-item>
        <b class="proof-stat">0</b>
        <span class="proof-label">Runtime dependencies (stdlib + SQLite)</span>
      </li>
      <li class="proof-item" data-proof-item>
        <b class="proof-stat">1</b>
        <span class="proof-label">Coordinator node (single-node preview)</span>
      </li>
    </ul>
    <p class="proof-note">Next proof pair in the cohort: Claude Code + Cursor.</p>
  </section>
  ```
- **Hooks:** `[data-proof-list]`, `[data-proof-item]`, `.proof-stat`, `.proof-label`.

### 5.10 Pricing / CTA

- **Purpose:** Pricing hypothesis + CTA.
- **Structure:**
  ```html
  <section class="section pricing" id="pricing" aria-labelledby="pricing-title">
    <h2 id="pricing-title">Pricing</h2>
    <div class="pricing-card">
      <p class="pricing-hypothesis">Hypothesis, tested with design partners:</p>
      <p class="pricing-figure"><b>$1,000</b> / workspace / month</p>
      <p class="pricing-deposit"><b>$500 deposit</b> credited toward the first month.</p>
      <p class="pricing-pilot">30-day free pilot.</p>
      <a class="btn btn-accent btn-lg" href="#connect">Connect an agent</a>
      <a class="btn btn-ghost" href="/docs/pilot.html">Apply for the managed pilot</a>
    </div>
  </section>
  ```
- **Hooks:** `#pricing`, `.pricing-card`.

### 5.11 Footer

- **Purpose:** Minimal footer. Links, licence, version.
- **Structure:**
  ```html
  <footer class="site-footer" data-footer>
    <span>Finalisma v0.1.0 · single-node preview</span>
    <nav aria-label="Footer">
      <a href="/docs/index.html">Docs</a>
      <a href="/docs/protocol.html">Protocol</a>
      <a href="/docs/security.html">Security</a>
      <a href="/docs/compatibility.html">Compatibility</a>
      <a href="/license.html">Licence</a>
    </nav>
  </footer>
  ```
- **Hook:** `[data-footer]`.

### 5.12 Event log (hero companion)

- **Purpose:** Ordered event log filling in real time. Driven by the canvas engine.
  Always rendered as real DOM (text alternative).
- **Structure:**
  ```html
  <ol class="agent-events" data-agent-events aria-label="Live event log">
    <li class="evt" data-evt-kind="assert" data-evt-seq="1">
      <span class="evt-seq">001</span>
      <span class="evt-time">00:00</span>
      <span class="evt-agent">agent-a</span>
      <span class="evt-msg">asserted scope: handoff.txt</span>
    </li>
    ...
  </ol>
  ```
- **Hooks:** `[data-agent-events]`, `.evt`, `[data-evt-kind]`, `[data-evt-seq]`.

### 5.13 Scroll progress

- **Structure:** `<div class="scroll-progress" data-scroll-progress><i></i></div>`
- **Hook:** `[data-scroll-progress]`.

---

## 6. Hero Visualization Concept (FULL SPEC)

### 6.1 Layout

- **Nodes:** 5 agent nodes arranged in a gentle arc/cluster on the left 2/3 of the
  canvas. One central "room" node (larger, glowing) on the right. Edges connect
  each agent to the room (star topology — "one link, many agents").
- **Node design:** Rounded squares (8px radius), 48×48px on desktop. Each shows:
  - A 2-letter agent label (`A`, `B`, `C`, `D`, `E`).
  - A presence indicator (green dot = active, pulsing ring).
  - A role tint: ASSERT agents tinted coral, PROVE agents tinted gold, neutral
    agents tinted accent-violet.
- **Room node:** Larger (72×72px), accent-violet glow, label "ROOM", a live
  member-count badge ("5").
- **Edges:** Hairline paths (`--hairline-inv`) from each agent to the room. When a
  message travels, a bright particle (`--accent`) animates along the edge.

### 6.2 Message travel animation

- On a send event, a particle (6px circle, `--accent` fill + glow) travels from
  the source agent to the room (or room → target for unicast). Duration: 900ms,
  `ease-out`. On arrival, the room pulses and the event log appends a row.
- Particle uses `requestAnimationFrame`, position interpolated along a quadratic
  Bézier (slight arc for visual interest). Only `transform: translate()` is
  mutated.

### 6.3 Presence pulse

- Each active node has an expanding ring (`::after`): `scale(1→1.8)`,
  `opacity(0.5→0)`, `2s ease-out infinite`, per-node phase offset (0–800ms).
- Member-count badge updates when agents join/leave.

### 6.4 Event log append

- Each event appends an `<li class="evt">` to `[data-agent-events]`. Newest at
  bottom (or top — decision: bottom, like a terminal). Max 12 visible rows;
  older rows fade out and are removed from DOM after a delay.
- Append animation: `translateY(8px→0)`, `opacity(0→1)`, `200ms ease-out`.
- Each event has a kind (`assert`, `prove`, `join`, `send`, `refuse`), a sequence
  number, a timestamp, an agent name, and a message string.

### 6.5 Gate-refusal sequence (the signature moment)

- A deliberate event: an agent attempts to claim a task with a **stale fencing
  token**. The evidence gate fires:
  1. The agent's message travels to the room (900ms).
  2. The room flashes: `translateX(0→-4→4→-2→0)` + background tint to `--refuse`
     (600ms).
  3. The gate readout changes from "armed" → "REFUSED: stale fencing token".
  4. A red event row appends: `refuse — agent-a: stale_fencing_token`.
  5. The particle dissipates (opacity→0) at the room boundary — it does NOT land.
- This sequence fires automatically once every ~18s (looping demo), and can be
  triggered manually by clicking a "Trigger refusal" button.

### 6.6 Playability

- **Hover a node:** Tooltip with agent name + role. Node scales to 1.08, glow
  intensifies.
- **Click a node:** Sends a message from that agent to the room. The message
  content is drawn from a pre-authored pool (real product strings). The event log
  appends. The message travels. The room pulses.
- **Click the room:** Broadcasts a message to all agents (particles travel room →
  each agent simultaneously).
- **Keyboard:** All nodes are focusable (`tabindex="0"`). Enter/Space sends.
  Arrow keys cycle focus between nodes.

### 6.7 Performance budget

- Canvas renders at `devicePixelRatio` (capped at 2). RAF loop only runs when the
  canvas is in the viewport (IntersectionObserver pauses when offscreen).
- Max 60fps. If frame time > 16ms for 3 consecutive frames, reduce particle count.
- No filters, no shadows on canvas (CSS shadows are on DOM overlays only).
- Pre-render static elements (node labels) to an offscreen canvas; only animate
  particles + pulses.
- Budget: < 8ms/frame for canvas draw.

### 6.8 Text alternative (CRITICAL)

- Below the canvas (visually, as an overlay that can be toggled; structurally, as
  a sibling `<ol data-agent-events-fallback>`), the SAME events rendered in the
  canvas are rendered as real DOM text. The canvas engine writes to BOTH the canvas
  and this list on every event.
- **No-JS:** The list is seeded with the first 5 static events (rendered
  server-side in the HTML). The canvas is never mounted. The content is identical.
- **Screen readers:** `[data-agent-events]` has `aria-live="polite"`. The canvas
  has `aria-hidden="true"`. The fallback list carries the full content.
- **Reduced motion:** Canvas freezes to a static state (all nodes + edges drawn,
  no particles). The event list continues to update (text only).

### 6.9 Static seed events (rendered server-side for no-JS / fallback)

```
001  [join]    agent-a joined the room
002  [join]    agent-b joined the room
003  [assert]  agent-a asserted scope: handoff.txt
004  [prove]   agent-b claimed task (fencing 1812495659374878)
005  [refuse]  agent-a: stale_fencing_token (refused)
```

> These mirror the real protocol flow from the INTEROP transcripts. Real strings,
> real fencing token format, real error codes.

---

## 7. Truthful Content Map

Every product claim, its source, and where it appears.

| Claim | Source | Where |
| --- | --- | --- |
| "One link. Many agents. All governed." | PRODUCT_ROADMAP §2 (the Room concept) | Hero headline |
| "9 documented MCP paths" | `index.html` current + compatibility page | Hero stat strip, proof list |
| "1 verified Finalisma host integration (OpenCode 1.18.13)" | INTEROP_VALIDATION §1, §3 | Hero stat strip, proof list |
| "0 provider credentials stored" | PRODUCT_ROADMAP §4 truthfulness + current page | Hero stat strip, proof list |
| "0 runtime dependencies (stdlib + SQLite)" | AGENTS.md "dependency-free" promise | Proof list |
| "1 coordinator node (single-node preview)" | PRODUCT_ROADMAP §4 (honest boundary) | Proof list |
| "Next proof pair: Claude Code + Cursor" | AGENTS.md "one open structural gap" | Proof section note |
| "$1,000 / workspace / month" | Current cohort section (pricing hypothesis) | Pricing card |
| "$500 deposit credited toward the first month" | Current cohort section | Pricing card |
| "30-day free pilot" | Current cohort section | Pricing card |
| "stdio or authenticated Streamable HTTP" | INTEROP_HTTP §1 (transport) | Connect tier tabs |
| "MCP is the tool protocol your hosts already speak" | Current page body | Problem / how-it-works |
| "ordered, idempotent, replayable from the last cursor" | Current page body (event log) | How-it-works |
| "scoped consent, fencing lease, evidence gate" | Current page body + INTEROP | How-it-works |
| Real tool names: `finalisma_register_agent`, `finalisma_create_pairing`, `finalisma_join_pairing`, `finalisma_claim_task`, `finalisma_verify_task`, `finalisma_complete_task`, `finalisma_room_*` | INTEROP_HTTP §verbatim transcript | Connect tier config blocks |
| Real error strings: `pairing_unavailable / "Pairing is consumed"`, `member_required / "Only room members can access this room"`, `actor_auth_invalid / "Actor token is invalid"`, `bootstrap_reused`, `Actor token mismatch` | INTEROP_HTTP §negative, INTEROP_SDK §negative, INTEROP_BRIDGE §negative | Live demo refusal section |
| `protocolVersion: 2025-11-25` | INTEROP_HTTP §initialize | Connect tier (HTTP panel) |
| `serverInfo: finalisma-mcp/0.1.0` | INTEROP_HTTP §initialize | Connect tier (HTTP panel) |
| `mcp.json` config shape (stdio) | INTEROP_VALIDATION §3 (opencode schema) | Connect tier (stdio panel) |
| SDK: `FinalismaClient`, `_call("finalisma_room_*")` | INTEROP_SDK §honest status | Connect tier (SDK panel) |
| Bridge: `ClipboardBridge`, `PollingBridge`, `WebhookBridge` | INTEROP_BRIDGE §summary | Connect tier (bridge panel) |

### 7.1 Hero headline + tagline (PROPOSED)

- **Headline:** "One link. Many agents. All governed."
- **Tagline:** "Open one room, share it with every agent on the task, and they all
  communicate — with ordered delivery, scoped consent, and an evidence gate that
  refuses stale or out-of-scope work."
- **Rationale:** Replaces "One incident. Two agents. One account of what happened."
  (the OLD two-party headline). The new headline reflects the product shift from
  two-party pairing (the wedge) to N-agent rooms (the product). It is truthful:
  one multi-use link admits N agents (Wave E Rooms implemented), governed via
  consent/scopes/fencing/evidence (all implemented). "Many agents" not a specific
  number — honest about the cap being plan-bound.

### 7.2 What is REMOVED (editorial framing)

- "Field Notes" — gone from brand, nav, folios, footer.
- "In this dispatch" — the numbered article sidebar (`.cover-rail`) is gone.
- "Reported from the incident queue" — gone from byline.
- "folio" lines (`.folio-line`) — gone.
- Numbered article navigation — gone.
- Magazine-column 12-col feature grid for body copy — replaced by product sections.
- The "Fig. 1" plate (`.plate`, `[data-recon-plane]`) — replaced by the hero canvas.
- `.keylist` / `.factlist` as the primary content structure — replaced by
  `[data-steps]`, `[data-proof-list]`.

---

## 8. Test-Invariant Reconciliation Plan

For each `test_site.py` invariant: KEEP or REPLACE with documented equivalent.
Same for `capture-site-qa.cjs` hooks.

### 8.1 `test_landing_page_has_truthful_semantic_launch_surface`

| Assertion | Verdict | Replacement / Note |
| --- | --- | --- |
| Exactly one `<h1>` | **KEEP AS-IS** | Still one h1 (hero headline). |
| `"One incident. Two agents. One account of what happened."` | **REPLACE** | New headline: `"One link. Many agents. All governed."` — update assertion string. |
| `"9 documented MCP paths"` | **KEEP AS-IS** | Still in hero stat strip + proof list. |
| `"1 verified Finalisma host integration"` | **KEEP AS-IS** | Still in hero stat strip. |
| `"next proof pair, Claude Code + Cursor"` | **KEEP AS-IS** | In proof section note. |
| `type="application/ld+json"` | **KEEP AS-IS** | JSON-LD retained (updated description). |
| `og:site_name = "Finalisma"` | **KEEP AS-IS** | Retained. |
| `twitter:image = "/assets/og-card.png"` | **KEEP AS-IS** | Retained (asset still shipped). |
| `aria-live="polite"` | **KEEP AS-IS** | On `[data-agent-readout]` + `[data-agent-events]`. |
| `data-sim-label` + `"Simulated account · no credentials · no live session"` | **REPLACE** | New hook: `[data-agent-readout]` with seed text `"Ready · simulated demo · no credentials"`. Update assertion. |
| `"MCP is the tool protocol"` | **KEEP AS-IS** | In problem/how-it-works copy. |
| `"single-node"` | **KEEP AS-IS** | In proof list + footer. |
| `data-cohort-form` | **KEEP AS-IS** | Retained (form still exists, possibly moved to pricing section). |
| `"Watch the 42-second proof"` | **REPLACE** | New: `"Connect an agent"` (CTA). Update assertion string, OR keep a demo link with new text. |
| `href="/demo.html"` | **KEEP AS-IS** | Demo page still exists. |
| `"$500 deposit"` | **KEEP AS-IS** | In pricing section. |
| `assertNotIn "verified agent handoff layer"` | **KEEP AS-IS** | Still absent. |
| `assertNotIn "Finalisma A2A Standard"` | **KEEP AS-IS** | Still absent. |
| `assertNotIn "gpt-5.5"` | **KEEP AS-IS** | Still absent. |
| `assertNotIn "lorem ipsum"` | **KEEP AS-IS** | Still absent. |

### 8.2 `test_progressive_enhancement_and_gated_story_cta`

| Assertion | Verdict | Replacement / Note |
| --- | --- | --- |
| `documentElement.classList.add('js')` | **KEEP AS-IS** | Retained. |
| `[hidden] { display: none !important; }` | **KEEP AS-IS** | Retained. |
| `.reveal { opacity: 1; transform: none; }` | **KEEP AS-IS** | Retained (reveal base state). |
| `.js .reveal` | **KEEP AS-IS** | Retained. |
| `data-story-next[^>]*hidden` | **REPLACE** | New hook: `[data-agent-canvas]` always present; the gated-CTA concept is replaced by the hero canvas being always-mounted. New assertion: `[data-agent-canvas]` exists and `[data-agent-events-fallback]` has ≥5 `<li>` children. |
| `data-recon-plane` | **REPLACE** | New hook: `[data-agent-canvas]`. Update assertion. |
| `perspective: 1500px` | **REPLACE** | New: `aspect-ratio` on `[data-agent-canvas-wrap]`. Update assertion. |
| `transform-style: preserve-3d` | **REPLACE** | New: `transform: translateZ(0)` on canvas (GPU layer). Update assertion. |

### 8.3 `test_unposted_entries_never_rely_on_colour_alone`

| Assertion | Verdict | Replacement / Note |
| --- | --- | --- |
| `.keylist` / `.factlist` structure (≥11 items, each with `.kt` or `<p>`) | **REPLACE** | New structure: `[data-proof-list]` with `[data-proof-item]` (5 items), each with `.proof-stat` (coloured number) + `.proof-label` (text). Also `[data-steps]` with `[data-step]` (4 steps), each with `.step-num` + `<h3>` text. Total 9 colour-coded rows, each with adjacent readable text. Update assertion to target `[data-proof-item]` and `[data-step]`, requiring `.proof-label` / `h3` text next to `.proof-stat` / `.step-num`. Invariant intent preserved: colour never carries meaning alone. |

### 8.4 `test_static_launch_bundle_contains_guides_articles_and_social_asset`

**KEEP AS-IS.** All required files (index.html, styles.css, app.js, robots.txt,
llms.txt, 404.html, license.html, demo.html, demo-stage.html, demo.css,
demo-stage.js, site.webmanifest, docs set, og-card.png, poster, mp4/webm/vtt,
blog articles) are still shipped. The test asserts file presence + dimensions +
headers — none of these change with a redesign.

### 8.5 `test_static_internal_content_links_resolve_inside_site_bundle`

**KEEP AS-IS.** All internal links must still resolve. Implementation must ensure
new section anchors (`#problem`, `#how`, `#demo`, `#connect`, `#pricing`, `#proof`)
exist in the DOM.

### 8.6 `test_compatibility_page_keeps_documented_and_verified_distinct`

**KEEP AS-IS.** This targets `docs/compatibility.html`, not `index.html`. Untouched.

### 8.7 `test_recorded_demo_is_redacted_and_grounded_in_a_real_run`

**KEEP AS-IS.** Targets `demo.html` + `assets/demo-transcript.json` + video assets.
Untouched by the landing-page redesign.

### 8.8 `test_server_mounts_self_contained_site_and_branded_404`

| Assertion | Verdict | Replacement / Note |
| --- | --- | --- |
| `200` on `/` | **KEEP AS-IS** | Server still serves index.html. |
| `"One incident. Two agents."` in `/` body | **REPLACE** | New hero copy. Update assertion to `"One link. Many agents."` |
| `200` on `/docs/compatibility.html` + `"Documented is not verified."` | **KEEP AS-IS** | Untouched. |
| `200` on `/demo.html` + `"A real coordinator run."` | **KEEP AS-IS** | Demo page untouched. |
| `site.webmanifest` name = "Finalisma" | **KEEP AS-IS** | Untouched. |
| `200` on demo mp4 | **KEEP AS-IS** | Untouched. |
| og-card.png `Cache-Control` | **KEEP AS-IS** | Untouched. |
| `404` + `"This path is not in the account."` | **KEEP AS-IS** | 404 page + message retained (may update wording to product voice but keep the test string, OR update both). |
| Path traversal blocked | **KEEP AS-IS** | Server-level, untouched. |

### 8.9 `TestCountSyncTests`

**KEEP AS-IS.** This auto-discovers the live count. No action needed unless tests
are added/removed.

### 8.10 `capture-site-qa.cjs` hook reconciliation

| Old hook | Verdict | New hook |
| --- | --- | --- |
| `[data-scroll-story]` | **REPLACE** | `[data-agent-canvas-wrap]` (hero is the persistent interactive surface; no scroll-locked story). |
| `.recon-sticky` | **REPLACE** | `.hero-canvas` (sticky on desktop within the hero section only, not scroll-locked). |
| `.recon-track` | **REPLACE** | `[data-agent-events]` (the event list). |
| `.ledger-ground` | **REPLACE** | `.hero` (the hero section is the "ground"). |
| `[data-recon-plane]` | **REPLACE** | `[data-agent-canvas]`. |
| `.entry-state` | **REPLACE** | `[data-evt-kind]` (event kind on each log row). |
| `.desktop-nav` / `#mobile-nav` / `.nav-toggle` | **KEEP** | Same hooks retained (nav structure preserved). |
| `[data-copy-target]` | **REPLACE** | `[data-copy]` (generic copy hook; value = tier id or "link"). |
| `[data-cohort-form]` | **KEEP** | Retained (form still exists). |
| `--split` | **REPLACE** | `--shell` (the layout token). Remove `--split` split-line concept. |
| `data-demo-events`, `data-demo-session`, `data-demo-action` | **REPLACE** | `[data-agent-events]`, `[data-agent-readout]`, `[data-gate]`. The old step-advance model is replaced by the live-event-stream model. |
| `.folio-label` | **REMOVE** | No folio concept. Update harness to skip. |
| `[data-story-next]` | **REPLACE** | `[data-agent-canvas]` (the canvas is always the "next" interactive element). |
| `.type-wipe` | **REMOVE** | No type-wipe effect. The terminal uses character-append if anything. |

### 8.11 Equivalent invariants for the new QA harness

The updated `capture-site-qa.cjs` must assert:

1. **Zero console errors** — unchanged.
2. **Zero failed requests** — unchanged.
3. **Zero bad responses** (origin, status ≥400) — unchanged.
4. **axe-clean** (wcag2a/2aa/21a/21aa) — unchanged.
5. **Mobile no-overflow** — unchanged (check `[data-agent-canvas-wrap]` doesn't overflow).
6. **Progressive enhancement:** `.reveal` base visible, `.js .reveal` gated, `[hidden]` rule — unchanged.
7. **Reduced motion:** `prefers-reduced-motion` → reveals visible, canvas frozen (not blank) — new check: `[data-agent-canvas]` still has painted content (non-zero pixels) under reduced motion.
8. **No-JS:** headline visible (`h1` height > 0), canvas fallback has ≥5 events (`[data-agent-events-fallback]` children ≥ 5), no `js` class.
9. **Form labels:** all inputs in `[data-cohort-form]` (or new form hook) have associated labels — unchanged.
10. **Focus styles:** visible focus-visible on all focusable elements — unchanged.
11. **Landmarks:** one `main`, one `nav`, one `header`, one `footer`, one `h1` — unchanged.
12. **Skip link:** present — unchanged.
13. **Live regions:** `[aria-live]` present (event log + readout) — unchanged count, new hooks.
14. **Hero canvas mounts:** `[data-agent-canvas]` has non-zero dimensions within 2s — NEW.
15. **Agent graph animates:** at least 3 distinct canvas states captured over 2s — NEW (sample pixels or a `window.__agentFrameCount` hook).
16. **Gate refusal fires:** `[data-gate-state]` reaches "REFUSED" within the demo loop — NEW.
17. **Link generated:** `[data-generated-link]` is non-empty and matches `finalisma.*/r/` pattern — NEW.
18. **Copy works:** clicking `[data-copy]` sets clipboard + visible status — NEW (replaces old copy-status string check).
19. **Tier tabs switch:** clicking `[data-tier-tab]` shows the matching `[data-tier-panel]` — NEW.
20. **No third-party requests** — unchanged.

---

## 9. Accessibility Checklist

| Requirement | Implementation |
| --- | --- |
| **Keyboard operability** | All interactive elements (nodes, buttons, tabs, form controls) are native focusable elements or have `tabindex="0"`. Enter/Space activates. Arrow keys cycle canvas nodes. Escape closes mobile nav. |
| **Focus-visible** | `:focus-visible` outline: 2.5px solid `--accent`, offset 3px. On dark surfaces: `--accent-2`. Never removed (`outline: none`) without replacement. |
| **Skip link** | `.skip-link` to `#main`. Visible on focus. |
| **ARIA live regions** | `[data-agent-events]` `aria-live="polite"` (event log). `[data-agent-readout]` `aria-live="polite"` (canvas status). Copy status `aria-live="polite"`. |
| **Canvas fallback** | `[data-agent-events-fallback]` is real DOM, seeded with 5 static events, always present. Canvas has `aria-hidden="true"`. |
| **Form labels** | Every input/textarea has an associated `<label>` (wrapped or `for`-linked). |
| **Heading order** | One `h1` (hero). `h2` per section (problem, how, demo, connect, proof, pricing). `h3` within steps/cards. No skipped levels. |
| **Landmarks** | `<header>`, `<nav>` (primary), `<main id="main">`, `<footer>`. One of each. |
| **Contrast** | All pairs ≥4.5:1 (normal) / ≥3:1 (large). See §1.3. |
| **prefers-reduced-motion** | Fully honoured. Canvas freezes to legible static state. All animations disabled. Scroll behaviour set to `auto`. |
| **Colour never alone** | ASSERT/PROVE semantic colours always paired with text labels (`.proof-label`, `.step-num` + `h3`, `[data-evt-kind]` + `.evt-msg`). Re-expressed invariant in §8.3. |
| **No-JS core content** | Headline, all section copy, proof list, pricing, footer, and the first 5 event-log entries render without JS. Canvas is enhancement-only. |
| **Touch targets** | All interactive elements ≥ 44×44px on mobile (buttons, tabs, nodes). |
| **Reduced-motion canvas** | Static state shows all nodes, edges, and the current event list. Caption: "Animation paused (reduced motion)." |

---

## 10. Page Structure (final section order)

1. **Header** (sticky) — brand, nav, CTA, mobile toggle.
2. **Hero** — live agent-graph canvas + headline + tagline + stat strip + CTAs.
3. **Problem** — one tight beat. The chaos of pasted prompts.
4. **How it works** — 4 stepped, animated (open room → consent+scope → addressing → evidence gate).
5. **Live demo** — terminal output, generated+copied link, gate refusal firing.
6. **Connect** — four tiers, tabbed, real copy-paste config blocks.
7. **Proof** — evidence-backed claims with real numbers + real refusal strings.
8. **Pricing / CTA** — hypothesis, deposit, pilot, managed pilot link.
9. **Footer** — links, version, licence.

> Total: 9 sections. No section exceeds one viewport unless interactive (hero,
  demo). Ruthless.

---

## 11. Open Questions

1. **Demo page (`demo.html`)** — This redesign touches `index.html` only. Should
   `demo.html` (the 42-second video proof) be restyled to match V2, or left as-is
   for now? Recommendation: restyle to V2 tokens in a follow-up lane; keep the
   video asset and transcript unchanged.
2. **`/blog/`** — The blog currently uses the article-page CSS (`.article-body`).
   Recommendation: migrate blog index + articles to V2 tokens in a follow-up lane.
   Not blocking D2.
3. **`design-target.svg`** — The QA harness references a source SVG target.
   This should be regenerated to match V2 (or removed if the comparison model
   changes). Recommendation: regenerate as a static HTML/CSS mock, not SVG.
4. **Canvas engine module** — Should be a new file `site/agent-canvas.js`
   (separate from `app.js`). `app.js` handles nav, reveal, tiers, copy, form.
   `agent-canvas.js` handles the hero canvas only. This keeps concerns separate
   and lets the canvas be deferred-loaded.
5. **Reduced-motion static canvas** — Implementation must paint a static frame
   (nodes + edges + last event state) even when motion is reduced. This requires
   the canvas engine to expose a `renderStatic()` method. Confirmed required.

---

*End of DESIGN_SYSTEM_V2.md — Wave D2 ART-DIRECTION deliverable.*
