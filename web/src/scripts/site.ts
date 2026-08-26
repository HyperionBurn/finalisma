/**
 * site.ts — the client runtime for the landing page.
 *
 * One module owns every motion concern so timing stays coherent: Lenis
 * drives the scroll, GSAP's ticker drives Lenis, and ScrollTrigger reads
 * from the same clock. Three independent rAF loops fighting each other is
 * the usual cause of "smooth scroll feels slightly wrong".
 *
 * Everything here degrades. `prefers-reduced-motion` short-circuits the
 * whole file: content is revealed immediately, Lenis never starts, and the
 * page becomes an ordinary document. Nothing is hidden by CSS unless the
 * `.js` class is present, so a failed script leaves a readable page rather
 * than a blank one.
 */
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import Lenis from 'lenis';

gsap.registerPlugin(ScrollTrigger);

const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const EASE = 'power3.out';

/* ── smooth scroll ──────────────────────────────────────────────── */
function initSmoothScroll() {
  if (REDUCED) return null;

  const lenis = new Lenis({
    duration: 1.05,
    easing: (t: number) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
    smoothWheel: true,
    touchMultiplier: 1.6,
  });

  // One clock. Lenis is ticked by GSAP so ScrollTrigger never reads a
  // scroll position that is half a frame stale.
  lenis.on('scroll', ScrollTrigger.update);
  gsap.ticker.add((time) => lenis.raf(time * 1000));
  gsap.ticker.lagSmoothing(0);

  document.querySelectorAll<HTMLAnchorElement>('a[href^="#"]').forEach((a) => {
    a.addEventListener('click', (e) => {
      const id = a.getAttribute('href');
      if (!id || id === '#') return;
      const target = document.querySelector(id);
      if (!target) return;
      e.preventDefault();
      lenis.scrollTo(target as HTMLElement, { offset: 0, duration: 1.15 });
    });
  });

  return lenis;
}

/* ── split text into words for staggered reveal ─────────────────── */
/** Wraps each word in a masked span so lines rise from behind an edge. */
function splitWords(el: HTMLElement) {
  if (el.dataset.split === 'done') return Array.from(el.querySelectorAll('.w__i'));
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  const texts: Text[] = [];
  let n: Node | null;
  while ((n = walker.nextNode())) if (n.textContent?.trim()) texts.push(n as Text);

  texts.forEach((node) => {
    const frag = document.createDocumentFragment();
    node.textContent!.split(/(\s+)/).forEach((chunk) => {
      if (!chunk.trim()) { frag.appendChild(document.createTextNode(chunk)); return; }
      const outer = document.createElement('span');
      outer.className = 'w__o';
      const inner = document.createElement('span');
      inner.className = 'w__i';
      inner.textContent = chunk;
      outer.appendChild(inner);
      frag.appendChild(outer);
    });
    node.replaceWith(frag);
  });
  el.dataset.split = 'done';
  return Array.from(el.querySelectorAll('.w__i'));
}

function initTextReveals() {
  document.querySelectorAll<HTMLElement>('[data-split]').forEach((el) => {
    const words = splitWords(el);
    if (REDUCED) { gsap.set(words, { yPercent: 0, opacity: 1 }); return; }
    gsap.set(words, { yPercent: 118, opacity: 0 });
    ScrollTrigger.create({
      trigger: el,
      start: 'top 86%',
      once: true,
      onEnter: () =>
        gsap.to(words, {
          yPercent: 0, opacity: 1, duration: 1.05, ease: EASE,
          stagger: { each: 0.028, from: 'start' },
        }),
    });
  });
}

/* ── generic reveal ─────────────────────────────────────────────── */
function initReveals() {
  document.querySelectorAll<HTMLElement>('[data-rise]').forEach((el) => {
    if (REDUCED) { el.classList.add('is-in'); return; }
    ScrollTrigger.create({
      trigger: el,
      start: 'top 88%',
      once: true,
      onEnter: () => el.classList.add('is-in'),
    });
  });
}

/* ── numbers that count to their value ──────────────────────────── */
function initCounters() {
  document.querySelectorAll<HTMLElement>('[data-count]').forEach((el) => {
    const target = parseFloat(el.dataset.count || '0');
    const suffix = el.dataset.suffix || '';
    if (REDUCED) { el.textContent = target.toLocaleString() + suffix; return; }
    // The markup carries the real value so a scriptless reader sees the truth.
    // Zero it here, once we know JS is running and the count-up will happen —
    // invisible, because `.js [data-rise]` holds these at opacity:0 until reveal.
    el.textContent = (0).toLocaleString() + suffix;
    const obj = { v: 0 };
    ScrollTrigger.create({
      trigger: el,
      start: 'top 90%',
      once: true,
      onEnter: () =>
        gsap.to(obj, {
          v: target, duration: 1.5, ease: 'power2.out',
          onUpdate: () => {
            el.textContent = Math.round(obj.v).toLocaleString() + suffix;
          },
        }),
    });
  });
}

/* ── magnetic pills ─────────────────────────────────────────────── */
/** Subtle: 0.28 of the cursor offset, capped. Enough to feel alive,
 *  not enough to make the button hard to hit. */
function initMagnetic() {
  if (REDUCED || window.matchMedia('(hover: none)').matches) return;
  document.querySelectorAll<HTMLElement>('[data-magnetic]').forEach((el) => {
    const strength = 0.28;
    const max = 9;
    const move = (e: MouseEvent) => {
      const r = el.getBoundingClientRect();
      const dx = gsap.utils.clamp(-max, max, (e.clientX - (r.left + r.width / 2)) * strength);
      const dy = gsap.utils.clamp(-max, max, (e.clientY - (r.top + r.height / 2)) * strength);
      gsap.to(el, { x: dx, y: dy, duration: 0.5, ease: 'power3.out' });
    };
    const reset = () => gsap.to(el, { x: 0, y: 0, duration: 0.7, ease: 'elastic.out(1, 0.4)' });
    el.addEventListener('mousemove', move);
    el.addEventListener('mouseleave', reset);
  });
}

/* ── hero: the film recedes as you leave it ─────────────────────── */
function initHero() {
  if (REDUCED) return;
  const stage = document.querySelector('.stage');
  const video = document.querySelector('.plate-video');
  const copy = document.querySelector('.hero');
  const hosts = document.querySelector('.hosts');
  if (!stage || !video) return;

  gsap.timeline({
    scrollTrigger: { trigger: stage, start: 'top top', end: 'bottom top', scrub: 0.6 },
  })
    .to(video, { scale: 1.14, yPercent: 6, ease: 'none' }, 0)
    .to(copy, { yPercent: -18, opacity: 0, ease: 'none' }, 0)
    .to(hosts, { opacity: 0, ease: 'none' }, 0);
}

/* ── nav condenses once you leave the hero ──────────────────────── */
function initNav() {
  const nav = document.querySelector('.topbar');
  if (!nav) return;
  ScrollTrigger.create({
    start: 'top -80',
    onUpdate: (self) => nav.classList.toggle('is-stuck', self.scroll() > 80),
  });
}

/* ── infinite host marquee ──────────────────────────────────────── */
function initMarquee() {
  document.querySelectorAll<HTMLElement>('[data-marquee]').forEach((track) => {
    const inner = track.querySelector<HTMLElement>('.mq__row');
    if (!inner) return;
    inner.innerHTML += inner.innerHTML; // duplicate for a seamless wrap
    if (REDUCED) return;
    const loop = gsap.to(inner, {
      xPercent: -50, duration: 34, ease: 'none', repeat: -1,
    });
    track.addEventListener('mouseenter', () => gsap.to(loop, { timeScale: 0.25, duration: 0.5 }));
    track.addEventListener('mouseleave', () => gsap.to(loop, { timeScale: 1, duration: 0.5 }));
  });
}

/* ── land on the right section when arriving with a hash ─────── */
/**
 * Arriving from another page at `/#start` or `/#pricing` used to land the
 * reader thousands of pixels short of the target.
 *
 * The browser resolves the hash while parsing, against a document that has not
 * been laid out yet. Room.tsx then pins `.room__pin` with ScrollTrigger, which
 * inserts a pin spacer and makes the document taller. Every section AFTER the
 * room shifts down by that spacer, so the position the browser already scrolled
 * to is now the wrong one. `#room` itself is the pin, so it survives — which is
 * exactly why two unrelated anchors were observed landing at one identical
 * offset while a third was fine.
 *
 * In-page clicks never hit this: initSmoothScroll intercepts `a[href^="#"]` and
 * hands the scroll to Lenis after layout has settled. Only cross-page arrivals
 * are affected, which is why the docs and blog links were the ones that broke.
 *
 * So re-resolve the hash once the pins exist. getElementById rather than
 * querySelector: a hash is arbitrary user input and need not be a valid selector.
 */
function restoreHashTarget(lenis: Lenis | null) {
  const raw = location.hash.slice(1);
  if (!raw) return;
  let id: string;
  try { id = decodeURIComponent(raw); } catch { id = raw; }
  const target = document.getElementById(id);
  if (!target) return;
  if (lenis) lenis.scrollTo(target, { offset: 0, immediate: true });
  else target.scrollIntoView();
}

/* ── boot ───────────────────────────────────────────────────────── */
function boot() {
  document.documentElement.classList.add('js');
  const lenis = initSmoothScroll();
  initTextReveals();
  initReveals();
  initCounters();
  initMagnetic();
  initHero();
  initNav();
  initMarquee();
  ScrollTrigger.refresh();
  // Pins exist now, so the document is its final height — an inbound hash can
  // finally be resolved against the layout the reader will actually see.
  restoreHashTarget(lenis);

  // Fonts change metrics; recompute trigger positions once they land.
  document.fonts?.ready.then(() => {
    ScrollTrigger.refresh();
    restoreHashTarget(lenis);
  });

  // Safety net: nothing stays invisible, whatever happens above.
  window.setTimeout(() => {
    document.querySelectorAll('[data-rise]').forEach((el) => el.classList.add('is-in'));
    document.querySelectorAll<HTMLElement>('.w__i').forEach((el) => {
      el.style.transform = 'none';
      el.style.opacity = '1';
    });
  }, 4000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}
