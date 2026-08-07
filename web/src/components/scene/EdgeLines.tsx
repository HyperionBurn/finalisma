import { useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { useProgress, sceneActions } from './useSceneStore';

/**
 * EdgeLines.tsx — Line segments connecting each orbiting agent to the room.
 * Always visible at progress 0 (truthful initial state). The draw-in is a
 * subtle length-enhancement; edges are never invisible.
 *
 * SCALED UP: wider orbits, thicker/brighter lines.
 */

const AGENT_ORBITS = [
  { radius: 5.4, height: 1.4, phase: 0.0, speed: 0.42 },
  { radius: 4.4, height: -2.0, phase: 1.6, speed: 0.34 },
  { radius: 6.6, height: 0.3, phase: 3.2, speed: 0.28 },
  { radius: 4.0, height: 2.4, phase: 4.8, speed: 0.38 },
  { radius: 6.9, height: -1.2, phase: 5.6, speed: 0.30 },
];

const AGENT_COUNT = AGENT_ORBITS.length;

export default function EdgeLines() {
  const progress = useProgress();
  const linesRef = useRef<THREE.LineSegments>(null);

  const geometry = useMemo(() => {
    // 5 segments × 2 points × 3 coords = 30 floats
    const positions = new Float32Array(AGENT_COUNT * 2 * 3);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    return geo;
  }, []);

  useFrame((state) => {
    if (!linesRef.current) return;
    const posAttr = linesRef.current.geometry.getAttribute('position') as THREE.BufferAttribute;
    const arr = posAttr.array as Float32Array;

    const t = progress;
    const time = state.clock.elapsedTime;

    // Edge draw-in: subtle length enhancement from 0.82 → 1.0 of full edge.
    // At progress 0 the edge is already 82% visible (truthful state).
    const drawT = Math.min(1, 0.82 + t * 0.18);

    for (let i = 0; i < AGENT_COUNT; i++) {
      const orbit = AGENT_ORBITS[i];
      const rm = sceneActions.getState().reducedMotion;
      const angle = rm ? orbit.phase : orbit.phase + time * orbit.speed;
      const r = orbit.radius * (0.85 + Math.min(t, 1) * 0.15);
      const bob = rm ? 0 : Math.sin(time * 1.5 + i * 1.2) * 0.12;

      const ax = Math.cos(angle) * r;
      const ay = orbit.height + bob;
      const az = Math.sin(angle) * r;

      const roomPos: [number, number, number] = [0, 0, 0];

      const idx = i * 6;
      arr[idx] = ax;
      arr[idx + 1] = ay;
      arr[idx + 2] = az;
      arr[idx + 3] = ax + (roomPos[0] - ax) * drawT;
      arr[idx + 4] = ay + (roomPos[1] - ay) * drawT;
      arr[idx + 5] = az + (roomPos[2] - az) * drawT;
    }

    posAttr.needsUpdate = true;
  });

  return (
    <lineSegments ref={linesRef} geometry={geometry}>
      <lineBasicMaterial color="#9D82FF" transparent opacity={0.55} linewidth={2} />
    </lineSegments>
  );
}
