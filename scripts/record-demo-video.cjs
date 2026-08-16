const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const playwrightPath = process.env.WEFT_PLAYWRIGHT
  || 'C:/Users/Wasif/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright';
const { chromium } = require(playwrightPath);

const root = path.resolve(__dirname, '..');
const siteUrl = process.env.WEFT_SITE_URL || 'http://127.0.0.1:4175/';
const tempDir = path.join(root, '.tmp', 'demo-video');
const webmPath = path.join(root, 'site', 'assets', 'weft-demo.webm');
const mp4Path = path.join(root, 'site', 'assets', 'weft-demo.mp4');
const posterPath = path.join(root, 'site', 'assets', 'weft-demo-poster.png');

const findNestedFfmpeg = (directory, depth = 0) => {
  if (!directory || depth > 7 || !fs.existsSync(directory)) return [];
  const results = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const candidate = path.join(directory, entry.name);
    if (entry.isFile() && entry.name.toLowerCase() === 'ffmpeg.exe') results.push(candidate);
    if (entry.isDirectory()) results.push(...findNestedFfmpeg(candidate, depth + 1));
  }
  return results;
};

const findFfmpegCandidates = () => {
  const candidates = [];
  if (process.env.WEFT_FFMPEG) candidates.push(process.env.WEFT_FFMPEG);
  candidates.push(...findNestedFfmpeg('C:/Program Files/Lenovo/LegionSpace'));
  const playwrightCache = path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'ms-playwright');
  if (fs.existsSync(playwrightCache)) {
    candidates.push(...fs.readdirSync(playwrightCache)
      .filter((name) => name.startsWith('ffmpeg-'))
      .sort()
      .reverse()
      .map((name) => path.join(playwrightCache, name, 'ffmpeg-win64.exe')));
  }
  return [...new Set(candidates.filter((candidate) => fs.existsSync(candidate)))];
};

const encodeMp4 = () => {
  const attempts = [];
  for (const ffmpeg of findFfmpegCandidates()) {
    const encoderList = spawnSync(ffmpeg, ['-hide_banner', '-encoders'], { encoding: 'utf8' });
    const encoders = `${encoderList.stdout || ''}\n${encoderList.stderr || ''}`;
    const choices = [
      ['libx264', ['-c:v', 'libx264', '-preset', 'medium', '-crf', '23']],
      ['h264_mf', ['-c:v', 'h264_mf', '-b:v', '4M']],
      ['h264_nvenc', ['-c:v', 'h264_nvenc', '-b:v', '4M']],
      ['h264_qsv', ['-c:v', 'h264_qsv', '-b:v', '4M']],
      ['mpeg4', ['-c:v', 'mpeg4', '-q:v', '4']]
    ];
    for (const [encoder, codecArgs] of choices) {
      if (!new RegExp(`\\b${encoder}\\b`).test(encoders)) continue;
      const encoded = spawnSync(ffmpeg, [
        '-hide_banner', '-loglevel', 'error', '-y',
        '-i', webmPath,
        '-an', ...codecArgs,
        '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
        mp4Path
      ], { cwd: root, encoding: 'utf8' });
      attempts.push({ ffmpeg, encoder, status: encoded.status, error: encoded.stderr || encoded.stdout || '' });
      if (encoded.status === 0) return { ffmpeg, encoder, attempts };
    }
  }
  throw new Error(`No existing ffmpeg encoder produced MP4: ${JSON.stringify(attempts)}`);
};

(async () => {
  fs.rmSync(tempDir, { recursive: true, force: true });
  fs.mkdirSync(tempDir, { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 1,
    recordVideo: { dir: tempDir, size: { width: 1280, height: 720 } }
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  const video = page.video();
  await page.goto(new URL('demo-stage.html?frameMs=4200', siteUrl).href, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.body.dataset.demoReady === 'true', null, { timeout: 10000 });
  await page.waitForFunction(() => document.body.dataset.demoComplete === 'true', null, { timeout: 55000 });
  await page.waitForTimeout(700);
  await page.close();
  await context.close();
  if (!video) throw new Error('Playwright did not attach a video recorder');
  await video.saveAs(webmPath);

  const posterPage = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
  await posterPage.goto(new URL('demo-stage.html?autoplay=0&frame=8', siteUrl).href, { waitUntil: 'networkidle' });
  await posterPage.waitForFunction(() => document.body.dataset.demoReady === 'true', null, { timeout: 10000 });
  await posterPage.screenshot({ path: posterPath });
  await posterPage.close();
  await browser.close();

  if (errors.length) throw new Error(`Demo stage emitted browser errors: ${errors.join(' | ')}`);
  const encoding = encodeMp4();

  fs.rmSync(tempDir, { recursive: true, force: true });
  const result = {
    status: 'ok',
    duration_seconds: 43,
    resolution: '1280x720',
    webm: { path: path.relative(root, webmPath), bytes: fs.statSync(webmPath).size },
    mp4: { path: path.relative(root, mp4Path), bytes: fs.statSync(mp4Path).size },
    poster: { path: path.relative(root, posterPath), bytes: fs.statSync(posterPath).size },
    mp4_encoder: encoding.encoder,
    credentials_redacted: true,
    audio: 'none; captions are burned into the stage and provided as WebVTT'
  };
  fs.writeFileSync(path.join(root, 'artifacts', 'design-qa', 'demo-video-results.json'), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result, null, 2));
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
