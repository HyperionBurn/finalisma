import { sceneActions, SEED_EVENTS, type AgentEvent } from './useSceneStore';

/**
 * eventBridge.ts — Scene → DOM bridge.
 * Dispatches CustomEvent('agent-event') that the DOM listener subscribes to.
 * Also handles auto gate-refusal firing and beat event dispatch.
 */

let autoRefusalTimer: ReturnType<typeof setInterval> | null = null;
let pastBeat3 = false;
let disposed = false;

export function initEventBridge() {
  // Seed initial events into store
  for (const e of SEED_EVENTS) {
    sceneActions.pushEvent(e);
  }

  // Auto gate refusal every ~18s once past Beat 3
  startAutoRefusal();
}

function startAutoRefusal() {
  if (autoRefusalTimer) clearInterval(autoRefusalTimer);
  autoRefusalTimer = setInterval(() => {
    if (disposed) return;
    const state = sceneActions.getState();
    if (state.progress >= 0.30) {
      fireGateRefusal();
    }
  }, 18000);
}

/**
 * Fire the signature gate-refusal event.
 */
export function fireGateRefusal() {
  const seq = 100 + (sceneActions.getState().events.length - 5); // unique seq
  const evt: AgentEvent = {
    seq,
    kind: 'refuse',
    agent: 'agent-a',
    msg: 'stale_fencing_token (refused)',
  };
  sceneActions.pushEvent(evt);
  sceneActions.triggerRefusal();

  // Dispatch DOM event
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('agent-event', {
      detail: { kind: 'refuse', seq, agent: 'agent-a', msg: 'stale_fencing_token (refused)' },
    }));
  }
}

/**
 * Dispatch a beat event (called by beat detection in SceneCanvas).
 */
export function dispatchBeatEvent(beat: number) {
  if (typeof window === 'undefined') return;

  const events: Record<number, () => void> = {
    2: () => {
      // Agents join
      window.dispatchEvent(new CustomEvent('agent-event', {
        detail: { kind: 'beat', beat, msg: 'Many agents join the room' },
      }));
    },
    3: () => {
      // Messages travel
      window.dispatchEvent(new CustomEvent('agent-event', {
        detail: { kind: 'beat', beat, msg: 'Messages travel the edges' },
      }));
    },
    4: () => {
      // The Gate
      fireGateRefusal();
    },
    5: () => {
      window.dispatchEvent(new CustomEvent('agent-event', {
        detail: { kind: 'beat', beat, msg: 'Evidence recorded' },
      }));
    },
    6: () => {
      window.dispatchEvent(new CustomEvent('agent-event', {
        detail: { kind: 'beat', beat, msg: 'Connect your agent' },
      }));
    },
  };

  events[beat]?.();
}

export function markPastBeat3() {
  pastBeat3 = true;
}

export function disposeEventBridge() {
  disposed = true;
  if (autoRefusalTimer) {
    clearInterval(autoRefusalTimer);
    autoRefusalTimer = null;
  }
}
