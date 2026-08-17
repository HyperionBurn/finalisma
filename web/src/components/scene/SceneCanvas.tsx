import { useEffect, useRef, useState } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { AdaptiveDpr, PerformanceMonitor } from '@react-three/drei';
import * as THREE from 'three';
import CameraRig from './CameraRig';
import RoomCore from './RoomCore';
import AgentNodes from './AgentNodes';
import EdgeLines from './EdgeLines';
import MessageParticles from './MessageParticles';
import { sceneActions } from './useSceneStore';
import { initEventBridge, disposeEventBridge, fireGateRefusal } from './eventBridge';

/**
 * SceneCanvas.tsx — The R3F island. client:only="load".
 * Exposes window.WeftScene for the scroll lane to drive.
 * SCALED UP: volumetric haze, wider FOV, and a lightweight animated graph.
 */

// Frame time ring buffer
const FRAME_BUFFER_SIZE = 120;
const frameTimes: number[] = [];
let lastFrameTime = 0;

function FrameTimer() {
  useFrame((state) => {
    const now = state.clock.elapsedTime * 1000;
    if (lastFrameTime > 0) {
      const delta = now - lastFrameTime;
      frameTimes.push(delta);
      if (frameTimes.length > FRAME_BUFFER_SIZE) frameTimes.shift();
    }
    lastFrameTime = now;

    // Expose frame times
    if (typeof window !== 'undefined') {
      (window as any).__weftFrameTimes = frameTimes;
    }

    // Decrement refusal flash
    sceneActions.decrementRefusal(1 / 60);
  });

  return null;
}

// Volumetric haze — a large additive glow plane behind everything
function VolumetricHaze() {
  const meshRef = useRef<THREE.Mesh>(null);

  const material = useRef(
    new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
      },
      vertexShader: `
        varying vec2 vUv;
        void main() {
          vUv = uv;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform float uTime;
        varying vec2 vUv;
        void main() {
          // Soft radial gradient haze centered slightly above middle
          vec2 center = vec2(0.5, 0.42);
          float d = distance(vUv, center);
          float glow = smoothstep(0.7, 0.0, d) * 0.35;
          // Subtle animated pulse
          float pulse = 0.85 + 0.15 * sin(uTime * 0.8);
          vec3 col = vec3(0.357, 0.239, 0.941) * glow * pulse;
          gl_FragColor = vec4(col, glow * 0.5);
        }
      `,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      side: THREE.DoubleSide,
    })
  );

  useFrame((state) => {
    material.current.uniforms.uTime.value = state.clock.elapsedTime;
  });

  return (
    <mesh ref={meshRef} position={[0, 0, -2]} scale={[18, 12, 1]}>
      <planeGeometry args={[1, 1]} />
      <primitive object={material.current} attach="material" />
    </mesh>
  );
}

// Scene group that applies a subtle pointer-reactive drift on top of the
// CameraRig's own parallax. This makes the whole graph feel alive even
// before the camera path kicks in.
function SceneGroup({ children, position = [0, 0, 0] }: { children: React.ReactNode; position?: [number, number, number] }) {
  const groupRef = useRef<THREE.Group>(null);
  const pointer = useRef({ x: 0, y: 0 });
  const basePos = useRef<[number, number, number]>(position);

  if (typeof window !== 'undefined') {
    window.addEventListener('pointermove', (e) => {
      pointer.current.x = (e.clientX / window.innerWidth - 0.5);
      pointer.current.y = (e.clientY / window.innerHeight - 0.5);
    }, { passive: true });
  }

  useFrame((_, delta) => {
    if (!groupRef.current || sceneActions.getState().reducedMotion) return;
    // Subtle counter-drift: the graph leans slightly toward the pointer.
    const targetX = basePos.current[0] + pointer.current.x * 0.3;
    const targetY = basePos.current[1] - pointer.current.y * 0.3;
    groupRef.current.position.x += (targetX - groupRef.current.position.x) * Math.min(1, delta * 2);
    groupRef.current.position.y += (targetY - groupRef.current.position.y) * Math.min(1, delta * 2);
    // Gentle continuous yaw so the scene never looks frozen.
    groupRef.current.rotation.y += delta * 0.06;
  });

  return <group ref={groupRef} position={position}>{children}</group>;
}

function SceneContent() {
  return (
    <>
      <PerformanceMonitor
        onIncline={() => sceneActions.setTier('high')}
        onDecline={() => sceneActions.setTier('medium')}
      />
      <AdaptiveDpr pixelated />
      <CameraRig />
      <VolumetricHaze />
      {/* Funnel lane (2026-08-07): nudged the graph down+right so the glowing
          core's bloom clears the hero headline's tail at 1440x900. */}
      <SceneGroup position={[5.5, -1.5, 0]}>
        <RoomCore />
        <AgentNodes />
        <EdgeLines />
        <MessageParticles />
      </SceneGroup>
      <FrameTimer />

      {/* Lighting — stronger for more dramatic glow */}
      <ambientLight intensity={0.5} />
      <pointLight position={[3, 4, 5]} intensity={1.2} color="#9D82FF" />
      <pointLight position={[-3, -2, 4]} intensity={0.6} color="#5B3DF0" />
      <pointLight position={[0, 0, 0]} intensity={0.8} color="#9D82FF" />

    </>
  );
}

export default function SceneCanvas() {
  const [webglSupported, setWebglSupported] = useState(true);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [frameloop, setFrameloop] = useState<'always' | 'never'>('always');

  // Plan §5.10: pause the RAF render loop when the hero canvas is out of
  // viewport. The scene only needs to animate while visible; this frees GPU
  // for the rest of the page and keeps the scroll thread clean.
  useEffect(() => {
    const el = canvasRef.current?.closest('[data-agent-canvas-wrap]') as HTMLElement | null;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      setFrameloop(entries[0].isIntersecting ? 'always' : 'never');
    }, { rootMargin: '0px' });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  // Check WebGL support
  useEffect(() => {
    try {
      const testCanvas = document.createElement('canvas');
      const gl = testCanvas.getContext('webgl2') || testCanvas.getContext('webgl');
      if (!gl) {
        setWebglSupported(false);
        return;
      }
    } catch {
      setWebglSupported(false);
      return;
    }

    // Check reduced motion
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      sceneActions.setReducedMotion(true);
    }

    // Init event bridge
    initEventBridge();

    // Expose API for scroll lane
    (window as any).WeftScene = {
      setProgress: (t: number) => sceneActions.setProgress(t),
      setTier: (t: 'high' | 'medium' | 'low') => sceneActions.setTier(t),
      fireGateRefusal,
    };

    return () => {
      disposeEventBridge();
    };
  }, []);

  if (!webglSupported) {
    return null; // Static poster from .astro handles fallback
  }

  return (
    <Canvas
      frameloop={frameloop}
      dpr={[1, 1]}
      gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
      camera={{ position: [0, 0, 14], fov: 60, near: 0.1, far: 100 }}
      style={{ width: '100%', height: '100%', display: 'block' }}
      data-agent-canvas
      aria-hidden="true"
    >
      <color attach="background" args={['#0E0F12']} />
      <SceneContent />
    </Canvas>
  );
}
