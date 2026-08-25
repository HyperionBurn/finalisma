import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

test('quickstart states the hosted/self-hosted tool boundary and bridge prerequisite', () => {
  const html = read('site/docs/quickstart.html');

  assert.match(html, /14 MCP tools on POST \/mcp/);
  assert.doesNotMatch(html, /\b9 MCP tools\b|No local install|&lt;absolute-path-to-repo&gt;/);
  assert.match(html, /standalone MCP bridge/);
  assert.match(html, /No package installation or source checkout is required/);
  assert.match(html, /&lt;path-to-downloaded-weft-mcp-bridge\.py&gt;/);
  assert.match(html, /absolute interpreter path/);
  assert.match(html, /"PYTHONUTF8":\s*"1"/);
  assert.match(html, /full <strong>59-tool<\/strong> surface/);
  assert.match(html, /OpenCode has two native config contracts/);
  assert.match(html, /mcp\.servers\.weft/);
  assert.match(html, /mcp\.weft/);
  assert.match(html, /Required creator-key step/);
  assert.match(html, /creating key must redeem the returned link with <code>room_join<\/code>/);
  assert.match(html, /The key is not auto-joined by <code>room_create<\/code>/);
  assert.doesNotMatch(html, /"org_name"\s*:/);

  const example = JSON.parse(read('site/examples/mcp.json'));
  assert.equal(example.mcpServers.weft.env.PYTHONUTF8, '1');
  assert.deepEqual(
    example.mcpServers.weft.args.slice(0, 3),
    ['-B', '-m', 'weft_mcp'],
    'the downloadable config must use the supported module entry point'
  );
  assert.equal(
    example.mcpServers.weft.args.some((arg) => /ABSOLUTE|weft-mcp\.py/.test(arg)),
    false,
    'the downloadable config must not contain a machine-specific script path'
  );
  assert.match(read('site/index.html'), /PYTHONUTF8/);

  const connectSource = read('web/src/components/ConnectTiers.astro');
  const renderedBridge = read('site/index.html')
    .replaceAll('&#34;', '"')
    .replaceAll('&quot;', '"');
  for (const document of [connectSource, renderedBridge]) {
    assert.match(document, /ClipboardBridge\(store\)/);
    assert.match(document, /team_id=["']demo["']/);
    assert.match(document, /agent_id=["']agent-a["']/);
    assert.match(document, /endpoint=["']http:\/\/127\.0\.0\.1:8787["']/);
    assert.match(document, /pairing\[?["']pairing_id["']\]?/);
    assert.match(document, /pairing\[?["']join_token["']\]?/);
    assert.doesNotMatch(document, /coordinator_db=|nonce=/);
  }

  assert.match(
    connectSource,
    /coordinator_url="\$\{HOSTED_ORIGIN\}\/mcp"/,
    'the SDK source must target the hosted origin'
  );
  assert.match(
    renderedBridge,
    /coordinator_url="https:\/\/YOUR-VERIFIED-WEFT-ORIGIN\/mcp"/,
    'the checked-in site must keep the hosted origin explicit until verified'
  );
  assert.match(connectSource, /bearer_token=os\.environ\["WEFT_TOKEN"\]/);
  assert.match(renderedBridge, /bearer_token=os\.environ\["WEFT_TOKEN"\]/);
  assert.doesNotMatch(connectSource, /SELF_HOSTED_SDK_CODE/);
  assert.doesNotMatch(renderedBridge, /coordinator_url="http:\/\/127\.0\.0\.1:8787\/mcp"/);
  const sdkPanelStart = renderedBridge.indexOf('id="tier-panel-sdk"');
  const sdkCopyStart = renderedBridge.indexOf('data-copy="sdk"', sdkPanelStart);
  assert.ok(sdkPanelStart >= 0 && sdkCopyStart > sdkPanelStart, 'the generated SDK panel must exist');
  assert.doesNotMatch(
    renderedBridge.slice(sdkPanelStart, sdkCopyStart),
    /actor_token=/,
    'the generated SDK panel must not teach self-hosted actor credentials'
  );

  const hostedTools = [
    'room_create', 'room_join', 'room_send', 'room_receipts', 'room_poll',
    'room_wait', 'room_info', 'room_ack', 'room_heartbeat',
    'room_remove_member', 'room_close', 'room_event_log', 'room_leave', 'room_list'
  ];
  assert.equal(
    hostedTools.filter((name) => html.includes(`<code>${name}</code>`)).length,
    hostedTools.length,
    'the quickstart should name all 14 hosted tools'
  );
});

test('small-screen code guidance is focusable without introducing overflow', () => {
  const html = read('site/docs/quickstart.html');
  const css = read('site/styles.css');
  const codeBlocks = [...html.matchAll(/<pre class="article-code"[^>]*>/g)];

  assert.match(html, /id="code-scroll-note"/);
  assert.ok(codeBlocks.length > 0);
  assert.equal(
    codeBlocks.filter((match) => /tabindex="0"/.test(match[0]) && /aria-describedby="code-scroll-note"/.test(match[0])).length,
    codeBlocks.length,
    'every code block should expose the scroll guidance to keyboard and assistive-technology users'
  );
  assert.match(css, /\.article-code\s*\{[^}]*overflow-x:\s*auto/s);
});

test('compatibility tier note uses readable paper ink', () => {
  const css = read('site/styles.css');
  assert.match(css, /\.fig-note\s*\{[^}]*color:\s*var\(--ink-faint\)/s);
});

test('branded 404 exposes the global skip link target', () => {
  const html = read('site/404.html');

  assert.match(html, /<a class="skip-link" href="#main">Skip to content<\/a>/);
  assert.match(html, /<main id="main"[^>]*aria-label="Page not found"/);
});

test('compatibility metadata matches the nine documented paths shown in the page', () => {
  const html = read('site/docs/compatibility.html');
  assert.match(html, /nine documented MCP host paths/);
  assert.doesNotMatch(html, /eight documented MCP host paths/);
});
