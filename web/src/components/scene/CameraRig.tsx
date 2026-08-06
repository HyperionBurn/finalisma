import { useRef, useMemo, useEffect } from 'react';
import { useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { useProgress, useRefusalFlash } from './useSceneStore';

/**
 * CameraRig.tsx — Reads normalized scroll progress and drives camera along a CatmullRomCurve3.
 * 12 position keyframes (2 per beat) from plan §4, followed EXACTLY:
 *   Beat1 Wide  z=14        → z=14
 *   Beat2 Approach (0,0.5,11) → (0,0,9)
 *   Beat3 Inside  (0,0,6)    → (0,0,4)
 *   Beat4 Gate    (0,0,4)    → (0,0.2,3.5)
 *   Beat5 Pull    (0,0.2,3.5)→ (0,0,7)
 *   Beat6 Land    (0,0,7)    → (0,0,9)
 */

// Camera position keyframes: 2 per beat (entry + exit) — MUST match §4 exactly
const CAMERA_KEYFRAMES: [number, number, number][] = [
  // Beat 1 Wide — entry + exit both wide at z=14
  [0, 0, 14],
  [0, 0, 14],
  // Beat 2 Approach: (0,0.5,11) → (0,0,9)
  [0, 0.5, 11],
  [0, 0, 9],
  // Beat 3 Inside: (0,0,6) → (0,0,4)
  [0, 0, 6],
  [0, 0, 4],
  // Beat 4 The Gate: (0,0,4) → (0,0.2,3.5)
  [0, 0, 4],
  [0, 0.2, 3.5],
  // Beat 5 Pull back: (0,0.2,3.5) → (0,0,7)
  [0, 0.2, 3.5],
  [0, 0, 7],
  // Beat 6 Land: (0,0,7) → (0,0,9)
  [0, 0, 7],
  [0, 0, 9],
];

// Target keyframes — slight upward drift at the Gate (beat 4)
const TARGET_KEYFRAMES: [number, number, number][] = [
  [0, 0, 0],
  [0, 0, 0],
  [0, 0, 0],
  [0, 0, 0],
  [0, 0, 0],
  [0, 0, 0],
  [0, 0, 0],
  [0, 0.2, 0], // Gate: look slightly up
  [0, 0.1, 0],
  [0, 0, 0],
  [0, 0, 0],
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

  // Pointer parallax (throttled, registered once)
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onPointerMove = (e: PointerEvent) => {
      pointerOffset.current.x = (e.clientX / window.innerWidth - 0.5) * 0.3;
      pointerOffset.current.y = (e.clientY / window.innerHeight - 0.5) * 0.3;
    };
    window.addEventListener('pointermove', onPointerMove, { passive: true });
    return () => window.removeEventListener('pointermove', onPointerMove);
  }, []);

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
