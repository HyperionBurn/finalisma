import { useSyncExternalStore } from 'react';

/**
 * useSceneStore.ts — Central scene state store.
 * Uses useSyncExternalStore (no external deps).
 * Holds progress (0..1), current beat, tier, and dispatches beat events.
 */

export type SceneTier = 'high' | 'medium' | 'low';

export interface AgentEvent {
  seq: number;
  kind: 'join' | 'assert' | 'prove' | 'refuse';
  agent: string;
  msg: string;
}

export interface SceneState {
  progress: number;
  beat: number;
  tier: SceneTier;
  reducedMotion: boolean;
  gateState: 'armed' | 'REFUSED: stale fencing token';
  events: AgentEvent[];
  refusalFlash: number;
}

// Beat thresholds matching plan §4
export const BEAT_THRESHOLDS = [0.0, 0.12, 0.30, 0.50, 0.66, 0.82, 1.0];
export const BEAT_CAPTIONS = [
  'One link.',
  'Many agents join.',
  'They all communicate.',
  'The gate refuses stale work.',
  'Evidence recorded.',
  'Connect your agent.',
];

export function beatFromProgress(p: number): number {
  for (let i = BEAT_THRESHOLDS.length - 2; i >= 0; i--) {
    if (p >= BEAT_THRESHOLDS[i]) return i + 1;
  }
  return 1;
}

// Seed events (§8.5 of plan)
export const SEED_EVENTS: AgentEvent[] = [
  { seq: 1, kind: 'join', agent: 'agent-a', msg: 'joined the room' },
  { seq: 2, kind: 'join', agent: 'agent-b', msg: 'joined the room' },
  { seq: 3, kind: 'assert', agent: 'agent-a', msg: 'asserted scope: handoff.txt' },
  { seq: 4, kind: 'prove', agent: 'agent-b', msg: 'claimed task (fencing 1812495659374878)' },
  { seq: 5, kind: 'refuse', agent: 'agent-a', msg: 'stale_fencing_token (refused)' },
];

let state: SceneState = {
  progress: 0,
  beat: 1,
  tier: 'high',
  reducedMotion: false,
  gateState: 'armed',
  events: [],
  refusalFlash: 0,
};

const listeners = new Set<() => void>();

function emitChange() {
  for (const l of listeners) l();
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => { listeners.delete(l); };
}

function getSnapshot(): SceneState {
  return state;
}

export const sceneActions = {
  setProgress(p: number) {
    const clamped = Math.max(0, Math.min(1, p));
    const newBeat = beatFromProgress(clamped);
    if (state.progress !== clamped || state.beat !== newBeat) {
      state = { ...state, progress: clamped, beat: newBeat };
      emitChange();
    }
  },

  setTier(t: SceneTier) {
    if (state.tier !== t) {
      state = { ...state, tier: t };
      emitChange();
    }
  },

  setReducedMotion(v: boolean) {
    if (state.reducedMotion !== v) {
      state = { ...state, reducedMotion: v };
      emitChange();
    }
  },

  pushEvent(e: AgentEvent) {
    if (state.events.find((x) => x.seq === e.seq)) return;
    state = { ...state, events: [...state.events, e] };
    emitChange();
  },

  setGateState(s: SceneState['gateState']) {
    if (state.gateState !== s) {
      state = { ...state, gateState: s };
      emitChange();
    }
  },

  triggerRefusal() {
    state = { ...state, gateState: 'REFUSED: stale fencing token', refusalFlash: 1.0 };
    emitChange();
    setTimeout(() => {
      if (state.gateState !== 'armed') {
        state = { ...state, gateState: 'armed' };
        emitChange();
      }
    }, 2500);
  },

  decrementRefusal(dt: number) {
    if (state.refusalFlash > 0) {
      state = { ...state, refusalFlash: Math.max(0, state.refusalFlash - dt * 1.5) };
      emitChange();
    }
  },

  getState(): SceneState {
    return state;
  },
};

export function useSceneStore(): SceneState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

// Selector hooks
export function useProgress(): number {
  return useSyncExternalStore(subscribe, () => state.progress, () => state.progress);
}

export function useBeat(): number {
  return useSyncExternalStore(subscribe, () => state.beat, () => state.beat);
}

export function useTier(): SceneTier {
  return useSyncExternalStore(subscribe, () => state.tier, () => state.tier);
}

export function useRefusalFlash(): number {
  return useSyncExternalStore(subscribe, () => state.refusalFlash, () => state.refusalFlash);
}

export function useGateState(): SceneState['gateState'] {
  return useSyncExternalStore(subscribe, () => state.gateState, () => state.gateState);
}
