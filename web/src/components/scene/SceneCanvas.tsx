import { useEffect, useRef, useState } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { AdaptiveDpr, PerformanceMonitor } from '@react-three/drei';
import { EffectComposer, Bloom, Vignette } from '@react-three/postprocessing';
import * as THREE from 'three';
import CameraRig from './CameraRig';
import RoomCore from './RoomCore';
import AgentNodes from './AgentNodes';
import EdgeLines from './EdgeLines';
import MessageParticles from './MessageParticles';
import { useTier, sceneActions } from './useSceneStore';
import { initEventBridge, disposeEventBridge, fireGateRefusal } from './eventBridge';

/**
 * SceneCanvas.tsx — The R3F island. client:only="visible".
 * Exposes window.FinalismaScene for the scroll lane to drive.
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
      (window as any).__finalismaFrameTimes = frameTimes;
    }

    // Decrement refusal flash
    sceneActions.decrementRefusal(1 / 60);
  });

  return null;
}

function SceneContent() {
  const tier = useTier();

  return (
    <>
      <PerformanceMonitor
        onIncline={() => sceneActions.setTier('high')}
        onDecline={() => sceneActions.setTier('medium')}
      />
      <AdaptiveDpr pixelated />
      <CameraRig />
      <RoomCore />
      <AgentNodes />
      <EdgeLines />
      <MessageParticles />
      <FrameTimer />

      {/* Lighting */}
      <ambientLight intensity={0.4} />
      <pointLight position={[2, 3, 4]} intensity={0.8} color="#9D82FF" />
      <pointLight position={[-2, -1, 3]} intensity={0.4} color="#5B3DF0" />

      {/* Postprocessing — disabled on low tier */}
      {tier !== 'low' && (
        <EffectComposer>
          <Bloom
            luminanceThreshold={0.6}
            intensity={tier === 'medium' ? 0.4 : 0.6}
            mipmapBlur
          />
          <Vignette eskil={false} offset={0.3} darkness={0.7} />
        </EffectComposer>
      )}
    </>
  );
}

export default function SceneCanvas() {
  const [webglSupported, setWebglSupported] = useState(true);

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
    (window as any).FinalismaScene = {
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
      dpr={[1, 2]}
      gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
      camera={{ position: [0, 0, 14], fov: 50, near: 0.1, far: 100 }}
      style={{ width: '100%', height: '100%', display: 'block' }}
      data-agent-canvas
      aria-hidden="true"
    >
      <color attach="background" args={['#0E0F12']} />
      <SceneContent />
    </Canvas>
  );
}
