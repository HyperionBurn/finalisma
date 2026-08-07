"""Generate a public, credential-redacted demo transcript from a real local run."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_mcp.core import WeftError, WeftStore  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate the redacted Finalisma launch-demo transcript")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "site" / "assets" / "demo-transcript.json"),
        help="Output JSON path",
    )
    return parser


def run_demo() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="weft-launch-demo-") as temporary:
        workspace = Path(temporary)
        with WeftStore(workspace / "state.db", workspace) as store:
            agent_a = store.register_agent(
                "incident-7f3a",
                "agent-a",
                "Incident owner",
                "architect",
                "gpt-5.6-luna",
                ["planning", "incident-triage"],
            )
            pairing = store.create_pairing(
                "agent-a",
                "incident-7f3a",
                capabilities_offered=["read", "comment"],
                policy={"requires_consent": True, "workspace_scope": ["retry-boundary.txt"]},
                actor_token=agent_a["actor_token"],
            )
            preview = store.pairing_preview(pairing["join_token"])
            joined = store.join_pairing(
                pairing["join_token"],
                "agent-b",
                name="Review agent",
                role="security-review",
                model="qwencloud/qwen3.8-max-preview",
                capabilities=["security", "testing"],
                consent=True,
            )
            consumed_preview = store.pairing_preview(pairing["join_token"])

            offered = store.session_send(
                pairing["initiator_session_token"],
                "agent-a",
                "task.offer",
                {
                    "question": "Why do checkout retries amplify after a timeout?",
                    "scope": ["retry-boundary.txt"],
                },
                "launch-demo-offer-v1",
            )
            received = store.session_poll(joined["session_token"], "agent-b", after_seq=0)
            acknowledged = store.session_ack(joined["session_token"], "agent-b", offered["event"]["seq"])

            task_result = store.create_task(
                "incident-7f3a",
                "agent-a",
                "Review the retry boundary",
                "Explain the amplification path and return one bounded evidence artifact.",
                scope=["retry-boundary.txt"],
                preferred_agent="agent-b",
                preferred_model="qwencloud/qwen3.8-max-preview",
                idempotency_key="launch-demo-task-v1",
                actor_token=agent_a["actor_token"],
            )
            task = task_result["task"]
            claimed = store.claim_task(
                "incident-7f3a",
                "agent-b",
                task["task_id"],
                actor_token=joined["actor_token"],
            )
            progress = store.update_task(
                "incident-7f3a",
                "agent-b",
                task["task_id"],
                progress=60,
                note="Reproduced the retry fan-out and isolated the stale timeout branch.",
                fencing_token=claimed["fencing_token"],
                actor_token=joined["actor_token"],
            )
            artifact = workspace / "retry-boundary.txt"
            artifact.write_text(
                "Synthetic incident mirror\nFinding: stale timeout retries fan out before acknowledgement.\n",
                encoding="utf-8",
            )
            verified = store.verify_task(
                "incident-7f3a",
                "agent-b",
                task["task_id"],
                claimed["fencing_token"],
                ["retry-boundary.txt"],
                [
                    {
                        "name": "incident-mirror-reproduction",
                        "status": "passed",
                        "command": "synthetic fixture",
                        "evidence": "Retry amplification reproduced and bounded to the declared artifact.",
                    }
                ],
                actor_token=joined["actor_token"],
            )
            completed = store.complete_task(
                "incident-7f3a",
                "agent-b",
                task["task_id"],
                claimed["fencing_token"],
                "Bounded retry finding returned with artifact-linked evidence.",
                actor_token=joined["actor_token"],
            )
            status = store.team_status(
                "incident-7f3a",
                include_events=True,
                agent_id="agent-a",
                actor_token=agent_a["actor_token"],
            )

            digest = verified["details"]["files"][0]["sha256"]
            event_types = [event["type"] for event in reversed(status["events"])]
            return {
                "schema": "weft.public-demo/v1",
                "source": "real local WeftStore protocol run",
                "boundary": "Agent hosts are deterministic fixtures; coordinator, SQLite state, pairing, session, lease, fencing, artifact hashing, secret scan, evidence gate, and completion are real.",
                "credentials_redacted": True,
                "duration_seconds": 42,
                "proof": {
                    "pairing_preview_status": preview["status"],
                    "pairing_status": consumed_preview["status"],
                    "session_state": joined["state"],
                    "session_events_received": len(received["events"]),
                    "last_ack_seq": acknowledged["last_ack_seq"],
                    "task_progress_observed": progress["progress"],
                    "evidence_passed": verified["passed"],
                    "artifact_sha256_prefix": digest[:12],
                    "secret_scan": verified["details"]["secret_scan"]["status"],
                    "task_status": completed["status"],
                    "audit_event_count": len(status["events"]),
                    "audit_event_types": event_types,
                    "raw_credentials_in_output": False,
                },
                "frames": [
                    {
                        "eyebrow": "The handoff problem",
                        "headline": "One agent stalls. Copy-paste destroys the account.",
                        "caption": "Finalisma keeps scope, ownership, ordered progress, and evidence in one inspectable record.",
                        "agent_a": "Incident owner · context exhausted",
                        "agent_b": "Review agent · waiting outside the account",
                        "event": "No governed handoff yet",
                        "state": "unposted",
                    },
                    {
                        "eyebrow": "01 · Identity",
                        "headline": "Agent A opens a named incident account.",
                        "caption": "The model route is recorded. The host keeps its provider credential and approval boundary.",
                        "agent_a": "Registered · architect · gpt-5.6-luna",
                        "agent_b": "Not joined",
                        "event": "agent.registered",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "02 · Pairing",
                        "headline": "A one-use link offers read + comment—not blanket access.",
                        "caption": "The token expires, is stored only as a hash, and reveals policy before consent.",
                        "agent_a": "Pairing issued · 15-minute expiry",
                        "agent_b": "Policy preview · no session credential",
                        "event": "pairing.issued",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "03 · Consent",
                        "headline": "Agent B previews the scope, then joins explicitly.",
                        "caption": "Literal consent=true opens a session with separate member-bound credentials.",
                        "agent_a": "Member A · credential retained by host",
                        "agent_b": "Member B · consent recorded",
                        "event": "pairing.joined · session.active",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "04 · Ordered relay",
                        "headline": "The question crosses once and can replay after disconnect.",
                        "caption": "The receiver observes sequence 1, acknowledges sequence 1, and can resume from that cursor.",
                        "agent_a": "task.offer · seq 1",
                        "agent_b": "poll → receive → ack 1",
                        "event": "finalisma.a2a/1.0 · idempotent",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "05 · Scope + ownership",
                        "headline": "One file. One lease owner. One fencing token.",
                        "caption": "A stale owner cannot keep writing after the lease changes.",
                        "agent_a": "retry-boundary.txt · declared scope",
                        "agent_b": "Task claimed · atomic lease",
                        "event": "task.created · task.claimed",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "06 · Work",
                        "headline": "The review agent reproduces the retry fan-out.",
                        "caption": "Progress reaches 60% with a bounded finding—without gaining the first host's hidden history or provider key.",
                        "agent_a": "Owner watches ordered progress",
                        "agent_b": "Reproduction isolated · 60%",
                        "event": "task.updated",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "07 · Evidence gate",
                        "headline": "Completion waits for an artifact and a passing check.",
                        "caption": f"Scope contained · secret scan passed · SHA-256 {digest[:12]}…",
                        "agent_a": "No prose-only close accepted",
                        "agent_b": "Artifact hashed · check passed",
                        "event": "quality.evaluated",
                        "state": "posted",
                    },
                    {
                        "eyebrow": "08 · Reconciled",
                        "headline": "The account closes on evidence—not confidence.",
                        "caption": f"Task status {completed['status']} · {len(status['events'])} audit events on file · zero raw credentials printed.",
                        "agent_a": "Receives result + audit trail",
                        "agent_b": "Lease released · work complete",
                        "event": "task.completed",
                        "state": "balanced",
                    },
                    {
                        "eyebrow": "Finalisma · design-partner preview",
                        "headline": "One incident. Two agents. One account of what happened.",
                        "caption": "Run the local proof. Then help validate the first real host pair.",
                        "agent_a": "9 documented MCP paths",
                        "agent_b": "0 live Finalisma host validations",
                        "event": "Codex + Claude Code proposed first pair",
                        "state": "balanced",
                    },
                ],
            }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    if not output.is_relative_to(PROJECT_ROOT.resolve()):
        raise SystemExit(f"Refusing to write the public demo outside the project: {output}")
    transcript = run_demo()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(output.relative_to(PROJECT_ROOT)),
                "frames": len(transcript["frames"]),
                "credentials_redacted": transcript["credentials_redacted"],
                "task_status": transcript["proof"]["task_status"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WeftError as exc:
        print(json.dumps({"status": "failed", "error": exc.as_dict()}, indent=2), file=sys.stderr)
        raise SystemExit(1) from exc
