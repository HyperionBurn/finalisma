import { useRef, useMemo, useCallback } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createParticleMaterial } from './shaders/messageParticle';
import { useProgress, useTier } from './useSceneStore';

/**
 * MessageParticles.tsx — Pooled additive glow particles travelling edges.
 * Unicast (accent), group (assert), broadcast (prove) — visually distinct.
 */

const AGENT_POSITIONS: [number, number, number][] = [
  [-2.5, 0.5, -0.5],
  [-2.0, -0.8, 0.3],
  [-3.0, 0.0, -1.0],
  [-1.8, 1.0, 0.5],
  [-3.2, -0.5, 0.0],
];

const ROOM_POS: [number, number, number] = [0, 0, 0];

// Particle pool sizes per tier
const POOL_SIZES = { high: 60, medium: 30, low: 12 };

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
    slot.duration = refused ? 0.6 : 0.9; // refused particles stop early
  }, [pool]);

  // Track progress to fire beat events
  const prevProgress = useRef(0);

  useFrame((_, delta) => {
    if (!pointsRef.current) return;

    // Fire particles based on progress crossing thresholds
    const p = progress;
    const prev = prevProgress.current;

    // Beat 3: messages travel (0.30 → 0.50)
    if (prev < 0.32 && p >= 0.32) {
      // Unicast: agent A → room
      fireParticle(
        new THREE.Vector3(...AGENT_POSITIONS[0]),
        new THREE.Vector3(...ROOM_POS),
        COLORS.unicast
      );
    }
    if (prev < 0.38 && p >= 0.38) {
      // Group: room → agents A,B,C
      for (let i = 0; i < 3; i++) {
        setTimeout(() => {
          fireParticle(
            new THREE.Vector3(...ROOM_POS),
            new THREE.Vector3(...AGENT_POSITIONS[i]),
            COLORS.group
          );
        }, i * 100);
      }
    }
    if (prev < 0.44 && p >= 0.44) {
      // Broadcast: room → all
      for (let i = 0; i < 5; i++) {
        setTimeout(() => {
          fireParticle(
            new THREE.Vector3(...ROOM_POS),
            new THREE.Vector3(...AGENT_POSITIONS[i]),
            COLORS.broadcast
          );
        }, i * 80);
      }
    }

    // Beat 4: refusal particle (0.50 → 0.66)
    if (prev < 0.52 && p >= 0.52) {
      fireParticle(
        new THREE.Vector3(...AGENT_POSITIONS[0]),
        new THREE.Vector3(...ROOM_POS),
        COLORS.refuse,
        true // refused
      );
    }

    prevProgress.current = p;

    // Update particles
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
      let t = particle.t / particle.duration;

      // Refused particles stop at boundary (t clamped to ~0.7)
      if (particle.refused) {
        t = Math.min(t, 0.7);
      }

      if (t >= 1) {
        particle.active = false;
        opacities[i] = 0;
        sizes[i] = 0;
        continue;
      }

      // Position interpolation (slight arc)
      const pos = new THREE.Vector3().lerpVectors(particle.from, particle.to, t);
      // Arc offset
      const arcHeight = Math.sin(t * Math.PI) * 0.3;
      pos.y += arcHeight;

      positions[idx] = pos.x;
      positions[idx + 1] = pos.y;
      positions[idx + 2] = pos.z;

      // Color
      colors[idx] = particle.color.x;
      colors[idx + 1] = particle.color.y;
      colors[idx + 2] = particle.color.z;

      // Opacity ramp: 0 → 1 → 1 → 0
      const opacity = t < 0.2 ? t / 0.2 : t > 0.8 ? (1 - t) / 0.2 : 1;
      opacities[i] = opacity;

      // Size
      sizes[i] = 12 + Math.sin(t * Math.PI) * 6;
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
