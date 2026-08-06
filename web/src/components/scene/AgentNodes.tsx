import { useRef, useMemo } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createNodeMaterial } from './shaders/nodeMaterial';
import { useProgress, useRefusalFlash, sceneActions } from './useSceneStore';

/**
 * AgentNodes.tsx — 5 instanced agent nodes orbiting the room core.
 * Continuous orbital motion + idle bob + presence pulse. Always visible
 * at progress 0 (truthful initial state); animation only enhances.
 */

const AGENT_COUNT = 5;
const AGENT_LABELS = ['A', 'B', 'C', 'D', 'E'];

// Orbit radii / heights / phases — each agent gets its own track
const AGENT_ORBITS = [
  { radius: 3.2, height: 0.8, phase: 0.0, speed: 0.28 },
  { radius: 2.6, height: -1.2, phase: 1.6, speed: 0.22 },
  { radius: 4.0, height: 0.1, phase: 3.2, speed: 0.18 },
  { radius: 2.4, height: 1.4, phase: 4.8, speed: 0.24 },
  { radius: 4.2, height: -0.7, phase: 5.6, speed: 0.20 },
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
  const refusalFlash = useRefusalFlash();
  // reduced-motion is read via getState() inside useFrame to avoid
  // re-render churn on every frame (the store mutates refusalFlash often).

  const material = useMemo(() => createNodeMaterial(), []);
  const dummy = useMemo(() => new THREE.Object3D(), []);

  // Instance attributes
  const { geometry, phaseOffset } = useMemo(() => {
    const geo = new THREE.BoxGeometry(0.9, 0.9, 0.9, 2, 2, 2);

    const colors = new Float32Array(AGENT_COUNT * 3);
    const phases = new Float32Array(AGENT_COUNT);
    const refusedArr = new Float32Array(AGENT_COUNT);

    for (let i = 0; i < AGENT_COUNT; i++) {
      colors[i * 3] = AGENT_COLORS[i][0];
      colors[i * 3 + 1] = AGENT_COLORS[i][1];
      colors[i * 3 + 2] = AGENT_COLORS[i][2];
      phases[i] = AGENT_ORBITS[i].phase;
      refusedArr[i] = 0;
    }

    const instColor = new THREE.InstancedBufferAttribute(colors, 3);
    const ph = new THREE.InstancedBufferAttribute(phases, 1);
    const ref = new THREE.InstancedBufferAttribute(refusedArr, 1);

    geo.setAttribute('instanceColor', instColor);
    geo.setAttribute('phaseOffset', ph);
    geo.setAttribute('refused', ref);

    return { geometry: geo, phaseOffset: ph };
  }, []);

  useFrame((state) => {
    if (!meshRef.current) return;
    const mat = meshRef.current.material as THREE.ShaderMaterial;
    mat.uniforms.uTime.value = state.clock.elapsedTime;
    // Boost pulse visibility
    mat.uniforms.uPulse.value = 1.0;

    const t = progress;
    const time = state.clock.elapsedTime;

    for (let i = 0; i < AGENT_COUNT; i++) {
      const orbit = AGENT_ORBITS[i];

      // Continuous orbital angle — always moving (idle). Under reduced
      // motion we freeze at the phase angle so the frame is static.
      const angle = sceneActions.getState().reducedMotion ? orbit.phase : orbit.phase + time * orbit.speed;

      // Orbit radius grows slightly with progress (graph "opens up")
      const r = orbit.radius * (0.85 + Math.min(t, 1) * 0.15);

      const x = Math.cos(angle) * r;
      const z = Math.sin(angle) * r;
      // Idle bob — subtle vertical oscillation
      const bob = sceneActions.getState().reducedMotion ? 0 : Math.sin(time * 1.5 + i * 1.2) * 0.08;
      const y = orbit.height + bob;

      // Arrival pop enhancement (after the node is already visible)
      let scale = 1.0;
      if (t > 0 && !sceneActions.getState().reducedMotion) {
        const arrivalStart = 0.12 + i * 0.03;
        const arrivalEnd = 0.20 + i * 0.02;
        if (t > arrivalStart && t < arrivalEnd) {
          const st = (t - arrivalStart) / (arrivalEnd - arrivalStart);
          scale = 1.0 + Math.sin(st * Math.PI) * 0.15; // oversettle pop
        }
      }

      dummy.position.set(x, y, z);
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
