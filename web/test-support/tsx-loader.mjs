/**
 * A Node ESM loader hook that transforms .tsx source through esbuild before
 * Node parses it. Node's own built-in TypeScript type-stripping (the thing
 * that already lets tests/*.js import web/src/lib/*.ts directly) erases
 * type annotations only — it does not transform JSX syntax, so a .tsx file
 * like RoomView.tsx cannot be imported as-is. This is the one piece missing
 * to mount a real component in the node --test lane without touching the
 * component itself.
 *
 * Registered from a test file via `module.register()`; only intercepts
 * `.tsx` URLs and defers everything else (including plain `.ts`) to Node's
 * own resolution, so nothing already working changes behaviour.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import esbuild from 'esbuild';

// TypeScript source omits extensions on relative imports (`from
// '../../lib/api'`) and relies on the compiler's own resolution to find
// api.ts. Node's ESM resolver has no such leniency, so a component that
// works fine under Astro/Vite's build fails to import at all here. Retry a
// failed relative resolution with .tsx/.ts appended before giving up -
// this is resolution behaviour only, RoomView.tsx's imports are untouched.
const RESOLVABLE_EXTENSIONS = ['.tsx', '.ts'];

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (err) {
    const isNotFound = err?.code === 'ERR_MODULE_NOT_FOUND' || err?.code === 'ERR_UNSUPPORTED_DIR_IMPORT';
    if (!isNotFound || !(specifier.startsWith('.') || specifier.startsWith('/'))) throw err;
    for (const ext of RESOLVABLE_EXTENSIONS) {
      try {
        return await nextResolve(specifier + ext, context);
      } catch { /* try the next extension */ }
    }
    throw err;
  }
}

export function load(url, context, nextLoad) {
  if (!url.endsWith('.tsx')) return nextLoad(url, context);
  const file = fileURLToPath(url);
  const source = readFileSync(file, 'utf8');
  const { code } = esbuild.transformSync(source, {
    loader: 'tsx',
    format: 'esm',
    jsx: 'automatic',
    jsxImportSource: 'react',
    target: 'esnext',
    sourcefile: file,
  });
  return { format: 'module', source: code, shortCircuit: true };
}
