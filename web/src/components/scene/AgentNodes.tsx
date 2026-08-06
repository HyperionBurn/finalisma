import { useRef, useMemo, useEffect } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createNodeMaterial } from './shaders/nodeMaterial';
import { useProgress, useBeat, useRefusalFlash } from './useSceneStore';

/**
 * AgentNodes.tsx — 5 instanced agent nodes arranged in an arc.
 * Scale-in on arcs during Beat 2. Per-instance colour (role tint).
 */

const AGENT_COUNT = 5;
const AGENT_LABELS = ['A', 'B', 'C', 'D', 'E'];

// Arc positions around the room
const AGENT_POSITIONS: [number, number, number][] = [
  [-2.5, 0.5, -0.5],
  [-2.0, -0.8, 0.3],
  [-3.0, 0.0, -1.0],
  [-1.8, 1.0, 0.5],
  [-3.2, -0.5, 0.0],
];

// Role tints: A=assert, B=prove, C=accent, D=accent, E=assert
const AGENT_COLORS: [number, number, number][] = [
  [0.957, 0.459, 0.420], // assert #F4756B
  [0.878, 0.702, 0.290], // prove #E0B34A
  [0.357, 0.239, 0.941], // accent #5B3DF0
  [0.357, 0.239, 0.941], // accent
  [0.957, 0.459, 0.420], // assert
];

export default function AgentNodes() {
  const meshRef = useRef<THREE.InstancedMesh>(null);
  const progress = useProgress();
  const beat = useBeat();
  const refusalFlash = useRefusalFlash();

  const material = useMemo(() => createNodeMaterial(), []);
  const dummy = useMemo(() => new THREE.Object3D(), []);

  // Instance attributes
  const { geometry, instanceColor, phaseOffset, refused } = useMemo(() => {
    const geo = new THREE.BoxGeometry(0.5, 0.5, 0.5, 2, 2, 2);

    const colors = new Float32Array(AGENT_COUNT * 3);
    const phases = new Float32Array(AGENT_COUNT);
    const refusedArr = new Float32Array(AGENT_COUNT);

    for (let i = 0; i < AGENT_COUNT; i++) {
      colors[i * 3] = AGENT_COLORS[i][0];
      colors[i * 3 + 1] = AGENT_COLORS[i][1];
      colors[i * 3 + 2] = AGENT_COLORS[i][2];
      phases[i] = i * 0.8; // 0, 0.8, 1.6, 2.4, 3.2
      refusedArr[i] = 0;
    }

    const instColor = new THREE.InstancedBufferAttribute(colors, 3);
    const ph = new THREE.InstancedBufferAttribute(phases, 1);
    const ref = new THREE.InstancedBufferAttribute(refusedArr, 1);

    geo.setAttribute('instanceColor', instColor);
    geo.setAttribute('phaseOffset', ph);
    geo.setAttribute('refused', ref);

    return { geometry: geo, instanceColor: instColor, phaseOffset: ph, refused: ref };
  }, []);

  // Scale-in animation: agents arrive during Beat 2 (progress 0.12 → 0.30)
  useFrame((state) => {
    if (!meshRef.current) return;
    const mat = meshRef.current.material as THREE.ShaderMaterial;
    mat.uniforms.uTime.value = state.clock.elapsedTime;

    const t = progress;

    for (let i = 0; i < AGENT_COUNT; i++) {
      // Each agent arrives at slightly different time
      const arrivalStart = 0.12 + i * 0.03;
      const arrivalEnd = 0.20 + i * 0.02;
      let scale = 1.0;
      if (t < arrivalStart) {
        scale = 0.0;
      } else if (t < arrivalEnd) {
        const st = (t - arrivalStart) / (arrivalEnd - arrivalStart);
        scale = Math.min(1, st * 1.2);
      } else {
        scale = 1.0;
      }

      // Idle bob
      const bob = Math.sin(state.clock.elapsedTime * 1.5 + i * 1.2) * 0.05;

      dummy.position.set(
        AGENT_POSITIONS[i][0],
        AGENT_POSITIONS[i][1] + bob,
        AGENT_POSITIONS[i][2]
      );
      dummy.scale.setScalar(scale);
      dummy.updateMatrix();
      meshRef.current.setMatrixAt(i, dummy.matrix);
    }
    meshRef.current.instanceMatrix.needsUpdate = true;
  });

  return (
    <instancedMesh
      ref={meshRef}
      args={[geometry, material, AGENT_COUNT]}
    />
  );
}
