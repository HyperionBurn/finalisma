import { useEffect, useRef } from 'react';

/**
 * ScrollController.tsx — client:only="visible" island.
 * Instantiates Lenis + GSAP ScrollTrigger, drives the WebGL scene,
 * updates beat captions, colour-temperature shifts, and section reveals.
 */

const BEAT_THRESHOLDS = [0.0, 0.12, 0.3, 0.5, 0.66, 0.82, 1.0];
const BEAT_CAPTIONS = [
  'One link.',
  'Many agents join.',
  'They all communicate.',
  'The gate refuses stale work.',
  'Evidence recorded.',
  'Connect your agent.',
];

// Colour temperature per beat: [bg-temp, light-tint]
const BEAT_TEMPS: Record<number, { bg: string; tint: string; theme?: string }> = {
  1: { bg: '#0E0F12', tint: 'neutral' },
  2: { bg: '#0E0F12', tint: 'cool-violet' },
  3: { bg: '#0E0F12', tint: 'neutral' },
  4: { bg: '#2B1818', tint: 'refuse-red' },
  5: { bg: '#0E0F12', tint: 'neutral-cool' },
  6: { bg: '#F7F8FA', tint: 'warm-light', theme: 'light' },
};

export default function ScrollController() {
  const triggersRef = useRef<any[]>([]);
  const lenisRef = useRef<any>(null);

  useEffect(() => {
    let cancelled = false;
    let lenisInstance: any = null;
    let gsapMod: any;
    let ScrollTrigger: any;
    let rafId: number | null = null;

    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    const reducedMotion = mediaQuery.matches;

    async function init() {
      if (cancelled) return;

      // Dynamic imports for client-only
      const Lenis = (await import('lenis')).default;
      gsapMod = await import('gsap');
      const ST = await import('gsap/ScrollTrigger');
      ScrollTrigger = ST.ScrollTrigger;

      if (cancelled) return;

      const gsap = gsapMod.gsap;
      gsap.registerPlugin(ScrollTrigger);

      // Lenis smooth scroll
      lenisInstance = new Lenis({
        duration: 1.2,
        easing: (t: number) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
        smoothWheel: true,
        wheelMultiplier: 1,
        touchMultiplier: 2,
      });
      lenisRef.current = lenisInstance;

      // Canonical Lenis + GSAP integration
      lenisInstance.on('scroll', ScrollTrigger.update);
      gsap.ticker.add((time: number) => lenisInstance.raf(time * 1000));
      gsap.ticker.lagSmoothing(0);

      // Get DOM elements
      const root = document.documentElement;
      const beatCaption = document.querySelector('[data-beat-caption]');
      const rootEl = document.querySelector(':root') as HTMLElement;

      let lastBeat = 1;

      // Helper: update beat caption + data attr
      function updateBeat(beat: number) {
        if (beat === lastBeat) return;
        lastBeat = beat;
        if (beatCaption) {
          beatCaption.textContent = BEAT_CAPTIONS[beat - 1] || BEAT_CAPTIONS[0];
        }
        root.dataset.beat = String(beat);

        // Colour temperature shift
        const temp = BEAT_TEMPS[beat];
        if (temp && rootEl) {
          rootEl.style.setProperty('--bg-temp', temp.bg);
          rootEl.style.setProperty('--light-tint', temp.tint);
          if (temp.theme === 'light') {
            root.setAttribute('data-theme', 'light');
          } else {
            root.setAttribute('data-theme', 'dark');
          }
        }
      }

      // Helper: drive scene
      function driveScene(progress: number) {
        if ((window as any).FinalismaScene?.setProgress) {
          (window as any).FinalismaScene.setProgress(progress);
        }
        // Determine beat from progress
        for (let i = BEAT_THRESHOLDS.length - 2; i >= 0; i--) {
          if (progress >= BEAT_THRESHOLDS[i]) {
            updateBeat(i + 1);
            break;
          }
        }
      }

      // ScrollTrigger for full-page progress
      ScrollTrigger.create({
        trigger: 'body',
        start: 'top top',
        end: 'bottom bottom',
        onUpdate: (self: any) => {
          driveScene(self.progress);
        },
      });

      // Hero pinned state — canvas stays in view while headline scrolls
      const heroEl = document.querySelector('.hero');
      if (heroEl) {
        ScrollTrigger.create({
          trigger: '.hero',
          start: 'top top',
          end: '+=12%',
          pin: false, // Hero is full viewport; natural scroll
          onEnter: () => driveScene(0),
        });
      }

      // Section reveals via ScrollTrigger (not IO for main choreography)
      const sections = document.querySelectorAll('[data-reveal-section]');
      sections.forEach((section: any, idx: number) => {
        const children = section.querySelectorAll('.reveal');
        if (children.length > 0) {
          const t = ScrollTrigger.create({
            trigger: section,
            start: 'top 85%',
            end: 'top 40%',
            onEnter: () => {
              children.forEach((el: Element, i: number) => {
                setTimeout(() => {
                  el.classList.add('in');
                }, i * 80);
              });
            },
            onLeaveBack: () => {
              if (reducedMotion) return;
              children.forEach((el: Element) => {
                el.classList.remove('in');
              });
            },
          });
          triggersRef.current.push(t);
        }
      });

      // Step animations for HowItWorks
      const steps = document.querySelectorAll('[data-step]');
      steps.forEach((step: any, idx: number) => {
        const t = ScrollTrigger.create({
          trigger: step,
          start: 'top 80%',
          onEnter: () => {
            step.classList.add('step--active');
          },
          onLeaveBack: () => {
            if (!reducedMotion) step.classList.remove('step--active');
          },
        });
        triggersRef.current.push(t);
      });

      // Proof items stagger
      const proofItems = document.querySelectorAll('[data-proof-item]');
      proofItems.forEach((item: any, idx: number) => {
        const t = ScrollTrigger.create({
          trigger: item,
          start: 'top 85%',
          onEnter: () => {
            item.classList.add('proof-item--visible');
          },
          onLeaveBack: () => {
            if (!reducedMotion) item.classList.remove('proof-item--visible');
          },
        });
        triggersRef.current.push(t);
      });

      // Initial state
      driveScene(0);
      updateBeat(1);

      // The hero is above the fold and must be VISIBLE immediately — its
      // `.reveal` elements are not scroll-gated (the hero has no
      // data-reveal-section), so force `.in` on mount with a short stagger.
      // This is the design rule: the initial state is visible, animation
      // enhances from there — never the reverse.
      const heroReveals = document.querySelectorAll('.hero .reveal');
      heroReveals.forEach((el: Element, i: number) => {
        setTimeout(() => {
          el.classList.add('in');
        }, 60 + i * 90);
      });
      // Truthful-first counters: the correct figure is rendered in the HTML.
      // A count-up animation would have to start at 0 (wrong) and is
      // therefore prohibited by the "truthful initial state" rule. Instead a
      // brief scale pop enhances the numbers for motion-allowed users without
      // ever displaying an incorrect value; reduced-motion users keep the
      // static correct figure.
      if (!reducedMotion) {
        document.querySelectorAll('[data-count-to]').forEach((el: Element, i: number) => {
          setTimeout(() => el.classList.add('stat-pop'), 300 + i * 80);
        });
      }
      // Belt-and-braces: a hard timeout guarantees the hero is never left
      // invisible even if the RAF/scroll machinery stalls.
      setTimeout(() => {
        if (!cancelled) {
          document.querySelectorAll('.hero .reveal').forEach((el) => el.classList.add('in'));
        }
      }, 1200);
    }

    if (reducedMotion) {
      // Reduced motion: skip Lenis + ScrollTrigger animations, render static
      // Just drive scene to final state
      if ((window as any).FinalismaScene?.setProgress) {
        (window as any).FinalismaScene.setProgress(1);
      }
      // Make all reveals visible immediately
      document.querySelectorAll('.reveal').forEach((el) => {
        el.classList.add('in');
      });
      document.querySelectorAll('[data-step]').forEach((el) => {
        el.classList.add('step--active');
      });
      document.querySelectorAll('[data-proof-item]').forEach((el) => {
        el.classList.add('proof-item--visible');
      });
      // Set beat caption to final
      const beatCaption = document.querySelector('[data-beat-caption]');
      if (beatCaption) beatCaption.textContent = BEAT_CAPTIONS[BEAT_CAPTIONS.length - 1];
      document.documentElement.dataset.beat = '6';
      const rootEl = document.querySelector(':root') as HTMLElement;
      if (rootEl) {
        rootEl.style.setProperty('--bg-temp', '#F7F8FA');
        rootEl.style.setProperty('--light-tint', 'warm-light');
      }
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      init();
    }

    return () => {
      cancelled = true;
      // Kill ScrollTriggers
      triggersRef.current.forEach((t) => t.kill());
      triggersRef.current = [];
      // Destroy Lenis
      if (lenisRef.current) {
        lenisRef.current.destroy();
        lenisRef.current = null;
      }
      // Remove gsap ticker
      if (typeof window !== 'undefined') {
        // gsap.ticker is global; we added to it
      }
    };
  }, []);

  return null; // This is a logic-only island, no DOM output
}
