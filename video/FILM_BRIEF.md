# FILM BRIEF — 30s product film

Build this in **HyperFrames** (this directory). Bar: Apple product film / YC demo-day opener.
The storyboard below is **already validated** — it was built and rendered frame-by-frame as an
animatic first. Do not redesign the story. Execute it at higher fidelity than the animatic.

---

> **Revision 2026-08-15** — founder directive: the film is re-cut from one continuous camera move into **7 hard-cut scenes with constant motion** (*"apple videos are always moving all over the place"*). Story beats unchanged; only the cutting changed.

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

| S | Time | Camera (cut → move) | What happens | Transition | Caption |
|---|---|---|---|---|---|
| **1 — THE WALL** | 0.0–4.0 | Cut macro on Window A's input line at 400,470 @ 2.60, creep-in to 416,470 @ 2.74 | The prompt `ask claude to review my PR` **TYPES** char-by-char (~1.4s), the caret **BLINKS**; send **POPS**; thinking dots **PULSE**; the denial *"I can't message another assistant."* **LANDS** at 3.05 with a **SHAKE**. | — (opening shot) | *(none — let the window carry it; the denial IS the copy)* |
| **2 — THE LINK** | 4.0–7.0 | **HARD CUT** wide: 960,470 @ 0.90 → push to 1.05 | Both windows in frame, the gap between them. The link pill **POPS** at 4.6 in the gap; the reply **LANDS** in Window B at 5.2. | HARD CUT | **One link. One room.** — 4.7–6.3 |
| **3 — THE ROOM** | 7.0–11.0 | **HARD CUT** to the room: 960,470 @ 1.25 → push to 1.32 | The ring **ASSEMBLES** with a back.out pop at 7.1; the sheen **BLOOMS** at 7.3; five agent chips **DOCK** onto the ring 7.7–8.5, then **PULSE** 9.4–10.4. | HARD CUT | **Everything they say arrives in order.** — 8.8–10.6 |
| **4 — TRAFFIC** | 11.0–14.5 | Three **HARD CUTS inside the room** — 11.0 @ 1.50 / 12.2 @ 1.35 / 13.2 @ 1.35, each with a 1s push | Three traffic waves: chips→ring delivery 11.05; ring→chips green **ACKS** 12.25; second delivery 13.25. Every dot flies a two-segment arc with a motion **STREAK**. | HARD CUT ×3 (interior) | *(none)* |
| **5 — THE GATE** | 14.5–19.0 | **HARD CUT** to the gate: 830,470 @ 1.06 → creep push to 946,474 @ 1.56 | The outsider **DARTS** in over 0.9s (power2.in) after 120ms anticipation; the ring **SLAMS** red at 15.5 with overshoot + settle; the **REFUSED** stamp **POPS** at 15.7; red glow **PULSES** on the ring; the reason line `recipient_not_found · not a member of this room` **TYPES** itself 15.9–16.8. | HARD CUT | **Forwarding is easy. Refusing is the product.** — 16.0–18.4 |
| **6 — THE RECORD** | 19.0–23.5 | **HARD CUT** to the event log: 960,1150 @ 1.05 → push to 1.10 | The log shell **POPS** at 19.1; 8 rows **CASCADE** slide-in from 19.3 (0.12 stagger) — every action in order, including the refusal. | HARD CUT | **Every action, recorded in order.** — 19.6–21.6 |
| **7 — LAND** | 23.5–30.0 | Drift on the record (965,1145 @ 1.12 → 972,1152 @ 1.15), then a fast **WHIP-OUT** to the wide system view at 26.85 (960,800 @ 0.60) | Windows + room + log at once. The global camera breathing **ZEROES** 26.5–26.9; the windows **FADE** at 26.9; the final **DIVE** into the link runs 28.42–30.0 and the wordmark lockup **BLOOMS** from the chip's exact position. | WHIP-OUT + HARD CUT | wordmark **WEFT** + `One link. Every agent. On the record.` at the lockup |

### Rhythm

The founder's rule: *"apple videos are always moving all over the place."* Every scene keeps moving — **no frame is ever frozen** — and a global camera breathing (layered sine drift on every frame from t=0) runs under all seven scenes, zeroed only for the landing. The pattern: **hold–fast–fast–RAPID-CUT–SLAM–scan–LAND**.

- **S1 hold-fast-fast** — macro creep-in, typing, send, denial shake. Small, tense motion.
- **S2 fast** — wide push while the link pops; the frame never rests.
- **S3 fast** — assembly in ascending beats; the chip pulse builds momentum.
- **S4 RAPID-CUT** — three cuts inside 3.5 seconds, one traffic wave per angle; the fastest cutting in the film.
- **S5 SLAM** — the refusal hits at 15.5s, **the energy peak of the film**; then the reason line types slowly and lets the moment breathe.
- **S6 scan** — steady push, rows cascading on a fixed stagger.
- **S7 LAND** — wide and calm; breathing zeroes out, the windows fade, then the dive at 28.42s — **the second peak** — into the lockup.

### SFX cues (design-only — the render is still silent)

No audio exists in the render yet; these are cues for the future sound pass.

- **S1** soft key clicks per character, ending in a send POP; a low thock on the denial shake at 3.05.
- **S2** a quiet POP for the link pill at 4.6; a soft "delivered" blip when the reply lands at 5.2.
- **S3** ascending DOCK ticks, one per chip, 7.7–8.5; a shimmer sweep on the sheen bloom at 7.3.
- **S4** a short whoosh per wave (11.05 / 12.25 / 13.25), pitched up on the green acks.
- **S5** a low impact THUD when the ring slams red at 15.5; a mechanical STAMP at 15.7; faint typewriter ticks for the reason line 15.9–16.8.
- **S6** a soft data CHIME cascade matching the 0.12 row stagger.
- **S7** a rising pad under the dive, cutting to silence at the lockup.

---

## 2. SPATIAL MAP

One continuous world, cut into **seven hard-cut scenes** — no cross-fades, no dissolves.
Each scene change is an instant cut to a new camera position and scale, and the camera keeps
moving inside every scene. The founder's directive (2026-08-15): *"apple videos are always
moving all over the place."* So no frame is ever frozen: every scene has its own push or creep,
and a global camera breathing (layered sine drift) runs on every frame from t=0.

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

### Camera cuts (validated — port these values)

Camera = scale the world about its centre, then translate so the target point lands centre-screen.
`translate = ( -(tx-960)*s , -(ty-540)*s )`

| Scene | Cut at t | Target | Zoom | Intra-scene move |
|---|---|---|---|---|
| S1 | 0.00 | 400, 470 | 2.60 | creep-in to 416, 470 @ 2.74 |
| S2 | 4.00 | 960, 470 | 0.90 | push to 1.05 |
| S3 | 7.00 | 960, 470 | 1.25 | push to 1.32 |
| S4 | 11.00 | 960, 470 | 1.50 | 1s push; re-cut 12.2 at 880,470 @ 1.35 + 1s push; re-cut 13.2 at 1040,470 @ 1.35 + 1s push |
| S5 | 14.50 | 830, 470 | 1.06 | creep push to 946, 474 @ 1.56 — never static through the verdict |
| S6 | 19.00 | 960, 1150 | 1.05 | push to 1.10 |
| S7 | 23.00 | drift 965,1145 @ 1.12 → 972,1152 @ 1.15, then 26.85 whip to 960,800 @ 0.60 (power2.in) | breathing zeroed 26.5–26.9; windows fade 26.9; dive into the link 28.42 → 30.00 at 960, 176 @ 2.30 |

Easing: use a custom cubic `cubic-bezier(0.32, 0.72, 0, 1)` for every large move
(GSAP: `CustomEase` or `power3.out` as the closest stock equivalent). `linear` only for holds
and the two shake frames. Never `ease-in-out` defaults. The outsider dart uses `power2.in`
(0.9s) after a 120ms anticipation; the ring slam is an overshoot + settle
(`back.out(2.5)`, then the house ease).

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
   the wide scenes have parallax, not flat zoom.

---

## 4. TRUTH RULES — non-negotiable

- **No invented customers, logos, testimonials, endorsements or metrics.**
- The two window titles (`chatgpt`, `claude`) are **lowercase plain text, no logos, no brand
  marks, no colours borrowed from those products.** They are there because the audience needs to
  recognise the problem. Nothing may imply partnership, endorsement, or that either vendor has
  integrated with us. If in doubt, make them plainer, never fancier.
- The refusal reason must stay literally true to what the evidence gate actually checks: `recipient_not_found � not a member of this room`. Do not embellish it.

- The log rows describe a plausible session — keep them mechanical and unremarkable. No
  "10,000 agents", no fake scale.

---

## 5. BUILD ORDER & VERIFICATION

Work in small steps and run `check` after each. Do not build the whole thing then debug it.

1. Scaffold the world + camera rig, no content. Prove the seven hard cuts + camera breathing render correctly.
2. S1: Window A + typing + denial + shake. `check`, render, sample frames at 1s/3s.
3. S2: wide cut + link pill + reply in Window B + caption.
4. S3: room assembly + chips docking + pulse + caption.
5. S4: three traffic waves with the three interior cuts + motion streaks.
6. S5: gate + refusal slam + stamp + reason typing + caption.
7. S6: log + row cascade + caption.
8. S7: wide + breathing zero + windows fade + dive + lockup.

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

Sample at least: `0.5, 3.0, 4.6, 5.2, 7.1, 9.4, 11.05, 12.25, 13.25, 15.5, 16.0, 19.1, 19.6, 26.9, 28.42, 29.9`.
Look for: text clipped or overflowing, elements off-screen at the zoom extremes, the caption
being greyed by an overlay, dots overlapping into a column, each HARD CUT landing exactly on its
keyframe, anything static for >1.5s.

**`npm run dev` is a long-running server.** Run it in the background, never in the foreground.

### Definition of done

- 30.0s exactly, 1920×1080, MP4.
- `npm run check` clean.
- Nothing on screen is motionless for more than ~1.5s.
- No scene change is a cross-fade — every transition is a HARD CUT, and the camera is always
  moving (no frame ever frozen).
- A first-time viewer who has never heard of MCP understands, in one watch: two assistants
  couldn't talk → a link put them in one room → the room can refuse bad actions → everything is
  on the record.

---

## 6. REFERENCE

The validated animatic (Figma, story + camera timing only — **not** the visual target):
`https://www.figma.com/design/qUbHp8uCv5RjzKU95u13zZ`

Existing art direction and copy rules: `docs/AWARD_REFERENCE_2026.md`, `docs/DESIGN_SYSTEM_V2.md`.
