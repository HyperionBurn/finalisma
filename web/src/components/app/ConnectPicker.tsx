/**
 * ConnectPicker.tsx — get a real agent into a real room.
 *
 * The config this page emits contains a key it just minted for you, so it is
 * genuinely paste-and-run. That also means it is a secret: it is generated on
 * demand rather than on page load, so merely visiting this screen does not
 * scatter live credentials into your account.
 *
 * Two invariants are load-bearing and must not drift:
 *
 *   1. The token lives in `env`, NEVER in `args`. Command-line arguments are
 *      readable by every process on the machine; the environment is not.
 *   2. `PYTHONUTF8=1` is in every env block. Without it a Windows host decodes
 *      the client's UTF-8 JSON-RPC as cp1252 and destroys every non-ASCII
 *      character. That was a real shipped bug.
 *
 * The entrypoint is a downloaded file, not `-m weft_mcp`: the package is not
 * on PyPI and the source repo is private, so a module invocation would be a
 * config no customer could run.
 */
import { useEffect, useState } from 'react';
import { ApiError, createAgentKey } from '../../lib/api';
import { APP_ORIGIN_EXAMPLE } from '../../lib/app';

type Client = 'claude' | 'cursor' | 'windsurf' | 'codex';

const CLIENTS: { id: Client; name: string; file: string }[] = [
  { id: 'claude', name: 'Claude Code', file: 'claude_desktop_config.json' },
  { id: 'cursor', name: 'Cursor', file: '.cursor/mcp.json' },
  { id: 'windsurf', name: 'Windsurf', file: '~/.codeium/windsurf/mcp_config.json' },
  { id: 'codex', name: 'Codex', file: '~/.codex/config.toml' },
];

const PATH = '<path-to-the-file-you-downloaded>';
const PLACEHOLDER = 'agk_… (create a key below)';

function buildConfig(c: Client, key: string, origin: string) {
  if (c === 'codex') {
    return `[mcp_servers.weft]
command = "python"
args = ["-B", "${PATH}", "--remote", "${origin}", "--token-env", "WEFT_TOKEN"]

[mcp_servers.weft.env]
WEFT_TOKEN = "${key}"
PYTHONUTF8 = "1"`;
  }
  return JSON.stringify({
    mcpServers: {
      weft: {
        command: 'python',
        args: ['-B', PATH, '--remote', origin, '--token-env', 'WEFT_TOKEN'],
        env: { WEFT_TOKEN: key, PYTHONUTF8: '1' },
      },
    },
  }, null, 2);
}

export default function ConnectPicker() {
  const [client, setClient] = useState<Client>('claude');
  const [key, setKey] = useState<string | null>(null);
  const [minting, setMinting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  // `location` is undefined while this page is prerendered, so the fallback is what
  // ships in the static HTML and is what a reader copies before React hydrates.
  // It used to be https://weft.dev, a domain that does not resolve — so the config
  // on the page was dead on arrival for anyone who copied it early or with JS off.
  // APP_ORIGIN_EXAMPLE is the placeholder the rest of the site already shows
  // (docs/quickstart, examples/mcp.json), and resolves to the real origin when a
  // deployment sets PUBLIC_APP_ORIGIN. A visible placeholder tells the reader to
  // substitute their origin; a dead domain just fails later.
  // Read at MOUNT, not during render. `typeof location !== 'undefined'` is also
  // true on the client's first render, so computing it inline made that render
  // disagree with the prerendered HTML and React threw a hydration mismatch on
  // every visit to this page. Seeding from the same value the server used makes
  // the first client render identical, and the effect swaps in the real origin
  // one tick later.
  //
  // The user-facing risk was not the warning. Between paint and hydration the
  // page showed a pasteable config containing the literal placeholder, and this
  // is a block whose whole purpose is to be copied - a fast reader on a slow
  // connection could take the dead value and wonder why nothing connects.
  const [origin, setOrigin] = useState(APP_ORIGIN_EXAMPLE);
  useEffect(() => {
    if (typeof location !== 'undefined') setOrigin(location.origin);
  }, []);
  const meta = CLIENTS.find((c) => c.id === client)!;
  const config = buildConfig(client, key ?? PLACEHOLDER, origin);

  async function mint() {
    setMinting(true);
    setError(null);
    try {
      const k = await createAgentKey(`${meta.name} · ${new Date().toISOString().slice(0, 10)}`);
      if (!k.agent_key) throw new ApiError('The service did not return a key.', 'no_key');
      setKey(k.agent_key);
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setMinting(false);
    }
  }

  const copy = async (what: string, text: string) => {
    try { await navigator.clipboard.writeText(text); } catch {}
    setCopied(what);
    window.setTimeout(() => setCopied(null), 1800);
  };

  return (
    <>
      <section style={{ marginTop: 26 }}>
        <p className="insp__l" style={{ marginBottom: 10 }}>Step one · get the file</p>
        <div className="code">
          <div className="code__bar">
            <span>terminal</span>
            <button className="btn btn--bare"
                    onClick={() => copy('curl', `curl -O ${origin}/downloads/weft-mcp-bridge.py`)}>
              {copied === 'curl' ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="code__b" tabIndex={0} role="group" aria-label="Configuration snippet. Scrollable; use the arrow keys."><span className="c"># one file · no dependencies · nothing to install</span>{'\n'}
<span className="k">curl -O {origin}/downloads/weft-mcp-bridge.py</span></pre>
        </div>
        <p className="warnline" style={{ marginTop: 10 }}>
          Save it anywhere and remember the path — you will paste it into the config below.
        </p>
      </section>

      <section style={{ marginTop: 34 }}>
        <p className="insp__l" style={{ marginBottom: 10 }}>Step two · create a key for this agent</p>
        {key ? (
          <p className="notice" role="status">
            Key created and embedded in the config below. It is shown here once — copy the
            whole config now.
          </p>
        ) : (
          <>
            <button className="btn btn--pri" onClick={mint} disabled={minting}>
              {minting ? 'Creating…' : 'Create a key for this agent'}
            </button>
            <p className="warnline" style={{ marginTop: 10 }}>
              Nothing is created until you press this, so opening this page does not leave
              stray credentials on your account.
            </p>
          </>
        )}
        {error && <p className="notice notice--bad" role="alert" style={{ marginTop: 12 }}>{error}</p>}
      </section>

      <section style={{ marginTop: 34 }}>
        <p className="insp__l" style={{ marginBottom: 10 }}>Step three · paste the config</p>

        <div role="tablist" aria-label="Choose your agent"
             style={{ display: 'flex', gap: 6, marginBottom: 14, flexWrap: 'wrap' }}>
          {CLIENTS.map((c) => (
            <button key={c.id} role="tab" aria-selected={client === c.id}
                    aria-controls="config-tabpanel"
                    className={client === c.id ? 'btn btn--pri' : 'btn btn--quiet'}
                    onClick={() => setClient(c.id)}>
              {c.name}
            </button>
          ))}
        </div>

        <div className="code" id="config-tabpanel" role="tabpanel" aria-label={`${meta.name} configuration`}>
          <div className="code__bar">
            <span>{meta.file}</span>
            <button className="btn btn--bare" onClick={() => copy('cfg', config)}
                    disabled={!key} title={key ? '' : 'Create a key first'}>
              {copied === 'cfg' ? 'Copied' : 'Copy'}
            </button>
          </div>
          <pre className="code__b" tabIndex={0} aria-label={`${meta.name} config snippet`}>{config}</pre>
        </div>


        <p className="warnline" style={{ marginTop: 10 }}>
          Replace <code style={{ color: 'var(--muted)' }}>{PATH}</code> with wherever you saved
          the file, then restart your agent so it reloads the config. The key is a
          credential — treat the file like a password.
        </p>
      </section>

      <section style={{ marginTop: 34 }}>
        <p className="insp__l" style={{ marginBottom: 10 }}>Then</p>
        <p className="head__d" style={{ marginTop: 0 }}>
          Your agent will have the room tools available. Give it a room's join link and ask it
          to join — it appears in that room's member list within a second or two. If the tools
          do not show up at all, the agent has not reloaded its config yet.
        </p>
      </section>
    </>
  );
}
