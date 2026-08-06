import * as THREE from 'three';

/**
 * nodeMaterial.ts — Fresnel + presence pulse GLSL for agent nodes.
 * Per-instance colour, refusal flash, time-driven pulse (2.4s cycle, per-instance phase).
 */

export const nodeVertexShader = /* glsl */ `
  attribute vec3 instanceColor;
  attribute float phaseOffset;
  attribute float refused;

  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vInstanceColor;
  varying float vPhase;
  varying float vRefused;

  void main() {
    vInstanceColor = instanceColor;
    vPhase = phaseOffset;
    vRefused = refused;

    vec4 worldPos = instanceMatrix * vec4(position, 1.0);
    vec4 mvPos = modelViewMatrix * worldPos;
    vNormal = normalize(normalMatrix * normal);
    vViewDir = normalize(-mvPos.xyz);

    gl_Position = projectionMatrix * mvPos;
  }
`;

export const nodeFragmentShader = /* glsl */ `
  uniform float uTime;
  uniform float uPulse;

  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vInstanceColor;
  varying float vPhase;
  varying float vRefused;

  void main() {
    // Fresnel rim
    float fresnel = pow(1.0 - max(dot(vNormal, vViewDir), 0.0), 2.5);

    // Presence pulse — 2.4s cycle, per-instance phase
    float t = (uTime + vPhase) / 2.4;
    float pulse = 0.5 + 0.5 * sin(t * 6.28318);

    // Base colour with pulse brightening
    vec3 base = vInstanceColor * (0.7 + 0.3 * pulse * uPulse);

    // Rim glow
    vec3 rim = vInstanceColor * fresnel * 1.4;

    // Refusal flash — overrides to refuse red
    vec3 refuseCol = vec3(0.973, 0.443, 0.443); // #F87171
    float flash = vRefused * (0.6 + 0.4 * sin(uTime * 12.0));
    vec3 color = mix(base + rim, refuseCol + rim * 0.5, flash);

    float alpha = 0.85 + fresnel * 0.15;

    gl_FragColor = vec4(color, alpha);
  }
`;

export function createNodeMaterial(): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    vertexShader: nodeVertexShader,
    fragmentShader: nodeFragmentShader,
    uniforms: {
      uTime: { value: 0 },
      uPulse: { value: 1.0 },
    },
    transparent: true,
    depthWrite: true,
  });
}
