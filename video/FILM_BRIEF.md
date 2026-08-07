# FILM BRIEF — 30s product film

Build this in **HyperFrames** (this directory). Bar: Apple product film / YC demo-day opener.
The storyboard below is **already validated** — it was built and rendered frame-by-frame as an
animatic first. Do not redesign the story. Execute it at higher fidelity than the animatic.

---

## 0. READ THIS FIRST (opencode-specific)

`AGENTS.md` in this folder references Claude Code slash-skills (`/hyperframes`, `/hyperframes-core`…).
**You do not have those.** Use the CLI docs instead — same content, no skill system required:

```bash
npx hyperframes docs data-attributes
npx hyperframes docs gsap
npx hyperframes docs compositions
npx hyperframes docs examples
npx hyperframes docs troubleshooting
```

Everything else in `AGENTS.md` (commands, contract, pinned version) **does** apply.

**ffmpeg is not on the system PATH.** Prefix every render/check command:

```bash
export PATH="/c/Users/Wasif/.local/bin:$PATH"
```

---

## 1. THE STORY

A person cannot get one AI assistant to talk to another. That is the whole problem.
We do not explain it. We show someone hit the wall, then we open the door.

| Act | Time | What happens |
|---|---|---|
| **1 — THE WALL** | 0.0–7.0 | Tight on a chat window. Someone types `ask claude to review my PR`. Send. Reply: **"I can't message another assistant."** The caret blinks. Nothing happens. Camera pulls back — there *is* another window, far away in the dark. Two rooms, no door. |
| **2 — THE LINK** | 7.0–14.5 | A link appears in the gap between them. It goes into both. The windows turn toward it. A room opens *between* them. Both agents join; three more follow. The same request goes again — this time it travels, and it **arrives**. |
| **3 — THE GATE** | 14.5–22.5 | Something outside the room tries to act. The boundary hardens red. **REFUSED** — `stale fencing token · outside declared scope`. Hold it. This is the moat. |
| **4 — THE RECORD** | 22.5–27.0 | Camera cranes down to the event log. Every action in order — including the refusal. |
| **5 — LAND** | 27.0–30.0 | Push into the link. Wordmark. One line. |

### Captions (screen-space, one at a time, lower third)

- 1.6–4.6 — *(none — let the window carry it; the denial IS the copy)*
- 8.4–11.2 — **One link. One room.**
- 12.4–16.0 — **Everything they say arrives in order.**
- 22.5–24.9 — **Forwarding is easy. Refusing is the product.**
- 25.9–27.6 — **Every action, recorded in order.**
- 28.8–30.0 — wordmark **FINALISMA** + `One link. Every agent. On the record.`

---

## 2. SPATIAL MAP

One continuous world. **Nothing cross-fades between scenes** — the camera moves through
space and reveals the next thing. This was the single biggest note from the founder:
*"not this fade in/fade out to the next scene."*

World coordinates (the camera frames regions of this space):

```
            x=170        x=660      x=960      x=1260      x=1750
              |            |          |          |           |
   y=258   ┌──────────┐                            ┌──────────┐
           │ WINDOW A │        ← gap →             │ WINDOW B │
           │ chatgpt  │                            │  claude  │
   y=688   └──────────┘                            └──────────┘
                          ╭────────────────╮
   y=470                  │   THE ROOM     │   centre (960,470), ring r=300
                          │  ◦ link pill   │   5 agent chips ON the ring at
                          ╰────────────────╯   -90°, -18°, 54°, 126°, 198°

   y=470   ◦ outside agent enters from x≈250, stopped at ring edge x=660

   y=1150              ┌──────────────────┐
                       │    EVENT LOG     │   ~1044 x 464, centred (960,1150)
                       └──────────────────┘
```

The room forms **in the gap between the two windows**. That is the point: the product is
literally the thing that goes in the space between two agents that cannot reach each other.

### Camera path (validated — port these values)

Camera = scale the world about its centre, then translate so the target point lands centre-screen.
`translate = ( -(tx-960)*s , -(ty-540)*s )`

| t | target | zoom | note |
|---|---|---|---|
| 0.00 | 400, 470 | 2.30 | tight on window A's input line |
| 3.20 | 405, 468 | 2.24 | barely drifts — the shot breathes while they type |
| 5.00 | 410, 470 | 2.15 | denial lands |
| 8.60 | 960, 470 | 0.86 | **PULL BACK** — both windows, the gap between them |
| 10.40 | 960, 470 | 1.00 | link appears in the gap; settle |
| 13.00 | 966, 466 | 1.06 | traffic |
| 15.60 | 972, 464 | 1.14 | slow push |
| 17.40 | 830, 470 | 1.06 | pan left — the outsider enters frame |
| 18.28 | 900, 470 | 1.32 | **PUNCH** on impact (ease-out, ~0.22s) |
| 18.40 / 18.52 | ±8px jitter | 1.33 / 1.31 | two-frame shake, then settle |
| 21.40 | 946, 474 | 1.56 | keeps creeping in through the verdict — never static |
| 23.20 | 960, 470 | 0.95 | release |
| 25.30 | 960, 880 | 0.99 | crane down |
| 26.90 | 960, 1150 | 1.02 | log fills frame |
| 28.40 | 960, 800 | 0.60 | pull wide — whole system at once |
| 30.00 | 960, 470 | 2.30 | push into the link |

Easing: use a custom cubic `cubic-bezier(0.32, 0.72, 0, 1)` for every large move
(GSAP: `CustomEase` or `power3.out` as the closest stock equivalent). `linear` only for holds
and the two shake frames. Never `ease-in-out` defaults.

---

## 3. ART DIRECTION — Ethereal Glass

| Token | Value |
|---|---|
| background | `#050505` |
| plate (window/log interior) | `#0A0A0C` |
| hairline | `rgba(255,255,255,0.11)` |
| text primary | `rgba(255,255,255,0.97)` |
| text muted | `rgba(255,255,255,0.45)` |
| violet (room glow) | `#7C5CFF` |
| cyan (messages) | `#46E0FF` |
| green (presence / ok) | `#38E8A0` |
| red (refusal / outsider) | `#FF4D4D` |

- **Double-bezel** on every panel: outer shell `rgba(255,255,255,0.05)` + 1px hairline,
  `padding: 7px`, `border-radius: 32px`; inner plate its own darker fill, inner highlight
  `inset 0 1px 1px rgba(255,255,255,0.15)`, concentric radius `25px`.
- Type: **Archivo SemiBold** for display/captions (tracking `-2.5%`), **Geist** for UI,
  **IBM Plex Mono** for the link, log rows and the refusal reason. Self-host the woff2 in
  `assets/fonts/` — do NOT use a CDN, the renderer must be deterministic and offline-safe.
  **Never Inter/Roboto/Arial/Helvetica.**
- Atmosphere: two large low-opacity radial gradients (violet, indigo) drifting slowly against
  the camera for parallax. A vignette **under** the caption layer — in the animatic the vignette
  sat on top and greyed out every caption. Do not repeat that.

### Where HyperFrames must beat the animatic

The Figma animatic could not do these. You can, and should:

1. **Real character-by-character typing** on `ask claude to review my PR` (the animatic faked it
   with a sliding mask). Blinking caret, then it stops when the denial appears.
2. **Motion blur / streak on the message dots** so traffic reads as motion, not as beads.
3. **Fan the broadcast** — in the animatic all four broadcast dots left the sender on the same
   vector and overlapped into a single vertical column. Give each its own arc.
4. **Glow bloom** on the ring at the moment of refusal (CSS `filter: drop-shadow` stack or a
   blurred duplicate layer).
5. **Depth**: slight scale/opacity offset between window layer, room layer, and atmosphere so
   the pull-back has parallax, not flat zoom.

---

## 4. TRUTH RULES — non-negotiable

- **No invented customers, logos, testimonials, endorsements or metrics.**
- The two window titles (`chatgpt`, `claude`) are **lowercase plain text, no logos, no brand
  marks, no colours borrowed from those products.** They are there because the audience needs to
  recognise the problem. Nothing may imply partnership, endorsement, or that either vendor has
  integrated with us. If in doubt, make them plainer, never fancier.
- The refusal reason `stale fencing token · outside declared scope` must stay literally true to what the
  evidence gate actually checks. Do not embellish it.
- The log rows describe a plausible session — keep them mechanical and unremarkable. No
  "10,000 agents", no fake scale.

---

## 5. BUILD ORDER & VERIFICATION

Work in small steps and run `check` after each. Do not build the whole thing then debug it.

1. Scaffold the world + camera rig, no content. Prove the camera path alone renders correctly.
2. Window A + typing + denial. `check`, render, sample frames at 1s/3s/5s.
3. Pull-back reveal + window B + link + room + chips docking.
4. Traffic (unicast → group → broadcast).
5. Gate + refusal.
6. Log + crane down.
7. Lockup + final push.

```bash
export PATH="/c/Users/Wasif/.local/bin:$PATH"
cd video
npm run check          # lint + runtime + layout + motion + contrast — must be clean
npm run render         # -> renders/final.mp4 (stable path, gitignored)
```

**Verify by sampling frames, not by assuming.** After each render:

```bash
ffmpeg -ss <t> -i renders/final.mp4 -frames:v 1 frame_<t>.png
```

Sample at least: `0.5, 3.0, 5.5, 8.6, 10.5, 13.0, 17.4, 18.3, 19.5, 23.2, 26.9, 28.4, 29.9`.
Look for: text clipped or overflowing, elements off-screen at the zoom extremes, the caption
being greyed by an overlay, dots overlapping into a column, anything static for >1.5s.

**`npm run dev` is a long-running server.** Run it in the background, never in the foreground.

### Definition of done

- 30.0s exactly, 1920×1080, MP4.
- `npm run check` clean.
- Nothing on screen is motionless for more than ~1.5s.
- No scene change is a cross-fade — every transition is a camera move or an in-place transform.
- A first-time viewer who has never heard of MCP understands, in one watch: two assistants
  couldn't talk → a link put them in one room → the room can refuse bad actions → everything is
  on the record.

---

## 6. REFERENCE

The validated animatic (Figma, story + camera timing only — **not** the visual target):
`https://www.figma.com/design/qUbHp8uCv5RjzKU95u13zZ`

Existing art direction and copy rules: `docs/AWARD_REFERENCE_2026.md`, `docs/DESIGN_SYSTEM_V2.md`.
