# DEP_PINNING.md — Wave D3 scaffold dependency notes

## Version changes from plan §1/§2.2

| Package | Plan pin | Actual | Reason |
|---|---|---|---|
| `typescript` | `5.7.0` | **`5.7.3`** | `typescript@5.7.0` does not exist in the npm registry (404). `5.7.3` is the latest available `5.7.x` patch. No API-relevant difference for this scaffold. |

## All other packages — installed exactly as pinned

| Package | Installed |
|---|---|
| `astro` | `5.10.0` |
| `@astrojs/react` | `4.2.0` |
| `react` | `19.0.0` |
| `react-dom` | `19.0.0` |
| `three` | `0.170.0` |
| `@react-three/fiber` | `9.7.0` |
| `@react-three/drei` | `10.0.0` |
| `gsap` | `3.15.0` |
| `lenis` | `1.3.26` |
| `motion` | `13.0.0` |
| `@tailwindcss/vite` | `4.3.3` |
| `tailwindcss` | `4.3.3` |
| `@types/react` | `19.0.0` |
| `@types/react-dom` | `19.0.0` |
| `@types/three` | `0.170.0` |

## Peer-dependency verification (pre-install)

- `astro@5.10.0` engines: `node 18.20.8 || ^20.3.0 || >=22.0.0` — satisfied by Node `v24.18.0`.
- `@astrojs/react@4.2.0` peers: `react ^17||^18||^19`, `react-dom ^17||^18||^19`, `@types/react`, `@types/react-dom` — all satisfied. No `astro` peer (added via integration, not peer).
- `@react-three/fiber@9.7.0` peers: `react >=19 <19.3`, `three >=0.156` — satisfied by react `19.0.0` and three `0.170.0`.
- `@react-three/drei@10.0.0` peers: `react ^19`, `three >=0.159`, `@react-three/fiber ^9.0.0` — all satisfied.
- `@tailwindcss/vite@4.3.3` peers: `vite ^5.2||^6||^7||^8` — satisfied by astro's bundled vite `^6.3.4`.

No peer conflicts encountered at `npm install`. `npm ci` succeeds once the lockfile is generated.
