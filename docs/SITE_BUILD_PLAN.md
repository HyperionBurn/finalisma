# SITE BUILD PLAN — Wave D3: Awwwards-tier Weft Marketing Site

> **Status:** BUILD-PLAN lane deliverable. Authoritative implementation spec for the Astro 5 + R3F + GSAP scroll-narrative rebuild.
> **Scope:** Rebuilds `site/index.html` + `site/styles.css` + `site/app.js` (the marketing surface only) via an Astro 5 source tree in `web/` that builds static output to `site/`. All other committed `site/` content (blog, docs, demo, assets, 404, license, llms, robots, manifest) is PRESERVED byte-for-byte.
> **Supersedes:** The Wave-D2 vanilla `site/agent-canvas.js` artifact (untracked) is deleted; the "FIELD NOTES" magazine framing is retired per `DESIGN_SYSTEM_V2.md`.
> **Source of truth:** `docs/AWARD_REFERENCE_2026.md` (the bar), `docs/DESIGN_SYSTEM_V2.md` (art direction), `AGENTS.md` (discipline), `tests/test_site.py` (invariants), `scripts/capture-site-qa.cjs` (QA harness).

---

## 1. Stack summary (SETTLED — do not relitigate)

| Layer | Choice | Pinned version | Justification |
|---|---|---|---|
| SSG | Astro 5 (static) | `astro@5.10.0` (verify at install) | Server-rendered HTML, client islands for WebGL. Node ≥22 satisfied by Node 24. |
| React island | @astrojs/react | matching Astro 5 peer | Required for the R3F hero island. |
| 3D | @react-three/fiber + @react-three/drei + three | `r3f@9.7.0`, `drei@10.x`, `three@0.170.0` | Declarative R3F; drei for `<OrbitControls>`, `<AdaptiveDpr>`, `<PerformanceMonitor>`, postprocessing helpers. |
| Scroll | GSAP + ScrollTrigger + Lenis | `gsap@3.15.0`, `lenis@1.3.26` | GSAP ScrollTrigger drives the six-beat camera spline; Lenis provides weighted smooth scroll. |
| Styling | Tailwind CSS 4 (Vite plugin) | `tailwindcss@4.3.3`, `@tailwindcss/vite@4.3.3` | Tailwind 4 uses the Vite plugin (no PostCSS). `@theme` tokens map 1:1 to DESIGN_SYSTEM_V2 palette. |
| Motion (DOM) | motion (formerly framer-motion) | `motion@13.0.0` | Section reveals, tier-tab switching, event-log appends. |
| Runtime | Node | `v24.18.0` / npm `11.16.0` | Astro 7 needs Node ≥20; Astro 5 needs ≥18 — Node 24 satisfies both. We pin Astro 5. |
| Registry | npm | reachable | Self-host fonts only (no Google Fonts CDN). |

**Engine check:** Astro 5 requires Node ≥18.0.0; Node 24.18.0 satisfies. (Astro 7 would require ≥20 — we stay on Astro 5 for ecosystem stability with R3F 9 / React 19.)

**Package-lock:** `package-lock.json` committed. No installs from CDNs at runtime.

---

## 2. Astro project layout — full tree under `web/`

```
C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/
├── astro.config.mjs
├── package.json
├── package-lock.json
├── tsconfig.json
├── .gitignore                  # ignores node_modules/, dist/, .legacy-staging/
├── public/                     # unused by default — legacy assets preserved via snapshot (§3)
├── src/
│   ├── env.d.ts
│   ├── styles/
│   │   └── global.css          # @import "tailwindcss"; @theme { ... } tokens
│   ├── layouts/
│   │   └── RootLayout.astro    # <html>, <head>, fonts, JSON-LD, og/twitter, <body>
│   ├── components/
│   │   ├── Header.astro        # sticky nav, brand, section links, CTA, mobile toggle
│   │   ├── HeroWebGL.astro     # headline + canvas island + stat strip + CTAs
│   │   ├── SceneCanvas.tsx     # R3F <Canvas> island (client:only="visible")
│   │   ├── shaders/
│   │   │   ├── nodeMaterial.ts # fresnel + pulse GLSL
│   │   │   ├── messageParticle.ts # additive glow points
│   │   │   └── roomCore.ts     # refraction/noise core
│   │   ├── Problem.astro
│   │   ├── HowItWorks.astro    # [data-steps] 4-step
│   │   ├── LiveDemo.astro      # terminal + generated link + gate
│   │   ├── ConnectTiers.astro  # tabbed config blocks
│   │   ├── Proof.astro         # [data-proof-list]
│   │   ├── Pricing.astro
│   │   ├── Footer.astro
│   │   └── EventLog.astro      # [data-agent-events] DOM list driven by scene
│   └── pages/
│       └── index.astro         # composes all sections; server-rendered HTML
└── scripts/
    ├── preserve-legacy.cjs   # pre-build snapshot of site/assets + legacy pages; post-build restore
    └── verify-preservation.cjs # post-build assertion that test-required files/strings survive
```

### 2.1 `astro.config.mjs`

```js
import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';

export default defineConfig({
  root: resolve('./'),
  outDir: resolve('../site'),          // CRITICAL: builds INTO the committed site/ bundle
  publicDir: resolve('./public'),       // effectively unused — legacy assets preserved via snapshot (§3)
  output: 'static',
  build: { assets: '_astro' },          // default; bundled JS/CSS land in site/_astro/, never site/assets/
  vite: {
    plugins: [tailwindcss()],
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            three: ['three'],
            r3f: ['@react-three/fiber', '@react-three/drei'],
            gsap: ['gsap', 'lenis'],
          },
        },
      },
    },
  },
  integrations: [react()],
  trailingSlash: 'never',
});
```

### 2.2 `package.json` (scripts)

```json
{
  "name": "weft-marketing-d3",
  "private": true,
  "type": "module",
  "engines": { "node": ">=22" },
  "scripts": {
    "dev": "astro dev --port 4174",
    "build": "node scripts/preserve-legacy.cjs && astro build && node scripts/preserve-legacy.cjs && node scripts/verify-preservation.cjs",
    "preview": "astro preview --port 4175",
    "astro": "astro"
  },
  "dependencies": {
    "astro": "5.10.0",
    "@astrojs/react": "4.2.0",
    "react": "19.0.0",
    "react-dom": "19.0.0",
    "three": "0.170.0",
    "@react-three/fiber": "9.7.0",
    "@react-three/drei": "10.0.0",
    "gsap": "3.15.0",
    "lenis": "1.3.26",
    "motion": "13.0.0"
  },
  "devDependencies": {
    "@tailwindcss/vite": "4.3.3",
    "tailwindcss": "4.3.3",
    "@types/react": "19.0.0",
    "@types/react-dom": "19.0.0",
    "@types/three": "0.170.0",
    "typescript": "5.7.0"
  }
}
```

---

## 3. Build→site pipeline — preservation spec (CRITICAL)

Astro's `outDir` **replaces** the output directory on every build. The committed `site/` contains hand-authored pages the tests assert on. We MUST preserve them.

### 3.1 Mechanism: legacy snapshot/restore + `public/` for NEW marketing assets only

**Decision:** Use THREE mechanisms, in order:

1. **Legacy snapshot/restore (`preserve-legacy.cjs`)** — the PRIMARY mechanism. Before the Astro build, snapshot the ENTIRE committed `site/assets/` directory PLUS the hand-authored pages listed in §3.2 into `web/.legacy-staging/`. After the build, restore them byte-for-byte into `site/`. This is the only correct approach because **Astro wipes `outDir` on every build** — `site/assets/` (fonts, og-card.png, weft-demo-*.mp4/webm/vtt, poster, favicon, demo-transcript.json) is deleted by the wipe and MUST be restored. Astro 5 writes its OWN bundled JS/CSS into `site/_astro/` (default `build.assets`), so `site/assets/` is the clean restore target with no collision.

2. **`public/` passthrough** for ANY genuinely new marketing asset the Astro build needs at build time (e.g. a regenerated og-card if the design requires it). Default: unused. The existing assets live in `site/assets/` and are preserved via mechanism 1, so `public/` stays effectively empty.

3. **`site/assets/fonts/`** — preserved via mechanism 1 (they are part of the `site/assets/` snapshot). The Astro `@font-face` declarations reference `assets/fonts/...` (relative), matching the restored location.

**Why snapshot the whole `site/assets/` (not just files listed in §3.2):** `tests/test_static_launch_bundle_contains_guides_articles_and_social_asset` asserts `site/assets/og-card.png` (1200x630 PNG), `site/assets/weft-demo-poster.png` (1280x720), `site/assets/weft-demo.mp4` / `.webm` / `.vtt`, and the og/twitter meta reference `/assets/og-card.png`. Any of these landing at the wrong path (e.g. `site/og-card.png`) breaks the test. The whole-directory snapshot makes preservation exhaustive and self-evident.

### 3.2 Exact copy source→dest mapping

| Source (pre-build snapshot) | Dest (post-build restore) | Type |
|---|---|---|
| `site/assets/*` → `web/.legacy-staging/assets/*` → | `site/assets/*` | directory (fonts, og-card, poster, demo video/vtt, favicon, transcript) |
| `site/blog/*` → `web/.legacy-staging/blog/*` → | `site/blog/*` | directory |
| `site/docs/*` → `web/.legacy-staging/docs/*` → | `site/docs/*` | directory |
| `site/demo.html` → `web/.legacy-staging/demo.html` → | `site/demo.html` | file |
| `site/demo.css` → `web/.legacy-staging/demo.css` → | `site/demo.css` | file |
| `site/demo-stage.html` → `web/.legacy-staging/demo-stage.html` → | `site/demo-stage.html` | file |
| `site/demo-stage.js` → `web/.legacy-staging/demo-stage.js` → | `site/demo-stage.js` | file |
| `site/404.html` → `web/.legacy-staging/404.html` → | `site/404.html` | file |
| `site/license.html` → `web/.legacy-staging/license.html` → | `site/license.html` | file |
| `site/llms.txt` → `web/.legacy-staging/llms.txt` → | `site/llms.txt` | file |
| `site/robots.txt` → `web/.legacy-staging/robots.txt` → | `site/robots.txt` | file |
| `site/site.webmanifest` → `web/.legacy-staging/site.webmanifest` → | `site/site.webmanifest` | file |

`.legacy-staging/` is gitignored. `preserve-legacy.cjs` exits non-zero if any source path is missing before the build.

### 3.3 Verification of preservation

`scripts/verify-preservation.cjs` runs AFTER `preserve-legacy.cjs` and asserts:

- Every file in the table above exists in `site/`.
- Every test-asserted string is present in `site/index.html` (see §9 reconciliation).
- No test-asserted string is missing from the preserved pages.
- `site/assets/og-card.png` is 1200x630 PNG (header check).
- `site/assets/weft-demo-poster.png` is 1280x720 PNG.
- `site/assets/weft-demo.mp4` / `.webm` / `.vtt` exist with correct headers.
- `site/docs/` contains exactly the 7 required HTML files.
- `site/blog/` contains ≥ 4 articles.

If any assertion fails, the build exits non-zero and prints the missing item.

### 3.4 `agent-canvas.js` fate

`site/agent-canvas.js` is the untracked Wave-D2 vanilla canvas artifact. It is **deleted** by the build (the superseded implementation; the R3F island replaces it). It is NOT part of the snapshot. Fonts, og-card.png, and the demo video survive via the `site/assets/` snapshot (§3.1), and the Astro `@font-face` declarations reference `assets/fonts/...` (relative), which resolves to the restored `site/assets/fonts/`.

---

## 4. Six-beat scroll storyboard — concrete camera/threshold table

This is the heart of the award brief. Each beat maps to a normalized scroll progress, a camera keyframe on a `CatmullRomCurve3`, a scene event, and a content reveal.

| Beat | Scroll progress | Camera position (x,y,z) | Camera target (x,y,z) | Scene event | Content reveal | Duration / Easing |
|---|---|---|---|---|---|---|
| **1. Wide** | 0.00 – 0.12 | (0, 0, 14) | (0, 0, 0) | One link node, alone in space. Slow idle breathing. No agents yet. | Hero headline + tagline + stat strip (visible at load, not scroll-gated) | — |
| **2. Approach** | 0.12 – 0.30 | (0, 0.5, 11) → (0, 0, 9) | (0, 0, 0) | Agents A–E arrive on arcs, edges draw to the room node. Presence pulses fire. Roster badge fills 1→5. | `#problem` section enters (pasted-prompt chaos card) | 1200ms / `power2.inOut` |
| **3. Inside the room** | 0.30 – 0.50 | (0, 0, 6) → (0, 0, 4) | (0, 0, 0) | Camera moves in. Messages travel edges: unicast (agent A→room, single particle), then group (room→{A,B,C}, 3 particles), then broadcast (room→all, 5 particles). Each visually distinct colour (accent / assert / prove). | `#how` section enters (4 steps animate in) | 1400ms / `power3.inOut` |
| **4. The Gate** | 0.50 – 0.66 | (0, 0, 4) → (0, 0.2, 3.5) | (0, 0, 0) | A stale/forged action arrives. **REFUSED.** Colour temperature shifts cool→warm-red. Room flashes `translateX(0→-4→4→-2→0)`. Gate readout → "REFUSED: stale fencing token". Particle dissipates at room boundary. Event row appends `[data-evt-kind="refuse"]`. | `#demo` section enters (terminal + gate) | 900ms gate flash / `power4.out` |
| **5. Pull back** | 0.66 – 0.82 | (0, 0.2, 3.5) → (0, 0, 7) | (0, 0, 0) | Camera pulls back. Ordered event log resolves; evidence recorded. Room persists, calmer. Colour temperature returns to neutral-cool. | `#connect` section enters (four tiers) | 1200ms / `power2.inOut` |
| **6. Land** | 0.82 – 1.00 | (0, 0, 7) → (0, 0, 9) | (0, 0, 0) | Camera settles. Room fully composed, all agents present. Colour temperature warms to light landing. | `#pricing` + `#proof` sections enter; footer | 1000ms / `power2.out` |

**Camera spline:** A `THREE.CatmullRomCurve3` through the 12 position keyframes (2 per beat: entry + exit). Normalized scroll progress (0→1, driven by Lenis + ScrollTrigger) maps to `curve.getPointAt(progress)`. The camera target is a second spline (or a fixed look-at with slight upward drift at the Gate).

**Beat detection:** ScrollTrigger creates 6 triggers at the progress thresholds. On each trigger enter, the scene dispatcher fires the beat's event sequence (agent joins, message sends, refusal). The scene is **idempotent** — re-entering a beat does not double-fire; it snaps to the beat's composed state.

**Text assembly per beat:** The hero headline stays pinned. A `[data-beat-caption]` element below the canvas updates per beat:
- Wide: "One link."
- Approach: "Many agents join."
- Inside: "They all communicate."
- The Gate: "The gate refuses stale work."
- Pull back: "Evidence recorded."
- Land: "Connect your agent."

---

## 5. WebGL scene architecture

### 5.1 R3F component tree

```
<Canvas>                          <!-- SceneCanvas.tsx, client:only="visible" -->
  <AdaptiveDpr pixelated />       <!-- drei: scale dpr by perf tier -->
  <PerformanceMonitor />          <!-- drei: detect GPU tier -->
  <CameraRig />                   <!-- reads normalized scroll → spline -->
  <RoomCore />                    <!-- central node, refraction/noise shader -->
  <AgentNodes />                  <!-- 5 instanced node meshes + fresnel material -->
  <EdgeLines />                   <!-- line segments room↔agent -->
  <MessageParticles />            <!-- additive glow points travelling edges />
  <FloorGrid />                   <!-- subtle grid for depth -->
  <EffectComposer>                <!-- light postprocessing: bloom + vignette -->
    <Bloom luminanceThreshold={0.6} intensity={0.6} />
    <Vignette eskil={false} offset={0.3} darkness={0.7} />
  </EffectComposer>
</Canvas>
```

### 5.2 Three.js objects

| Object | Implementation | Notes |
|---|---|---|
| Agent nodes | `InstancedMesh` of rounded-box geometry (48×48, 10px radius via bevel). 5 instances. | Per-instance colour (role tint: assert / prove / accent). Custom `nodeMaterial` shader. |
| Room core | Single `Mesh` (72×72 rounded box, larger). | `roomCore` refraction/noise shader. Member-count badge is a DOM overlay. |
| Edges | `<LineSegments>` with `LineBasicMaterial`. 5 segments room↔agent. | Drawn on agent join (Beat 2). |
| Message particles | `Points` with `ShaderMaterial` (additive glow). Pool of 60. | Reused per send event. |
| Floor grid | `GridHelper` or custom shader grid. Subtle. | Provides depth reference. |

### 5.3 Custom GLSL shaders (three that earn their place)

1. **`nodeMaterial.ts` — Fresnel + pulse.** Each agent node gets a fresnel rim glow that intensifies on presence-pulse. The pulse is time-driven (2.4s cycle, per-instance phase offset). On refusal, the offending node flashes to `--refuse`. Vertex shader passes instance matrix + per-instance colour; fragment computes fresnel from view-normal dot.

2. **`messageParticle.ts` — Additive glow travelling point.** Each message is a `Points` primitive with a circular falloff (no texture — computed in-shader: `smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5))`). Additive blending, colour per send-type (unicast=accent, group=assert, broadcast=prove). Opacity ramps 0→1→1→0 over the 900ms travel.

3. **`roomCore.ts` — Refraction/noise core.** The room node uses a fragment shader with simplex noise (Ashima) to create a subtle animated surface shimmer. On the Gate beat, the noise amplitude spikes and the colour shifts to refuse-tint for 600ms, then returns. This is the "living product" proof — the room visibly reacts.

### 5.4 Camera spline

`CatmullRomCurve3` built from the 12 keyframe positions in §4. `curve.getPointAt(progress)` gives camera position. `CameraRig` is a component that reads `useScrollProgress()` (a shared store updated by ScrollTrigger) and sets `camera.position` + `camera.lookAt(target)` each frame. The target is a second, simpler spline (or lerp between beat targets).

### 5.5 Pointer parallax

On pointer move (throttled), offset the camera target by ±0.3 units in x/y based on normalized pointer position. Disabled under reduced-motion and on touch.

### 5.6 Postprocessing

`@react-three/postprocessing` `EffectComposer` with:
- `Bloom` (luminanceThreshold 0.6, intensity 0.6, mipmapBlur) — for the glow on nodes/particles.
- `Vignette` (offset 0.3, darkness 0.7) — for cinematic framing.

**Tier scaling:** On low tier, disable EffectComposer entirely (bloom + vignette are the first to go). On medium, keep bloom at half resolution. On high, full.

### 5.7 Adaptive quality tiers

Detect GPU tier via `drei`'s `PerformanceMonitor` + a one-frame `WEBGL_debug_renderer_info` probe:

| Tier | Criteria | Polygon scale | Texture scale | Particles | Postprocessing |
|---|---|---|---|---|---|
| High | desktop GPU, ≥4 cores | 1.0 | 1.0 | 60 | full |
| Medium | integrated / mobile flagship | 0.7 | 0.75 | 30 | bloom only |
| Low | old mobile / software GL | 0.5 | 0.5 | 12 | none |

Tier is selected once at load and persisted to `localStorage` (re-detected only if `?retest=1`).

### 5.8 Fallbacks

- **Reduced-motion:** RAF pauses; scene renders a single composed static frame (all agents joined, edges drawn, last event state). Caption `[data-agent-readout]` shows "Animation paused (reduced motion)." Content is fully readable.
- **WebGL unsupported:** `<Canvas>` never mounts. The static poster (`og-card.png` or a server-rendered SVG of the composed scene) is shown in its place. `[data-agent-events-fallback]` carries the 5 seed events.
- **No-JS:** Same as WebGL-unsupported (the island never loads). The full page renders from server HTML.

### 5.9 DOM bridges (scene → DOM)

| Bridge | Mechanism | Target |
|---|---|---|
| Event log | Scene dispatcher calls `window.dispatchEvent(new CustomEvent('agent-event', {detail}))` → React subscribes → appends `<li class="evt">` to `[data-agent-events]` | `[data-agent-events]` |
| Gate state | Same event channel; gate component listens for `refuse` events | `[data-gate-state]` |
| Agent readout | Scene pushes status strings | `[data-agent-readout]` (aria-live="polite") |
| Beat caption | ScrollTrigger fires beat → updates caption text | `[data-beat-caption]` |

### 5.10 Performance

- **Precompute during load:** geometry instancing (one geometry, 5 instances), texture atlas (not needed — shaders are procedural), shadow baking (no shadows in scene — CSS shadows on DOM overlays only).
- **Scroll loop budget < 4ms/frame:** the R3F render loop reads the scroll-driven camera position (no per-frame allocations). Particle updates are a fixed-size pool.
- **RAF paused offscreen:** `IntersectionObserver` on the canvas — when not in viewport, `gl.setAnimationLoop(null)` pauses rendering.
- **60fps sustained:** frame-time monitor; if >16ms for 3 consecutive frames, drop a quality tier.
- **Max draw calls:** < 50 (5 instanced nodes + 1 room + 1 grid + 1 line segments + 1 points + DOM overlays).

---

## 6. Scroll choreography

### 6.1 Lenis + GSAP ScrollTrigger integration

```ts
// In SceneCanvas / a top-level ScrollController component
import Lenis from 'lenis';
import { gsap } from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

const lenis = new Lenis({ duration: 1.2, easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)) });
lenis.on('scroll', ScrollTrigger.update);
gsap.ticker.add((time) => lenis.raf(time * 1000));
gsap.ticker.lagSmoothing(0);
```

Lenis drives the scroll; ScrollTrigger reads it via the `lenis.scroll` proxy. The normalized progress is `ScrollTrigger.progress` (0→1 across the full page).

### 6.2 Per-section pinned states

Only the **hero** section uses a pinned canvas (the canvas stays in view while the headline scrolls past). Subsequent sections scroll naturally. The hero un-pins at scroll progress 0.12 (end of Beat 1) so the camera journey continues in scroll-space.

### 6.3 Colour-temperature shifts

The page background and accent lighting shift between beats via CSS custom properties on `:root`, animated by GSAP:

| Beat | `--bg-temp` | `--light-tint` |
|---|---|---|
| Wide | `#0E0F12` (ink) | neutral |
| Approach | `#0E0F12` | cool violet |
| Inside | `#0E0F12` | neutral |
| The Gate | shift to `#2B1818` (refuse warmth) | refuse-red |
| Pull back | return to `#0E0F12` | neutral-cool |
| Land | `#F7F8FA` (surface — light band) | warm light |

The landing beat transitions the page to a light surface band for pricing/proof — the DESIGN_SYSTEM_V2 dark/light alternation.

### 6.4 No naive scroll listeners

All scroll-driven behaviour goes through ScrollTrigger. No `window.addEventListener('scroll', ...)`. Lenis is the single scroll source of truth.

### 6.5 Reduced-motion

Everything renders statically. The scene shows the final composed state (Beat 6). Content is fully readable. No parallax, no infinite loops, no auto-advancing.

---

## 7. Tailwind 4 theme mapping — exact `@theme` block

In `web/src/styles/global.css`:

```css
@import "tailwindcss";

@theme {
  /* Core palette (DESIGN_SYSTEM_V2 §1, corrected values) */
  --color-ink: #0E0F12;
  --color-ink-2: #16181D;
  --color-ink-3: #1E2128;
  --color-ink-4: #2A2E38;
  --color-surface: #F7F8FA;
  --color-surface-2: #FFFFFF;
  --color-text: #0E0F12;
  --color-text-inv: #F2F3F5;
  --color-text-muted: #6B7280;
  --color-text-muted-inv: #9BA1AD;
  --color-hairline: rgba(14,15,18,0.12);
  --color-hairline-inv: rgba(242,243,245,0.14);

  /* Accent */
  --color-accent: #5B3DF0;
  --color-accent-2: #5434E8;
  --color-accent-ink: #FFFFFF;
  --color-accent-dark: #9D82FF;

  /* Semantic roles */
  --color-assert: #F4756B;        /* dark bg */
  --color-assert-light: #BE2B1B;  /* light bg */
  --color-prove: #E0B34A;
  --color-prove-light: #7C5708;
  --color-success: #4ADE80;
  --color-success-light: #15803D;
  --color-refuse: #F87171;
  --color-refuse-light: #B91C1C;
  --color-success-ink: #0E0F12;
  --color-refuse-ink: #0E0F12;

  /* Type */
  --font-display: 'Archivo', 'Arial Narrow', Arial, sans-serif;
  --font-body: 'Archivo', system-ui, sans-serif;
  --font-mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  /* Spacing */
  --spacing-shell: 1440px;
  --spacing-shell-narrow: 1100px;
  --spacing-header: 60px;
  --radius-card: 10px;
  --radius-sm: 6px;
  --radius-lg: 16px;

  /* Motion */
  --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --ease-snap: cubic-bezier(0.34, 1.56, 0.64, 1);
  --dur-fast: 150ms;
  --dur-mid: 300ms;
  --dur-slow: 600ms;
  --dur-crawl: 900ms;
}
```

---

## 8. Section components + hooks inventory

Each component emits the exact `data-*` hooks the QA harness and tests target. These mirror DESIGN_SYSTEM_V2 §5 but are the Astro-built DOM.

### 8.1 Header (`Header.astro`)

```html
<header class="site-header" data-header>
  <a class="brand" href="/">WEFT</a>
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

Hooks: `[data-header]`, `.nav-toggle`, `#mobile-nav`, `.nav-links`.

### 8.2 Hero (`HeroWebGL.astro` + `SceneCanvas.tsx`)

```html
<section class="hero" id="hero" aria-label="Weft — live product preview">
  <div class="hero-copy">
    <p class="kicker reveal">Agent coordination · evidence-gated</p>
    <h1 class="headline reveal" aria-label="One link. Many agents. All governed.">
      One link.<span class="accent">Many agents.</span><span class="accent-2">All governed.</span>
    </h1>
    <p class="lede reveal">Open one room, share it with every agent on the task, and they all communicate — with ordered delivery, scoped consent, and an evidence gate that refuses stale or out-of-scope work. Play it live below.</p>
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
    <div class="canvas-fallback" data-agent-fallback aria-hidden="false">
      <ol class="evt-fallback" data-agent-events-fallback>
        <!-- 5 seed events rendered server-side -->
      </ol>
    </div>
    <div class="canvas-readout" role="status" aria-live="polite" data-agent-readout>Ready</div>
  </div>
</section>
```

Hooks: `[data-agent-canvas]`, `[data-agent-canvas-wrap]`, `[data-agent-events-fallback]`, `[data-agent-readout]`, `[data-stat-strip]`, `[data-count-to]`.

### 8.3 Six-beat section hooks

| Beat | Hook | Element |
|---|---|---|
| All | `[data-beat-caption]` | caption below canvas updating per beat |
| Wide | `[data-beat="1"]` | marker (invisible, for testing) |
| Approach | `[data-beat="2"]` | marker |
| Inside | `[data-beat="3"]` | marker |
| The Gate | `[data-gate]` / `[data-gate-state]` | gate readout; state = "armed" → "REFUSED: stale fencing token" |
| Pull back | `[data-beat="5"]` | marker |
| Land | `[data-beat="6"]` | marker |

### 8.4 Other sections (hooks mirror DESIGN_SYSTEM_V2 §5)

| Section | Hooks |
|---|---|
| Problem | `#problem`, `.problem-card` |
| How it works | `#how`, `[data-steps]`, `[data-step]`, `.step-num` |
| Live demo | `[data-demo-shell]`, `[data-terminal]`, `[data-link-output]`, `[data-generated-link]`, `[data-copy]`, `[data-gate]`, `[data-gate-state]` |
| Connect | `[data-tier-tabs]`, `[data-tier-tab]`, `[data-tier-panel]`, `[data-code-block]`, `[data-copy]` |
| Proof | `[data-proof-list]`, `[data-proof-item]`, `.proof-stat`, `.proof-label` |
| Pricing | `#pricing`, `.pricing-card` |
| Footer | `[data-footer]` |
| Event log | `[data-agent-events]`, `.evt`, `[data-evt-kind]`, `[data-evt-seq]` |
| Scroll progress | `[data-scroll-progress]` |

### 8.5 Seed events (rendered server-side for no-JS / fallback)

```
001  [join]    agent-a joined the room
002  [join]    agent-b joined the room
003  [assert]  agent-a asserted scope: handoff.txt
004  [prove]   agent-b claimed task (fencing 1812495659374878)
005  [refuse]  agent-a: stale_fencing_token (refused)
```

---

## 9. `test_site.py` reconciliation table

For EVERY assertion: KEEP (passes against built output) or REPLACE with exact new assertion + rationale.

| Test | Assertion | Verdict | New assertion / note |
|---|---|---|---|
| `test_landing_page_has_truthful_semantic_launch_surface` | exactly one `<h1>` | **KEEP** | Astro renders one `<h1>` in `HeroWebGL.astro`. |
| | `"One incident. Two agents. One account of what happened."` | **REPLACE** | `"One link. Many agents. All governed."` |
| | `"9 documented MCP paths"` | **KEEP** | In hero stat strip + proof list. |
| | `"1 verified Weft host integration"` | **KEEP** | In hero stat strip. |
| | `"next proof pair, Claude Code + Cursor"` | **KEEP** | In proof section note. |
| | `application/ld+json` | **KEEP** | JSON-LD retained in `RootLayout.astro`. |
| | `og:site_name = "Weft"` | **KEEP** | Retained. |
| | `twitter:image = "/assets/og-card.png"` | **KEEP** | Retained. |
| | `aria-live="polite"` | **KEEP** | On `[data-agent-readout]` + `[data-agent-events]`. |
| | `data-sim-label` + `"Simulated account · no credentials · no live session"` | **REPLACE** | `[data-agent-readout]` with seed text `"Ready · simulated demo · no credentials"`. Update assertion string. |
| | `"MCP is the tool protocol"` | **KEEP** | In problem/how-it-works copy. |
| | `"single-node"` | **KEEP** | In proof list + footer. |
| | `data-cohort-form` | **KEEP** | Retained (form in pricing section). |
| | `"Watch the 42-second proof"` | **REPLACE** | `"Connect an agent"` (CTA). Update assertion string. |
| | `href="/demo.html"` | **KEEP** | Demo page still exists (preserved). |
| | `"$500 deposit"` | **KEEP** | In pricing section. |
| | `assertNotIn "verified agent handoff layer"` | **KEEP** | Still absent. |
| | `assertNotIn "Weft A2A Standard"` | **KEEP** | Still absent. |
| | `assertNotIn "gpt-5.5"` | **KEEP** | Still absent. |
| | `assertNotIn "lorem ipsum"` | **KEEP** | Still absent. |
| `test_progressive_enhancement_and_gated_story_cta` | `documentElement.classList.add('js')` | **KEEP** | Retained (inline script in `RootLayout.astro`). |
| | `[hidden] { display: none !important; }` | **KEEP** | In `global.css`. |
| | `.reveal { opacity: 1; transform: none; }` | **KEEP** | In `global.css`. |
| | `.js .reveal` | **KEEP** | In `global.css`. |
| | `data-story-next[^>]*hidden` | **REPLACE** | `[data-agent-canvas]` exists AND `[data-agent-events-fallback]` has ≥5 `<li>` children. |
| | `data-recon-plane` | **REPLACE** | `[data-agent-canvas]`. |
| | `perspective: 1500px` | **REPLACE** | `aspect-ratio` on `[data-agent-canvas-wrap]`. |
| | `transform-style: preserve-3d` | **REPLACE** | `transform: translateZ(0)` on canvas (GPU layer). |
| `test_unposted_entries_never_rely_on_colour_alone` | `.keylist`/`.factlist` (≥11 items) | **REPLACE** | `[data-proof-list]` with `[data-proof-item]` (5 items) + `[data-steps]` with `[data-step]` (4 steps). Total 9 colour-coded rows. Update assertion to target `[data-proof-item]` + `[data-step]`, requiring `.proof-label` / `h3` text adjacent. Note: threshold drops from 11 to 9 — document this. |
| `test_static_launch_bundle_contains_guides_articles_and_social_asset` | all required files | **KEEP** | All preserved (see §3). |
| `test_static_internal_content_links_resolve_inside_site_bundle` | all internal links resolve | **KEEP** | New section anchors (`#problem`, `#how`, `#demo`, `#connect`, `#pricing`, `#proof`) exist. |
| `test_compatibility_page_keeps_documented_and_verified_distinct` | compatibility.html content | **KEEP** | Untouched (preserved). |
| `test_recorded_demo_is_redacted_and_grounded_in_a_real_run` | demo.html + transcript + build | **KEEP** | Untouched (preserved). |
| `test_server_mounts_self_contained_site_and_branded_404` | `200` on `/` | **KEEP** | Server serves index.html. |
| | `"One incident. Two agents."` in `/` body | **REPLACE** | `"One link. Many agents."` |
| | `200` on `/docs/compatibility.html` + `"Documented is not verified."` | **KEEP** | Untouched. |
| | `200` on `/demo.html` + `"A real coordinator run."` | **KEEP** | Untouched. |
| | manifest name = "Weft" | **KEEP** | Untouched. |
| | `200` on demo mp4 | **KEEP** | Untouched. |
| | og-card.png `Cache-Control` | **KEEP** | Server sets it. |
| | `404` + `"This path is not in the account."` | **KEEP** | 404 page + message retained. |
| | path traversal blocked | **KEEP** | Server-level. |
| `TestCountSyncTests` | count sync | **KEEP** | Auto-discovers live count. |

### 9.1 NEW test: truthfulness claim enforcement (REQUIRED by brief)

Add to `test_site.py` (in `LaunchSurfaceTests`):

```python
def test_marketing_site_does_not_claim_dependency_free_website(self) -> None:
    """The marketing site is built with a toolchain (Astro/R3F/npm). It must
    NOT claim to be 'no-build', 'dependency-free', or 'no CDN' as a website
    property. The dependency-free claim applies to the COORDINATOR only, and
    must be scoped as such."""
    html = (SITE / "index.html").read_text(encoding="utf-8")
    self.assertNotIn("no build step", html.lower())
    self.assertNotIn("no-build landing page", html.lower())
    self.assertNotIn("dependency-free website", html.lower())
    self.assertNotIn("dependency-free site", html.lower())
    # If a dependency-free claim appears, it must be scoped to the coordinator.
    if "dependency-free" in html.lower():
        self.assertIn("coordinator", html.lower())
```

This enforces the re-scoped claim: "the coordinator you run is dependency-free; the marketing site is a separately built static bundle."

---

## 10. `capture-site-qa.cjs` reconciliation

### 10.1 Old hook → new hook table

| Old hook | Verdict | New hook |
|---|---|---|
| `[data-scroll-story]` | REPLACE | `[data-agent-canvas-wrap]` |
| `.recon-sticky` | REPLACE | `.hero-canvas` |
| `.recon-track` | REPLACE | `[data-agent-events]` |
| `.ledger-ground` | REPLACE | `.hero` |
| `[data-recon-plane]` | REPLACE | `[data-agent-canvas]` |
| `.entry-state` | REPLACE | `[data-evt-kind]` |
| `.desktop-nav` / `#mobile-nav` / `.nav-toggle` | KEEP | same |
| `[data-copy-target]` | REPLACE | `[data-copy]` |
| `[data-cohort-form]` | KEEP | same |
| `--split` | REPLACE | `--shell` |
| `data-demo-events`, `data-demo-session`, `data-demo-action` | REPLACE | `[data-agent-events]`, `[data-agent-readout]`, `[data-gate]` |
| `.folio-label` | REMOVE | no folio concept |
| `[data-story-next]` | REPLACE | `[data-agent-canvas]` |
| `.type-wipe` | REMOVE | no type-wipe |
| `[data-scroll-viewport]` | REPLACE | `[data-agent-canvas-wrap]` |
| `.recon-body` | REPLACE | `.hero-copy` |
| `.recon-sticky` (reduced-motion check) | REPLACE | `.hero-canvas` |
| `[data-demo-action="reset"/"create"/"next"]` | REMOVE | no step-advance model |

### 10.2 New invariant list for the QA harness

The updated `capture-site-qa.cjs` must assert:

1. **Zero console errors** — unchanged.
2. **Zero failed requests** — unchanged.
3. **Zero bad responses** (origin, status ≥400) — unchanged.
4. **axe-clean** (wcag2a/2aa/21a/21aa) — unchanged.
5. **Mobile no-overflow** — unchanged (check `[data-agent-canvas-wrap]` doesn't overflow).
6. **Progressive enhancement:** `.reveal` base visible, `.js .reveal` gated, `[hidden]` rule — unchanged.
7. **Reduced motion:** `prefers-reduced-motion` → reveals visible, canvas frozen (not blank) — NEW check: `[data-agent-canvas]` has painted content (non-zero pixels) under reduced motion.
8. **No-JS:** headline visible (`h1` height > 0), canvas fallback has ≥5 events (`[data-agent-events-fallback]` children ≥ 5), no `js` class.
9. **Form labels:** all inputs in `[data-cohort-form]` have associated labels — unchanged.
10. **Focus styles:** visible focus-visible on all focusable elements — unchanged.
11. **Landmarks:** one `main`, one `nav`, one `header`, one `footer`, one `h1` — unchanged.
12. **Skip link:** present — unchanged.
13. **Live regions:** `[aria-live]` present (event log + readout) — unchanged count, new hooks.
14. **Hero canvas mounts:** `[data-agent-canvas]` has non-zero dimensions within 2s — NEW.
15. **Agent graph animates:** at least 3 distinct canvas states captured over 2s — NEW (sample pixels or `window.__agentFrameCount` hook).
16. **Gate refusal fires:** `[data-gate-state]` reaches "REFUSED" within the demo loop — NEW.
17. **Link generated:** `[data-generated-link]` is non-empty and matches `weft.*/r/` pattern — NEW.
18. **Copy works:** clicking `[data-copy]` sets clipboard + visible status — NEW.
19. **Tier tabs switch:** clicking `[data-tier-tab]` shows the matching `[data-tier-panel]` — NEW.
20. **No third-party requests** — unchanged.
21. **Beat caption updates:** `[data-beat-caption]` text changes across scroll — NEW.

### 10.3 Server strategy for the harness

Per `workflow.md` §7.1, a Python driver MUST own an in-process server and shell out to node. **Decision:** Extend `scripts/weft-site.py` to add a `--qa-mode` flag OR write a new driver `scripts/run-site-qa.py`.

**Recommendation:** Write `scripts/run-site-qa.py` — a dedicated driver that:
1. Starts `QuietSiteHandler` on `("127.0.0.1", 0)` in a daemon thread.
2. Reads the assigned port.
3. Shells out to `node scripts/capture-site-qa.cjs` with `WEFT_SITE_URL=http://127.0.0.1:<port>/`.
4. Waits for node to exit.
5. In `finally`: `server.shutdown()` + `server.server_close()` + `thread.join(timeout=5)`.
6. Prints the QA result JSON and exits with node's exit code.

This keeps `weft-site.py` (the simple dev server) untouched and gives the QA harness a deterministic, self-cleaning driver. The driver uses `subprocess.run` (not `Popen` + pipe) to avoid the background-process trap.

---

## 11. Truthfulness claim audit

The marketing site now has a toolchain (Astro/R3F/npm). Claims that become FALSE once the site has a toolchain must be re-scoped.

| File:line | Current text | Problem | Replacement wording |
|---|---|---|---|
| `README.md:46` | "The repository includes a no-build landing page" | False — the landing page now builds via Astro. | "The repository includes a static marketing site (built with Astro from `web/`) and a dependency-free coordinator. The coordinator you run is Python stdlib-only, SQLite, no CDN — that promise is unchanged." |
| `docs/YC_APPLICATION.md:38` | "A no-build launch site" | False. | "A static marketing site (Astro-built) and a dependency-free coordinator. The coordinator is Python stdlib-only, SQLite, no CDN." |
| `docs/YC_READINESS.md:30` | "repository also contains a no-build landing page" | False. | "repository also contains a static marketing site (Astro-built from `web/`) and a dependency-free coordinator." |
| `docs/PRODUCT_ROADMAP.md:12` | "The repository promises 'dependency-free, Python stdlib only, SQLite, no CDN.'" | Ambiguous — the promise is on the COORDINATOR, not the whole repo. | "The COORDINATOR plane promises 'dependency-free, Python stdlib only, SQLite, no CDN.' The marketing site is a separately built static bundle (Astro + R3F)." |
| `docs/WEBAPP_DESIGN.md:12` | "the project's 'dependency-free' promise is a product claim on the website, a framework would violate it" | The webapp's stdlib choice must be re-justified on its own merits. | "stdlib `http.server` is sufficient for a surface that is pure request→HTML→response with no streaming-SSE requirement (event polling is client-side JS), so no framework dependency is honestly required." |

**Add the test assertion** from §9.1 to enforce this.

---

## 12. Verification plan

### 12.1 Build

```powershell
cd C:\Users\Wasif\Documents\Multiplayer-AI-isolated\web
npm ci           # installs from package-lock.json
npm run build    # astro build + verify-preservation.cjs
```

Expected: `site/` contains the Astro-built `index.html` + `assets/` + preserved `blog/`, `docs/`, `demo.*`, `404.html`, `license.html`, `llms.txt`, `robots.txt`, `site.webmanifest`, `assets/`.

### 12.2 Unit tests

```powershell
cd C:\Users\Wasif\Documents\Multiplayer-AI-isolated
python -B -m unittest discover -s tests
```

Expected: all pass. `TestCountSyncTests` auto-discovers the live count. If any assertion was added/removed, the guard tells you what to update.

### 12.3 Smoke test

```powershell
python -B scripts/weft-smoke.py
```

Expected: `evidence_passed: true`.

### 12.4 QA harness (screenshots + signals)

```powershell
python -B scripts/run-site-qa.py
```

This driver starts the in-process server, shells out to `node scripts/capture-site-qa.cjs`, and prints the result. Expected: `consoleErrors: []`, all booleans true, `totalAxeViolations: 0`, screenshots at 1440x900 and 390x844 / 390x667 / 320x568.

### 12.5 Lighthouse

**Recommendation:** Run real Lighthouse via `npx lighthouse` using the installed Playwright chromium as `--chrome-path`. Command:

```powershell
npx lighthouse http://127.0.0.1:4175/ `
  --chrome-path="C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/.local-browsers/chrome-win32/chrome.exe" `
  --port=9222 `
  --output=json --output-path=artifacts/design-qa/lighthouse-desktop.json `
  --only-categories=performance,accessibility,best-practices,seo `
  --screen-emulation.disabled `
  --throttling.cpuSlowdownMultiplier=1
```

Run at both 1440x900 (desktop) and 390x844 (mobile, via `--screen-emulation.mobile`). Target: ≥95 all categories.

**Alternative:** `@lhci/cli` as a devDep — but this requires a config and a server. The `npx lighthouse` route is simpler and uses the already-installed chromium.

### 12.6 Frame-time measurement

The scene exposes `window.__weftFrameTimes = []` — a ring buffer of the last 120 RAF timestamps (delta ms). The QA harness reads this after 5 seconds of scrolling and computes the median + p95. Expected: median < 4ms, p95 < 16ms (scroll loop budget).

The harness adds a check:
```js
const ft = window.__weftFrameTimes || [];
const median = ft.sort()[Math.floor(ft.length / 2)];
// assert median < 4, p95 < 16
```

---

## 13. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Astro `outDir` wipes preserved files | HIGH | `preserve-legacy.cjs` + `verify-preservation.cjs` with non-zero exit on failure. |
| R3F + React 19 peer mismatch | MEDIUM | Pin `r3f@9.7.0` (React 19 compatible) + `@astrojs/react@4.2.0`. |
| GSAP ScrollTrigger + Lenis loop conflict | MEDIUM | Use the canonical `gsap.ticker.add((t) => lenis.raf(t*1000))` pattern. |
| Font self-hosting fails (CORS / path) | LOW | Fonts in `web/public/fonts/` → copied to `site/fonts/` → served same-origin. |
| WebGL fallback looks broken on low-end | MEDIUM | Static poster + 5-seed-event fallback is always rendered. |
| Test count sync drifts | LOW | `TestCountSyncTests` auto-discovers; no hand-maintenance. |
| `capture-site-qa.cjs` Playwright path missing | LOW | `WEFT_PLAYWRIGHT` env var + fallback to installed path. |
| Astro 5 vs 7 engine requirement | LOW | Node 24 satisfies both; we pin Astro 5. |
| Truthfulness claim audit incomplete | MEDIUM | The new test (`test_marketing_site_does_not_claim_dependency_free_website`) enforces this automatically. |

---

## 14. Open questions

1. **Astro version:** Pin Astro 5.10.0 (verify at install). If Astro 5 latest is higher, use the latest 5.x. Confirm Node 24 satisfies (it does — Astro 5 needs ≥18).
2. **R3F + React 19:** Confirm `r3f@9.7.0` peer-depends on React 19. If not, pin to the version that does.
3. **`@react-three/postprocessing` version:** Not pinned above — verify compatibility with R3F 9 + three 0.170 at install.
4. **Lighthouse chromium path:** The path `C:/Users/Wasif/.cache/codex-runtimes/...` may differ. Verify with `ls` before running.
5. **Beat caption UX:** The `[data-beat-caption]` element is a new hook — confirm with the WebGL lane that it's the agreed bridge for beat → DOM.
6. **`data-cohort-form` location:** Currently in the page; the build plan places it in the pricing section. Confirm the form's submit handler (clipboard copy) is preserved.

---

*End of SITE_BUILD_PLAN.md — Wave D3 BUILD-PLAN deliverable.*
