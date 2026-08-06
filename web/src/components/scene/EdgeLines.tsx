import { useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { useProgress } from './useSceneStore';

/**
 * EdgeLines.tsx — Line segments connecting each agent to the room.
 * Drawn in during Beat 2 (agents joining).
 */

const AGENT_POSITIONS: [number, number, number][] = [
  [-2.5, 0.5, -0.5],
  [-2.0, -0.8, 0.3],
  [-3.0, 0.0, -1.0],
  [-1.8, 1.0, 0.5],
  [-3.2, -0.5, 0.0],
];

export default function EdgeLines() {
  const progress = useProgress();
  const linesRef = useRef<THREE.LineSegments>(null);

  const geometry = useMemo(() => {
    // 5 segments × 2 points × 3 coords = 30 floats
    const positions = new Float32Array(AGENT_POSITIONS.length * 2 * 3);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    return geo;
  }, []);

  useFrame(() => {
    if (!linesRef.current) return;
    const posAttr = linesRef.current.geometry.getAttribute('position') as THREE.BufferAttribute;
    const arr = posAttr.array as Float32Array;

    // Edge draw-in: progress 0.12 → 0.30
    const drawT = Math.max(0, Math.min(1, (progress - 0.12) / 0.18));

    for (let i = 0; i < AGENT_POSITIONS.length; i++) {
      const agentPos = AGENT_POSITIONS[i];
      const roomPos: [number, number, number] = [0, 0, 0];

      // Interpolate from agent to room based on draw progress
      // Each edge draws in slightly staggered
      const edgeStart = i * 0.15;
      const edgeT = Math.max(0, Math.min(1, (drawT - edgeStart) / (1 - edgeStart)));

      const idx = i * 6;
      arr[idx] = agentPos[0];
      arr[idx + 1] = agentPos[1];
      arr[idx + 2] = agentPos[2];
      arr[idx + 3] = agentPos[0] + (roomPos[0] - agentPos[0]) * edgeT;
      arr[idx + 4] = agentPos[1] + (roomPos[1] - agentPos[1]) * edgeT;
      arr[idx + 5] = agentPos[2] + (roomPos[2] - agentPos[2]) * edgeT;
    }

    posAttr.needsUpdate = true;
  });

  return (
    <lineSegments ref={linesRef} geometry={geometry}>
      <lineBasicMaterial color="#9D82FF" transparent opacity={0.4} />
    </lineSegments>
  );
}
