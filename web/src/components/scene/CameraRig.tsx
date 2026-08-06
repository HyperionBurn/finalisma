import { useRef, useMemo } from 'react';
import { useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { useProgress, useRefusalFlash } from './useSceneStore';

/**
 * CameraRig.tsx — Reads normalized scroll progress and drives camera along a CatmullRomCurve3.
 * 12 position keyframes (2 per beat) from plan §4.
 */

// Camera position keyframes: 2 per beat (entry + exit)
const CAMERA_KEYFRAMES: [number, number, number][] = [
  // Beat 1 Wide: (0,0,14)
  [0, 0, 14],
  [0, 0.2, 13.5],
  // Beat 2 Approach: (0,0.5,11) → (0,0,9)
  [0, 0.5, 11],
  [0, 0, 9],
  // Beat 3 Inside: (0,0,6) → (0,0,4)
  [0, 0, 6],
  [0, 0, 4],
  // Beat 4 The Gate: (0,0,4) → (0,0.2,3.5)
  [0, 0.2, 3.8],
  [0, 0.2, 3.5],
  // Beat 5 Pull back: (0,0.2,3.5) → (0,0,7)
  [0, 0.1, 5],
  [0, 0, 7],
  // Beat 6 Land: (0,0,7) → (0,0,9)
  [0, 0, 8],
  [0, 0, 9],
];

// Target keyframes (simpler — slight upward drift at Gate)
const TARGET_KEYFRAMES: [number, number, number][] = [
  [0, 0, 0],
  [0, 0.1, 0],
  [0, 0.2, 0],
  [0, 0.1, 0],
  [0, 0, 0],
  [0, 0, 0],
  [0, 0.1, 0],
  [0, 0.2, 0],
  [0, 0.3, 0],
  [0, 0.2, 0],
  [0, 0.1, 0],
  [0, 0, 0],
];

export default function CameraRig() {
  const { camera } = useThree();
  const progress = useProgress();
  const refusalFlash = useRefusalFlash();
  const pointerOffset = useRef({ x: 0, y: 0 });

  const curve = useMemo(() => {
    const pts = CAMERA_KEYFRAMES.map((p) => new THREE.Vector3(...p));
    return new THREE.CatmullRomCurve3(pts, false, 'catmullrom', 0.5);
  }, []);

  const targetCurve = useMemo(() => {
    const pts = TARGET_KEYFRAMES.map((p) => new THREE.Vector3(...p));
    return new THREE.CatmullRomCurve3(pts, false, 'catmullrom', 0.5);
  }, []);

  // Pointer parallax (throttled)
  if (typeof window !== 'undefined') {
    window.addEventListener('pointermove', (e) => {
      pointerOffset.current.x = (e.clientX / window.innerWidth - 0.5) * 0.3;
      pointerOffset.current.y = (e.clientY / window.innerHeight - 0.5) * 0.3;
    }, { passive: true });
  }

  useFrame(() => {
    const t = Math.max(0, Math.min(1, progress));
    const pos = curve.getPointAt(t);
    const target = targetCurve.getPointAt(t);

    // Apply pointer parallax offset
    camera.position.set(
      pos.x + pointerOffset.current.x,
      pos.y - pointerOffset.current.y,
      pos.z
    );
    camera.lookAt(
      target.x + pointerOffset.current.x * 0.5,
      target.y - pointerOffset.current.y * 0.5,
      target.z
    );
  });

  return null;
}
