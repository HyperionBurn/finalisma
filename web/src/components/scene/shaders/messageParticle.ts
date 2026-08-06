import * as THREE from 'three';

/**
 * messageParticle.ts — Additive glow travelling point.
 * No texture: in-shader smoothstep falloff. Colour per send-type.
 * Opacity ramps 0→1→1→0 over travel lifetime.
 */

export const particleVertexShader = /* glsl */ `
  attribute vec3 particleColor;
  attribute float particleSize;
  attribute float particleOpacity;

  varying vec3 vColor;
  varying float vOpacity;

  void main() {
    vColor = particleColor;
    vOpacity = particleOpacity;

    vec4 mvPos = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mvPos;
    gl_PointSize = particleSize * (300.0 / -mvPos.z);
  }
`;

export const particleFragmentShader = /* glsl */ `
  varying vec3 vColor;
  varying float vOpacity;

  void main() {
    // Circular falloff — no texture
    vec2 coord = gl_PointCoord - vec2(0.5);
    float dist = length(coord);
    float alpha = smoothstep(0.5, 0.0, dist) * vOpacity;
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(vColor * alpha, alpha);
  }
`;

export function createParticleMaterial(): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: particleVertexShader,
    fragmentShader: particleFragmentShader,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });
}
