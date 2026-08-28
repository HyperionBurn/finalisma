// Loaded as the FIRST import of astro.config.mjs, before @astrojs/react (and
// therefore before anything can `require('react')` and cache the development
// copy). ES module imports evaluate in source order, depth-first, so this file
// finishes running before any later import in the config is touched.
//
// Why it has to exist: React and @vitejs/plugin-react (via @astrojs/react)
// choose development vs production output purely from process.env.NODE_ENV.
// `astro build` only sets it with `||=`, so a build shell that already exports
// NODE_ENV=development (a dev tool's environment leaking in) silently yields a
// DEVELOPMENT bundle — the slow reconciler, dev-only warning paths,
// react/jsx-dev-runtime, ~2x the bytes — and a react/react-dom version
// mismatch during static-route generation. Pin it for the build here so the
// artifact never depends on the launching shell. `astro dev`/`preview`/`sync`
// are left untouched.
if (process.argv.includes('build')) {
  process.env.NODE_ENV = 'production';
}
