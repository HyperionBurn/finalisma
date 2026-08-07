import { useRef, useMemo, useCallback, useEffect } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createParticleMaterial } from './shaders/messageParticle';
import { useProgress, useTier, sceneActions } from './useSceneStore';

/**
 * MessageParticles.tsx — Pooled additive glow particles travelling edges.
 * Unicast (accent), group (assert), broadcast (prove), refuse — visually distinct.
 *
 * Fires a CONTINUOUS self-sustaining loop (unicast → group → broadcast → refuse,
 * repeating) on a timer so the scene is alive at progress 0 without any scroll.
 * Progress-based beat fires are kept as an additional enhancement.
 *
 * SCALED UP: larger particles, longer trails (stretched via size), more of them.
 */

const AGENT_ORBITS = [
  { radius: 5.4, height: 1.4, phase: 0.0, speed: 0.42 },
  { radius: 4.4, height: -2.0, phase: 1.6, speed: 0.34 },
  { radius: 6.6, height: 0.3, phase: 3.2, speed: 0.28 },
  { radius: 4.0, height: 2.4, phase: 4.8, speed: 0.38 },
  { radius: 6.9, height: -1.2, phase: 5.6, speed: 0.30 },
];

const ROOM_POS: [number, number, number] = [0, 0, 0];

// Particle pool sizes per tier — MORE particles for flashier scene
const POOL_SIZES = { high: 90, medium: 50, low: 20 };

interface Particle {
  active: boolean;
  from: THREE.Vector3;
  to: THREE.Vector3;
  t: number;
  duration: number;
  color: THREE.Vector3;
  refused: boolean;
}

const COLORS = {
  unicast: new THREE.Vector3(0.357, 0.239, 0.941),  // accent #5B3DF0
  group: new THREE.Vector3(0.957, 0.459, 0.420),    // assert #F4756B
  broadcast: new THREE.Vector3(0.878, 0.702, 0.290), // prove #E0B34A
  refuse: new THREE.Vector3(0.973, 0.443, 0.443),    // refuse #F87171
};

// Resolve an agent's live world position at the current time.
function agentLivePos(i: number, time: number, reducedMotion: boolean, progress: number): THREE.Vector3 {
  const orbit = AGENT_ORBITS[i];
  const angle = reducedMotion ? orbit.phase : orbit.phase + time * orbit.speed;
  const r = orbit.radius * (0.85 + Math.min(progress, 1) * 0.15);
  const bob = reducedMotion ? 0 : Math.sin(time * 1.5 + i * 1.2) * 0.12;
  return new THREE.Vector3(
    Math.cos(angle) * r,
    orbit.height + bob,
    Math.sin(angle) * r
  );
}

export default function MessageParticles() {
  const progress = useProgress();
  const tier = useTier();
  const pointsRef = useRef<THREE.Points>(null);

  const poolSize = POOL_SIZES[tier];

  const material = useMemo(() => createParticleMaterial(), []);

  // Pre-allocated buffers
  const { geometry, positions, colors, sizes, opacities } = useMemo(() => {
    const pos = new Float32Array(poolSize * 3);
    const col = new Float32Array(poolSize * 3);
    const sz = new Float32Array(poolSize);
    const opa = new Float32Array(poolSize);

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('particleColor', new THREE.BufferAttribute(col, 3));
    geo.setAttribute('particleSize', new THREE.BufferAttribute(sz, 1));
    geo.setAttribute('particleOpacity', new THREE.BufferAttribute(opa, 1));

    return { geometry: geo, positions: pos, colors: col, sizes: sz, opacities: opa };
  }, [poolSize]);

  // Particle pool
  const pool = useMemo<Particle[]>(() => {
    const p: Particle[] = [];
    for (let i = 0; i < poolSize; i++) {
      p.push({
        active: false,
        from: new THREE.Vector3(),
        to: new THREE.Vector3(),
        t: 0,
        duration: 0.9,
        color: new THREE.Vector3(),
        refused: false,
      });
    }
    return p;
  }, [poolSize]);

  // Fire a particle
  const fireParticle = useCallback((from: THREE.Vector3, to: THREE.Vector3, color: THREE.Vector3, refused = false) => {
    const slot = pool.find((p) => !p.active);
    if (!slot) return;
    slot.active = true;
    slot.from.copy(from);
    slot.to.copy(to);
    slot.t = 0;
    slot.color.copy(color);
    slot.refused = refused;
    slot.duration = refused ? 0.6 : 0.9;
  }, [pool]);

  // Continuous idle loop: fires unicast → group → broadcast → refuse on repeat.
  // Uses a ref-based timer so it runs independent of scroll progress.
  const idleTimerRef = useRef(0);
  const idleStepRef = useRef(0);

  // Reset the idle loop when reducedMotion flips so we don't double-fire.
  useEffect(() => {
    idleTimerRef.current = 0;
    idleStepRef.current = 0;
  }, [sceneActions.getState().reducedMotion]);

  // Track progress to fire beat events (enhancement on top of idle loop)
  const prevProgress = useRef(0);

  useFrame((state, delta) => {
    if (!pointsRef.current) return;
    const time = state.clock.elapsedTime;

    // ---- Continuous idle particle loop ----
    // Fire a repeating sequence every ~1.2s. Each "beat" of the sequence
    // fires one message type. Under reduced motion we skip the timer but
    // still allow a single static frame (no particles in flight).
    if (!sceneActions.getState().reducedMotion) {
      idleTimerRef.current += delta;
      const stepDuration = 1.2; // seconds between sequence steps (faster = flashier)
      if (idleTimerRef.current >= stepDuration) {
        idleTimerRef.current -= stepDuration;
        const step = idleStepRef.current % 4;
        idleStepRef.current++;

        if (step === 0) {
          // Unicast: agent A → room
          const from = agentLivePos(0, time, false, progress);
          fireParticle(from, new THREE.Vector3(...ROOM_POS), COLORS.unicast);
        } else if (step === 1) {
          // Group: room → agents A,B,C
          for (let i = 0; i < 3; i++) {
            const to = agentLivePos(i, time, false, progress);
            fireParticle(new THREE.Vector3(...ROOM_POS), to, COLORS.group);
          }
        } else if (step === 2) {
          // Broadcast: room → all 5
          for (let i = 0; i < 5; i++) {
            const to = agentLivePos(i, time, false, progress);
            fireParticle(new THREE.Vector3(...ROOM_POS), to, COLORS.broadcast);
          }
        } else {
          // Refuse: agent A → room (refused)
          const from = agentLivePos(0, time, false, progress);
          fireParticle(from, new THREE.Vector3(...ROOM_POS), COLORS.refuse, true);
        }
      }
    }

    // ---- Progress-based beat fires (additional enhancement) ----
    const p = progress;
    const prev = prevProgress.current;
    const rm = sceneActions.getState().reducedMotion;

    if (prev < 0.32 && p >= 0.32) {
      const from = agentLivePos(0, time, rm, p);
      fireParticle(from, new THREE.Vector3(...ROOM_POS), COLORS.unicast);
    }
    if (prev < 0.38 && p >= 0.38) {
      for (let i = 0; i < 3; i++) {
        const to = agentLivePos(i, time, rm, p);
        fireParticle(new THREE.Vector3(...ROOM_POS), to, COLORS.group);
      }
    }
    if (prev < 0.44 && p >= 0.44) {
      for (let i = 0; i < 5; i++) {
        const to = agentLivePos(i, time, rm, p);
        fireParticle(new THREE.Vector3(...ROOM_POS), to, COLORS.broadcast);
      }
    }
    if (prev < 0.52 && p >= 0.52) {
      const from = agentLivePos(0, time, rm, p);
      fireParticle(from, new THREE.Vector3(...ROOM_POS), COLORS.refuse, true);
    }

    prevProgress.current = p;

    // ---- Update particles ----
    for (let i = 0; i < poolSize; i++) {
      const particle = pool[i];
      const idx = i * 3;

      if (!particle.active) {
        positions[idx] = 0;
        positions[idx + 1] = 0;
        positions[idx + 2] = 0;
        opacities[i] = 0;
        sizes[i] = 0;
        continue;
      }

      particle.t += delta;
      let tNorm = particle.t / particle.duration;

      // Refused particles stop at boundary (t clamped to ~0.7)
      if (particle.refused) {
        tNorm = Math.min(tNorm, 0.7);
      }

      if (tNorm >= 1) {
        particle.active = false;
        opacities[i] = 0;
        sizes[i] = 0;
        continue;
      }

      // Position interpolation (slight arc)
      const pos = new THREE.Vector3().lerpVectors(particle.from, particle.to, tNorm);
      // Arc offset
      const arcHeight = Math.sin(tNorm * Math.PI) * 0.4;
      pos.y += arcHeight;

      positions[idx] = pos.x;
      positions[idx + 1] = pos.y;
      positions[idx + 2] = pos.z;

      // Color
      colors[idx] = particle.color.x;
      colors[idx + 1] = particle.color.y;
      colors[idx + 2] = particle.color.z;

      // Opacity ramp: 0 → 1 → 1 → 0
      const opacity = tNorm < 0.2 ? tNorm / 0.2 : tNorm > 0.8 ? (1 - tNorm) / 0.2 : 1;
      opacities[i] = opacity;

      // Size — LARGER particles with a "trail" feel via wider size range
      sizes[i] = 18 + Math.sin(tNorm * Math.PI) * 10;
    }

    // Mark attributes for update
    (geometry.getAttribute('position') as THREE.BufferAttribute).needsUpdate = true;
    (geometry.getAttribute('particleColor') as THREE.BufferAttribute).needsUpdate = true;
    (geometry.getAttribute('particleSize') as THREE.BufferAttribute).needsUpdate = true;
    (geometry.getAttribute('particleOpacity') as THREE.BufferAttribute).needsUpdate = true;
  });

  return (
    <points ref={pointsRef} geometry={geometry} material={material} />
  );
}
