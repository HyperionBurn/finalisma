(() => {
  'use strict';

  const query = new URLSearchParams(location.search);
  const autoplay = query.get('autoplay') !== '0';
  const requestedFrame = Number.parseInt(query.get('frame') || '0', 10);
  const requestedFrameMs = Number.parseInt(query.get('frameMs') || '4200', 10);
  const frameMs = Number.isFinite(requestedFrameMs) ? Math.max(800, Math.min(10000, requestedFrameMs)) : 4200;
  const stage = document.querySelector('[data-demo-stage]');

  const fields = {
    index: document.querySelector('[data-frame-index]'),
    eyebrow: document.querySelector('[data-frame-eyebrow]'),
    headline: document.querySelector('[data-frame-headline]'),
    caption: document.querySelector('[data-frame-caption]'),
    agentA: document.querySelector('[data-agent-a]'),
    agentB: document.querySelector('[data-agent-b]'),
    event: document.querySelector('[data-frame-event]'),
    accountState: document.querySelector('[data-account-state]'),
    accountEvent: document.querySelector('[data-account-event]'),
    accountProof: document.querySelector('[data-account-proof]'),
    progressBar: document.querySelector('[data-progress-bar]'),
    progressLabel: document.querySelector('[data-progress-label]'),
    proofPairing: document.querySelector('[data-proof-pairing]'),
    proofCursor: document.querySelector('[data-proof-cursor]'),
    proofEvidence: document.querySelector('[data-proof-evidence]'),
    proofTask: document.querySelector('[data-proof-task]')
  };

  const formatClock = (milliseconds) => {
    const seconds = Math.floor(milliseconds / 1000);
    return `00:${String(seconds).padStart(2, '0')}`;
  };

  fetch('assets/demo-transcript.json', { cache: 'no-store' })
    .then((response) => {
      if (!response.ok) throw new Error(`Transcript returned HTTP ${response.status}`);
      return response.json();
    })
    .then((transcript) => {
      const frames = transcript.frames;
      const proof = transcript.proof;
      const totalMs = frames.length * frameMs;
      let current = Math.max(0, Math.min(frames.length - 1, Number.isFinite(requestedFrame) ? requestedFrame : 0));
      let startedAt = performance.now() - (current * frameMs);

      fields.proofPairing.textContent = proof.pairing_status;
      fields.proofCursor.textContent = String(proof.last_ack_seq);
      fields.proofEvidence.textContent = proof.evidence_passed ? 'passed' : 'open';
      fields.proofTask.textContent = proof.task_status;

      const paint = (index) => {
        current = index;
        const frame = frames[index];
        document.body.dataset.state = frame.state;
        fields.index.textContent = `${String(index).padStart(2, '0')} / ${String(frames.length - 1).padStart(2, '0')}`;
        fields.eyebrow.textContent = frame.eyebrow;
        fields.headline.textContent = frame.headline;
        fields.caption.textContent = frame.caption;
        fields.agentA.textContent = frame.agent_a;
        fields.agentB.textContent = frame.agent_b;
        fields.event.textContent = frame.event;
        fields.accountEvent.textContent = frame.event;
        const postedTotal = frames.length - 2;
        const postedCount = Math.max(0, Math.min(index, postedTotal));
        fields.accountProof.textContent = `${postedCount} / ${postedTotal} posted`;
        fields.accountState.textContent = frame.state === 'balanced' ? 'Account balanced' : frame.state === 'posted' ? 'Posting evidence' : 'Out of balance';
        fields.progressBar.style.setProperty('--progress', `${((index + 1) / frames.length) * 100}%`);
        fields.progressBar.style.width = `${((index + 1) / frames.length) * 100}%`;
        stage.classList.remove('is-changing');
        void stage.offsetWidth;
        stage.classList.add('is-changing');
        document.body.dataset.demoFrame = String(index);
      };

      paint(current);
      document.body.dataset.demoReady = 'true';

      if (!autoplay) {
        fields.progressLabel.textContent = `Recorded proof · poster frame ${current + 1}`;
        return;
      }

      const tick = () => {
        const elapsed = performance.now() - startedAt;
        const next = Math.min(frames.length - 1, Math.floor(elapsed / frameMs));
        if (next !== current) paint(next);
        fields.progressLabel.textContent = `Recorded proof · ${formatClock(Math.min(elapsed, totalMs))} / ${formatClock(totalMs)}`;
        if (elapsed >= totalMs) {
          document.body.dataset.demoComplete = 'true';
          fields.progressLabel.textContent = `Recorded proof · ${formatClock(totalMs)} / ${formatClock(totalMs)}`;
          window.dispatchEvent(new CustomEvent('weft:demo-complete'));
          return;
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    })
    .catch((error) => {
      fields.eyebrow.textContent = 'Transcript unavailable';
      fields.headline.textContent = 'The recorded proof could not load.';
      fields.caption.textContent = String(error);
      document.body.dataset.demoError = 'true';
    });
})();
