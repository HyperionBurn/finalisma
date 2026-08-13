/* Weft Fig. 1 — dependency-free, canvas proof sequence. */
(() => {
  'use strict';

  const phases = [
    ['Pairing link issued', 'A one-use link opens a narrow handoff.'],
    ['Policy previewed', 'The receiver sees scope before agreeing.'],
    ['Consent recorded', 'The agreement becomes part of the account.'],
    ['Scope declared', 'Only the declared workspace can be referenced.'],
    ['Task claimed', 'Lease 0184 owns the work; stale 0183 is refused.'],
    ['Evidence gated', 'Hashes, paths, and secret checks all pass.'],
    ['Handoff returned', 'The result is ordered, replayable, and complete.']
  ];

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  const mount = (canvas, options = {}) => {
    const context = canvas.getContext('2d');
    if (!context) return null;
    const plate = canvas.closest('.plate');
    const keyItems = [...document.querySelectorAll('.keylist li')];
    const reduceMotion = Boolean(options.reducedMotion);
    let width = 0;
    let height = 0;
    let frame = 0;
    let startedAt = 0;
    let progress = reduceMotion ? 1 : 0;
    let destroyed = false;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      width = Math.max(320, rect.width || 960);
      height = Math.max(260, rect.height || 540);
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      draw(progress);
    };

    const signalPhase = (index) => {
      keyItems.forEach((item, itemIndex) => item.classList.toggle('is-live', itemIndex === index));
      if (typeof options.onPhase === 'function') {
        options.onPhase(index >= phases.length - 1 ? 'Handoff complete' : phases[Math.max(0, index)][0]);
      }
    };

    const draw = (value) => {
      if (!width || !height) return;
      const active = clamp(value, 0, 1) * phases.length;
      const current = Math.min(phases.length - 1, Math.floor(active));
      const ink = '#F5F0E4';
      const dim = '#C9BEAE';
      const prove = '#D6A94C';
      const assert = '#E2604C';
      const panel = '#2C1712';
      const left = Math.max(28, width * .08);
      const right = width - Math.max(28, width * .08);
      const top = Math.max(34, height * .12);
      const bottom = height - Math.max(40, height * .13);
      const step = phases.length > 1 ? (right - left) / (phases.length - 1) : 0;

      context.clearRect(0, 0, width, height);
      context.fillStyle = panel;
      context.fillRect(0, 0, width, height);

      context.strokeStyle = 'rgba(245,240,228,.10)';
      context.lineWidth = 1;
      for (let x = left; x <= right; x += Math.max(52, width / 12)) {
        context.beginPath(); context.moveTo(x, top); context.lineTo(x, bottom); context.stroke();
      }
      for (let y = top; y <= bottom; y += Math.max(42, height / 7)) {
        context.beginPath(); context.moveTo(left, y); context.lineTo(right, y); context.stroke();
      }

      context.fillStyle = dim;
      context.font = '600 11px "Big Shoulders Display", Arial Narrow, sans-serif';
      context.letterSpacing = '1.4px';
      context.fillText('FIG. 1  /  ORDERED HANDOFF', left, 24);
      context.fillStyle = prove;
      context.fillText(`${String(Math.round(active)).padStart(2, '0')} / 07 CHECKPOINTS`, right - 128, 24);

      const baseline = top + (bottom - top) * .55;
      context.lineWidth = 2;
      context.strokeStyle = 'rgba(214,169,76,.28)';
      context.beginPath(); context.moveTo(left, baseline); context.lineTo(right, baseline); context.stroke();

      const visible = Math.min(phases.length - 1, Math.floor(active));
      const fractional = active - Math.floor(active);
      const pathEnd = left + step * Math.min(phases.length - 1, active);
      context.strokeStyle = prove;
      context.lineWidth = 4;
      context.beginPath(); context.moveTo(left, baseline); context.lineTo(pathEnd, baseline); context.stroke();

      for (let index = 0; index < phases.length; index += 1) {
        const x = left + step * index;
        const isPast = index < visible || (index === visible && fractional > .18);
        const isCurrent = index === visible;
        context.beginPath();
        context.arc(x, baseline, isCurrent ? 9 : 6, 0, Math.PI * 2);
        context.fillStyle = isPast ? prove : 'rgba(245,240,228,.22)';
        context.fill();
        context.strokeStyle = isCurrent ? ink : 'rgba(245,240,228,.35)';
        context.lineWidth = isCurrent ? 2 : 1;
        context.stroke();

        context.fillStyle = isPast || isCurrent ? ink : dim;
        context.font = `${isCurrent ? 700 : 500} ${Math.max(11, Math.min(15, width / 76))}px "Big Shoulders Display", Arial Narrow, sans-serif`;
        context.fillText(String(index + 1).padStart(2, '0'), x - 8, baseline - 22);
      }

      const labelX = clamp(left + step * visible - 100, left, Math.max(left, right - 210));
      context.fillStyle = ink;
      context.font = 'italic 600 23px Fraunces, Georgia, serif';
      context.fillText(phases[visible][0], labelX, baseline + 56);
      context.fillStyle = dim;
      context.font = '400 14px Fraunces, Georgia, serif';
      const words = phases[visible][1].split(' ');
      let line = '';
      let lineIndex = 0;
      for (const word of words) {
        const candidate = line ? `${line} ${word}` : word;
        if (context.measureText(candidate).width > 230 && line) {
          context.fillText(line, labelX, baseline + 80 + lineIndex * 20);
          line = word;
          lineIndex += 1;
        } else line = candidate;
      }
      if (line) context.fillText(line, labelX, baseline + 80 + lineIndex * 20);

      if (visible >= 4 && active < 6.2) {
        context.strokeStyle = assert;
        context.lineWidth = 2;
        context.setLineDash([6, 5]);
        context.beginPath(); context.moveTo(left + step * 4, baseline - 44); context.lineTo(left + step * 4, baseline + 26); context.stroke();
        context.setLineDash([]);
        context.fillStyle = assert;
        context.font = '700 11px "Big Shoulders Display", Arial Narrow, sans-serif';
        context.fillText('STALE 0183 REFUSED', left + step * 4 - 52, baseline - 58);
      }

      context.fillStyle = visible === phases.length - 1 ? prove : dim;
      context.font = '700 11px "Big Shoulders Display", Arial Narrow, sans-serif';
      context.fillText(visible === phases.length - 1 ? 'EVIDENCE CLOSED' : 'LIVE SEQUENCE', left, bottom + 26);
      context.fillText('SIMULATED HOST FIXTURES · LOCAL COORDINATOR', Math.max(left, right - 246), bottom + 26);
    };

    const tick = (timestamp) => {
      if (destroyed) return;
      if (!startedAt) startedAt = timestamp;
      progress = reduceMotion ? 1 : clamp((timestamp - startedAt) / 7600, 0, 1);
      draw(progress);
      signalPhase(Math.min(phases.length - 1, Math.floor(progress * phases.length)));
      if (progress < 1 && !reduceMotion) frame = requestAnimationFrame(tick);
      else frame = 0;
    };

    const replay = () => {
      if (frame) cancelAnimationFrame(frame);
      startedAt = 0;
      progress = reduceMotion ? 1 : 0;
      keyItems.forEach((item) => item.classList.remove('is-live'));
      if (reduceMotion) {
        draw(progress);
        signalPhase(phases.length - 1);
        if (typeof options.onPhase === 'function') options.onPhase('Static sequence');
        frame = 0;
        return;
      }
      frame = requestAnimationFrame(tick);
    };

    const jumpToRejection = () => {
      if (frame) cancelAnimationFrame(frame);
      progress = 4.55 / phases.length;
      draw(progress);
      signalPhase(4);
      if (typeof options.onPhase === 'function') options.onPhase('Stale lease rejected');
    };

    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    if (observer) observer.observe(canvas);
    else window.addEventListener('resize', resize, { passive: true });
    resize();
    replay();

    return {
      replay,
      jumpToRejection,
      destroy() {
        destroyed = true;
        if (frame) cancelAnimationFrame(frame);
        if (observer) observer.disconnect();
      }
    };
  };

  window.WeftProof = { mount };
})();
