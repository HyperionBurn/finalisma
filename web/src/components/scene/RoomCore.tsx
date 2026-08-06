import { useRef, useMemo } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createRoomMaterial } from './shaders/roomCore';
import { useRefusalFlash, useBeat } from './useSceneStore';

/**
 * RoomCore.tsx — Central room node with simplex-noise animated surface.
 * Amplitude spikes on refusal.
 */

export default function RoomCore() {
  const meshRef = useRef<THREE.Mesh>(null);
  const refusalFlash = useRefusalFlash();
  const beat = useBeat();

  const material = useMemo(() => createRoomMaterial(), []);

  const geometry = useMemo(() => {
    // Rounded box approximation — use a box with bevel via segments
    const geo = new THREE.BoxGeometry(0.7, 0.7, 0.7, 4, 4, 4);
    return geo;
  }, []);

  useFrame((_, delta) => {
    if (!meshRef.current) return;
    const mat = meshRef.current.material as THREE.ShaderMaterial;
    mat.uniforms.uTime.value += delta;
    mat.uniforms.uRefuseAmp.value = refusalFlash;

    // Subtle rotation
    meshRef.current.rotation.y += delta * 0.05;
    meshRef.current.rotation.x += delta * 0.02;
  });

  return (
    <mesh ref={meshRef} geometry={geometry} material={material}>
    </mesh>
  );
}
