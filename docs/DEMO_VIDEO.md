# Finalisma launch-demo video

The repository now contains a reproducible 42-second narrative in 43.04-second, 1280 x 720 launch files:

- `site/assets/finalisma-demo.mp4` — Product Hunt/upload-friendly H.264 MP4
- `site/assets/finalisma-demo.webm` — browser-native Playwright recording
- `site/assets/finalisma-demo-poster.png` — 1280 x 720 poster
- `site/assets/finalisma-demo.vtt` — English WebVTT captions
- `site/assets/demo-transcript.json` — machine-readable, credential-redacted proof record
- `site/demo.html` — public watch page with transcript and cohort CTAs

## What is real

The video is generated from a fresh local `FinalismaStore` protocol run. SQLite state, agent identities, hashed-at-rest credentials, one-use pairing, consent, ordered session relay, acknowledgement cursor, scoped task, atomic claim, lease, fencing token, artifact hash, secret scan, evidence gate, completion, and audit events all run through the real implementation.

The two agent hosts are deterministic fixtures. The recording is not evidence that Codex, Claude Code, or another host pair has completed an end-to-end Finalisma integration. Every frame says that credentials are redacted and hosts are simulated.

## Regenerate it

Start the local site server:

```powershell
python -B .\scripts\finalisma-site.py --port 4175
```

Generate a fresh redacted transcript, then record and encode the video:

```powershell
python -B .\scripts\generate-demo-transcript.py
$env:FINALISMA_SITE_URL = "http://127.0.0.1:4175/"
node .\scripts\record-demo-video.cjs
```

The recording script uses the existing Playwright browser runtime. It records WebM directly and looks for an existing H.264-capable ffmpeg before producing MP4. It does not install packages, edit PATH, start a service, or download a model.

## Storyboard

| Time | Entry | Public claim |
| --- | --- | --- |
| 00-04s | The handoff problem | Copy-paste loses a governed account. |
| 04-08s | Identity | The host keeps its provider credential. |
| 08-13s | Pairing | A one-use link offers read and comment. |
| 13-17s | Consent | The invitee previews policy before joining. |
| 17-21s | Ordered relay | Sequence 1 is received and acknowledged. |
| 21-25s | Scope and ownership | One artifact, lease owner, and fencing token. |
| 25-29s | Work | The deterministic reviewer posts a bounded finding. |
| 29-34s | Evidence gate | Artifact hash, secret scan, and check pass. |
| 34-38s | Reconciled | The task closes with eleven audit events. |
| 38-42s | Finalisma | Run the local proof; host validation remains open. |

## Audio and captions

The video intentionally has no voice track. Every message is composed into the frame for muted autoplay, and the player also loads `finalisma-demo.vtt`. A founder voiceover can be added later without changing the factual edit; do not publish synthetic narration that obscures the host-simulation boundary.

## Release verification

`artifacts/design-qa/demo-video-results.json` records the generated sizes, resolution, encoder, caption status, and redaction boundary. Browser QA loads the public player, checks both sources and the caption track, reads video metadata, and captures the watch page.
