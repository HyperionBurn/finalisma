import { useRef, useMemo } from 'react';
import { useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import { createRoomMaterial } from './shaders/roomCore';
import { useRefusalFlash, useBeat } from './useSceneStore';

/**
 * RoomCore.tsx — Central room node with simplex-noise animated surface.
 * Amplitude spikes on refusal.
 *
 * SCALED UP: larger core, stronger emissive presence.
 */

export default function RoomCore() {
  const meshRef = useRef<THREE.Mesh>(null);
  const refusalFlash = useRefusalFlash();
  const beat = useBeat();

  const material = useMemo(() => createRoomMaterial(), []);

  const geometry = useMemo(() => {
    // Luminous core — sphere reads as a glowing orb, not a cube
     const geo = new THREE.SphereGeometry(1.4, 48, 48);
    return geo;
  }, []);

  useFrame((_, delta) => {
    if (!meshRef.current) return;
    const mat = meshRef.current.material as THREE.ShaderMaterial;
    mat.uniforms.uTime.value += delta;
    mat.uniforms.uRefuseAmp.value = refusalFlash;

    // Subtle rotation
    meshRef.current.rotation.y += delta * 0.06;
    meshRef.current.rotation.x += delta * 0.025;
  });

  return (
    <mesh ref={meshRef} geometry={geometry} material={material}>
    </mesh>
  );
}
