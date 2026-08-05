/* Render site/assets/og-card.svg to og-card.png at exactly 1200 x 630.
 *
 * The SVG is the single source of truth. The previous renderer redrew the card
 * from scratch in System.Drawing with Georgia and Consolas, which meant the
 * card and the site could drift apart silently and the PNG could never use the
 * self-hosted webfonts. Rendering the SVG itself removes both problems.
 *
 *   node ./scripts/render-og-card.cjs
 */
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const playwrightPath = process.env.FINALISMA_PLAYWRIGHT
  || "C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright";
const { chromium } = require(playwrightPath);

const root = path.resolve(__dirname, "..");
const svgPath = path.join(root, "site", "assets", "og-card.svg");
const outputPath = process.argv[2] || path.join(root, "site", "assets", "og-card.png");

const WIDTH = 1200;
const HEIGHT = 630;

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: WIDTH, height: HEIGHT },
    deviceScaleFactor: 1
  });

  // Load the SVG as a document so its relative @font-face URLs resolve against
  // site/assets/. A standalone SVG document has no <body>, so there is no
  // margin to reset — and no <head> to inject one into either.
  await page.goto(pathToFileURL(svgPath).href, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(200);

  await page.screenshot({ path: outputPath, clip: { x: 0, y: 0, width: WIDTH, height: HEIGHT } });
  await browser.close();

  // The launch test asserts these dimensions from the PNG header; verify here
  // rather than discovering a 1201-pixel card in CI.
  const header = fs.readFileSync(outputPath);
  const width = header.readUInt32BE(16);
  const height = header.readUInt32BE(20);
  if (header.subarray(0, 8).toString("latin1") !== "\x89PNG\r\n\x1a\n" || width !== WIDTH || height !== HEIGHT) {
    console.error(`og-card.png is ${width}x${height}, expected ${WIDTH}x${HEIGHT}`);
    process.exitCode = 1;
    return;
  }
  console.log(`Rendered ${path.relative(root, outputPath)} at ${width}x${height} (${header.length} bytes)`);
})();
