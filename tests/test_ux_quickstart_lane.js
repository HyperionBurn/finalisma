import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(import.meta.dirname, '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

test('quickstart states the hosted/self-hosted tool boundary and bridge prerequisite', () => {
  const html = read('site/docs/quickstart.html');

  assert.match(html, /12 MCP tools on POST \/mcp/);
  assert.doesNotMatch(html, /\b9 MCP tools\b|No local install|&lt;absolute-path-to-repo&gt;/);
  assert.match(html, /Python 3\.11\+/);
  assert.match(html, /Install the <code>weft-mcp<\/code> package/);
  assert.match(html, /python -m venv \.venv/);
  assert.match(html, /verify-package-install\.py/);
  assert.match(html, /absolute interpreter path/);
  assert.match(html, /"-B",\s*"-m",\s*"weft_mcp"/);
  assert.match(html, /"PYTHONUTF8":\s*"1"/);
  assert.match(html, /full <strong>59-tool<\/strong> surface/);

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

  for (const document of [connectSource, renderedBridge]) {
    assert.match(
      document,
      /coordinator_url=["']http:\/\/127\.0\.0\.1:8787\/mcp["']|coordinator_url=&(?:#34;|quot;)http:\/\/127\.0\.0\.1:8787\/mcp(?:&#34;|&quot;)/,
      'the SDK example must target the CLI default coordinator port'
    );
    assert.doesNotMatch(document, /127\.0\.0\.1:18787/, 'the SDK example must not ship a stale coordinator port');
  }

  const hostedTools = [
    'room_create', 'room_join', 'room_send', 'room_receipts', 'room_poll',
    'room_wait', 'room_info', 'room_ack', 'room_heartbeat',
    'room_remove_member', 'room_event_log', 'room_leave'
  ];
  assert.equal(
    hostedTools.filter((name) => html.includes(`<code>${name}</code>`)).length,
    hostedTools.length,
    'the quickstart should name all 12 hosted tools'
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

test('branded 404 exposes the global skip link target', () => {
  const html = read('site/404.html');

  assert.match(html, /<a class="skip-link" href="#main">Skip to content<\/a>/);
  assert.match(html, /<main id="main"[^>]*aria-label="Page not found"/);
});
