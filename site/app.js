(() => {
  'use strict';

  const root = document.documentElement;
  root.classList.add('js');

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- reading progress rail ---------- */
  const progressBar = document.querySelector('[data-progress]');
  if (progressBar && 'requestAnimationFrame' in window) {
    let frame = 0;
    const write = () => {
      frame = 0;
      const scrollable = root.scrollHeight - root.clientHeight;
      const pct = scrollable > 0 ? (root.scrollTop / scrollable) * 100 : 0;
      progressBar.style.width = pct.toFixed(2) + '%';
    };
    const queue = () => { if (!frame) frame = window.requestAnimationFrame(write); };
    document.addEventListener('scroll', queue, { passive: true });
    window.addEventListener('resize', queue, { passive: true });
    write();
  }

  /* ---------- cover reveal ----------
     Only the cover is ever visually gated, and the cover is above the fold at
     load, so a slow or failed observer can never leave real content hidden.
     The timeout is the belt-and-braces on top of that. */
  const revealables = document.querySelectorAll('.cover .reveal');
  const revealAll = () => revealables.forEach((element) => element.classList.add('in'));
  if (reduceMotion || !('IntersectionObserver' in window)) {
    revealAll();
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('in');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0 });
    revealables.forEach((element) => observer.observe(element));
    window.setTimeout(revealAll, 900);
  }

  /* ---------- copy to clipboard, with a visible fallback ---------- */
  const copyStatus = document.getElementById('copy-status');
  document.querySelectorAll('[data-copy-target]').forEach((button) => {
    button.addEventListener('click', async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      const original = button.textContent;
      const say = (message) => { if (copyStatus) copyStatus.textContent = message; };
      try {
        await navigator.clipboard.writeText(target.innerText);
        button.textContent = 'Copied';
        say('Configuration copied to the clipboard.');
      } catch (_) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(target);
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = 'Selected';
        say('Configuration selected — press Ctrl or Cmd + C to copy.');
        window.setTimeout(() => selection.removeAllRanges(), 3000);
      }
      window.setTimeout(() => { button.textContent = original; }, 1600);
    });
  });

  /* ---------- portable design-partner application ----------
     Nothing is transmitted from this page. The application is assembled in the
     browser and handed to the visitor to send through their own channel. */
  const cohortForm = document.querySelector('[data-cohort-form]');
  const cohortStatus = cohortForm ? cohortForm.querySelector('[data-cohort-status]') : null;

  if (cohortForm) {
    cohortForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!cohortForm.checkValidity()) {
        cohortForm.reportValidity();
        return;
      }
      const values = Object.fromEntries(new FormData(cohortForm).entries());
      const application = [
        'FINALISMA DESIGN-PARTNER APPLICATION',
        '',
        `Team: ${values.team}`,
        `Contact: ${values.contact}`,
        `Agent hosts: ${values.hosts}`,
        `Incident mirror: ${values.scenario}`,
        '',
        'Boundary: single node, trusted network, non-production data.',
        'Pricing: free tier; Pro and Enterprise by conversation (no checkout yet).',
        `Prepared from: ${window.location.href.split('#')[0]}`
      ].join('\n');

      try {
        if (navigator.share) {
          await navigator.share({ title: 'Finalisma design-partner application', text: application });
          if (cohortStatus) cohortStatus.textContent = 'Application shared. Nothing was sent to Finalisma automatically.';
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(application);
          if (cohortStatus) cohortStatus.textContent = 'Application copied. Paste it into the channel that brought you here.';
          return;
        }
      } catch (error) {
        if (error && error.name === 'AbortError') {
          if (cohortStatus) cohortStatus.textContent = 'Share cancelled. Your application remains local.';
          return;
        }
      }

      const blob = new Blob([application], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'finalisma-design-partner-application.txt';
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      if (cohortStatus) cohortStatus.textContent = 'Application downloaded. Share the text file through your preferred channel.';
    });
  }

  /* ---------- mobile navigation ---------- */
  const navToggle = document.querySelector('.nav-toggle');
  const mobileNav = document.getElementById('mobile-nav');
  const mobileClose = mobileNav ? mobileNav.querySelector('.mobile-close') : null;

  const setNav = (open) => {
    if (!mobileNav || !navToggle) return;
    mobileNav.classList.toggle('open', open);
    mobileNav.setAttribute('aria-hidden', String(!open));
    navToggle.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('nav-open', open);
    if (open) {
      const first = mobileNav.querySelector('a');
      if (first) first.focus();
    } else {
      navToggle.focus();
    }
  };

  if (navToggle && mobileNav) {
    navToggle.addEventListener('click', () => setNav(!mobileNav.classList.contains('open')));
    if (mobileClose) mobileClose.addEventListener('click', () => setNav(false));
    mobileNav.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => setNav(false)));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && mobileNav.classList.contains('open')) setNav(false);
      if (event.key !== 'Tab' || !mobileNav.classList.contains('open')) return;
      const focusable = [...mobileNav.querySelectorAll('a, button')].filter((element) => !element.hasAttribute('disabled'));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  /* ---------- mount the Fig. 1 proof engine ----------
     Both scripts are `defer`, so they execute in document order before
     DOMContentLoaded — by the time this listener runs, proof-engine.js has
     defined its global if it loaded at all. The plate only reveals its stage
     once the engine reports a successful mount; otherwise the written
     sequence stays visible and nothing looks broken. */
  document.addEventListener('DOMContentLoaded', () => {
    const plate = document.getElementById('reconciliation');
    const canvas = document.querySelector('[data-proof-canvas]');
    const readout = document.querySelector('[data-proof-readout]');
    const replayButton = document.querySelector('[data-proof-replay]');
    const rejectButton = document.querySelector('[data-proof-reject]');
    if (!plate || !canvas || !window.FinalismaProof) return;

    let engine = null;
    try {
      engine = window.FinalismaProof.mount(canvas, {
        reducedMotion: reduceMotion,
        onPhase: (label) => { if (readout) readout.textContent = label; }
      });
    } catch (error) {
      return; // static sequence stays visible
    }
    if (!engine) return;

    plate.classList.add('is-ready');
    if (readout) readout.textContent = reduceMotion ? 'Static sequence' : 'Playing';

    if (replayButton && typeof engine.replay === 'function') {
      replayButton.addEventListener('click', () => engine.replay());
    }
    if (rejectButton && typeof engine.jumpToRejection === 'function') {
      rejectButton.addEventListener('click', () => engine.jumpToRejection());
    }
  });
})();
