import { useEffect, useRef } from 'react';

/**
 * ScrollController.tsx — client:only="visible" island.
 * Instantiates Lenis + GSAP ScrollTrigger, drives the WebGL scene,
 * updates beat captions, colour-temperature shifts, and section reveals.
 *
 * Choreography (maps scroll → six beats):
 *   - Hero is pinned while Beat 1→2 plays (canvas in view as headline resolves).
 *   - On unpin, camera journey continues in scroll-space through subsequent sections.
 *   - Each beat threshold fires: caption update + colour-temp shift + scene event.
 *   - Beat 4 (The Gate) fires a refusal that HOLDS for the beat extent.
 *   - Beat 6 (Land) transitions page to light theme for pricing/proof.
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

    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    const reducedMotion = mediaQuery.matches;

    // ── Helper: determine beat from progress ──
    function beatFromProgress(p: number): number {
      for (let i = BEAT_THRESHOLDS.length - 2; i >= 0; i--) {
        if (p >= BEAT_THRESHOLDS[i]) return i + 1;
      }
      return 1;
    }

    // ── Helper: update beat caption + data attr + colour temp ──
    function applyBeat(beat: number) {
      const beatCaption = document.querySelector('[data-beat-caption]');
      if (beatCaption) {
        beatCaption.textContent = BEAT_CAPTIONS[beat - 1] || BEAT_CAPTIONS[0];
      }
      document.documentElement.dataset.beat = String(beat);

      const temp = BEAT_TEMPS[beat];
      if (temp) {
        const rootEl = document.documentElement;
        rootEl.style.setProperty('--bg-temp', temp.bg);
        rootEl.style.setProperty('--light-tint', temp.tint);
        rootEl.setAttribute('data-theme', temp.theme === 'light' ? 'light' : 'dark');
      }
    }

    // ── Helper: drive scene ──
    function driveScene(progress: number) {
      if ((window as any).WeftScene?.setProgress) {
        (window as any).WeftScene.setProgress(progress);
      }
    }

    // ── Helper: fire beat events (scene + DOM) ──
    function fireBeatEvent(beat: number, fromScroll: boolean) {
      // Dispatch agent-event CustomEvent for DOM listeners
      if (typeof window !== 'undefined') {
        if (beat === 4) {
          // The Gate — fire refusal (holds for beat extent via ScrollTrigger onLeave)
          if ((window as any).WeftScene?.fireGateRefusal) {
            (window as any).WeftScene.fireGateRefusal();
          } else {
            window.dispatchEvent(new CustomEvent('agent-event', {
              detail: { kind: 'refuse', seq: Date.now(), agent: 'agent-a', msg: 'stale_fencing_token (refused)' },
            }));
          }
        } else if (fromScroll) {
          const msgs: Record<number, string> = {
            2: 'Many agents join the room',
            3: 'Messages travel the edges',
            5: 'Evidence recorded',
            6: 'Connect your agent',
          };
          if (msgs[beat]) {
            window.dispatchEvent(new CustomEvent('agent-event', {
              detail: { kind: 'beat', beat, msg: msgs[beat] },
            }));
          }
        }
      }
    }

    // ── Reduced-motion path ──
    if (reducedMotion) {
      driveScene(1);
      applyBeat(6);
      document.querySelectorAll('.reveal').forEach((el) => el.classList.add('in'));
      document.querySelectorAll('[data-step]').forEach((el) => el.classList.add('step--active'));
      document.querySelectorAll('[data-proof-item]').forEach((el) => el.classList.add('proof-item--visible'));
      return () => { cancelled = true; };
    }

    // ── Full-motion init ──
    async function init() {
      if (cancelled) return;

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

      // ── Full-page ScrollTrigger: scroll → normalized progress ──
      // This is the master driver: maps total scroll progress to 0..1 for the scene.
      ScrollTrigger.create({
        trigger: 'body',
        start: 'top top',
        end: 'bottom bottom',
        onUpdate: (self: any) => {
          const p = self.progress;
          driveScene(p);
          const beat = beatFromProgress(p);
          applyBeat(beat);
        },
      });

      // ── HERO PIN: canvas stays in view while Beat 1→2 plays ──
      // Pin the hero for the first portion so the WebGL canvas remains visible
      // as the headline/beats resolve, then release so subsequent sections scroll naturally.
      const heroEl = document.querySelector('.hero');
      if (heroEl) {
        const pinT = ScrollTrigger.create({
          trigger: '.hero',
          start: 'top top',
          end: '+=200%', // pin for 2 viewport-heights of scroll
          pin: true,
          pinSpacing: true,
          anticipatePin: 1,
          onEnter: () => {
            driveScene(0);
            applyBeat(1);
          },
          onLeaveBack: () => {
            driveScene(0);
            applyBeat(1);
          },
        });
        triggersRef.current.push(pinT);
      }

      // ── Beat threshold triggers — fire scene events at each beat boundary ──
      // Beat thresholds (progress values where each beat begins)
      const beatThresholds = [0.12, 0.30, 0.50, 0.66, 0.82];
      beatThresholds.forEach((threshold, idx) => {
        const beatNum = idx + 2; // beats 2..6
        // Create a scroll trigger at the beat's progress position
        // We use the document body and compute scroll position from progress
        const t = ScrollTrigger.create({
          trigger: 'body',
          start: 'top top',
          end: 'bottom bottom',
          onUpdate: (self: any) => {
            if (self.progress >= threshold && self._lastFiredBeat !== beatNum) {
              self._lastFiredBeat = beatNum;
              fireBeatEvent(beatNum, true);
            }
          },
        });
        triggersRef.current.push(t);
      });

      // ── Beat 4 (The Gate) refusal HOLD ──
      // Fire refusal reliably when entering Beat 4, and HOLD the colour shift
      // for the extent of the beat (progress 0.50 → 0.66).
      const gateTrigger = ScrollTrigger.create({
        trigger: 'body',
        start: 'top top',
        end: 'bottom bottom',
        onUpdate: (self: any) => {
          const p = self.progress;
          if (p >= 0.50 && p < 0.66) {
            // Within the Gate beat — ensure refusal is active
            if (!self._gateFired) {
              self._gateFired = true;
              fireBeatEvent(4, true);
            }
          } else if (p >= 0.66) {
            // Past the Gate — reset so it can re-fire on scroll-back
            self._gateFired = false;
          }
        },
      });
      triggersRef.current.push(gateTrigger);

      // ── Section reveals via ScrollTrigger ──
      const sections = document.querySelectorAll('[data-reveal-section]');
      sections.forEach((section) => {
        const children = section.querySelectorAll('.reveal');
        if (children.length > 0) {
          const t = ScrollTrigger.create({
            trigger: section,
            start: 'top 85%',
            end: 'top 40%',
            onEnter: () => {
              children.forEach((el: Element, i: number) => {
                setTimeout(() => el.classList.add('in'), i * 80);
              });
            },
            onLeaveBack: () => {
              if (reducedMotion) return;
              children.forEach((el: Element) => el.classList.remove('in'));
            },
          });
          triggersRef.current.push(t);
        }
      });

      // ── Step animations for HowItWorks ──
      document.querySelectorAll('[data-step]').forEach((step) => {
        const t = ScrollTrigger.create({
          trigger: step,
          start: 'top 80%',
          onEnter: () => step.classList.add('step--active'),
          onLeaveBack: () => {
            if (!reducedMotion) step.classList.remove('step--active');
          },
        });
        triggersRef.current.push(t);
      });

      // ── Proof items stagger ──
      document.querySelectorAll('[data-proof-item]').forEach((item) => {
        const t = ScrollTrigger.create({
          trigger: item,
          start: 'top 85%',
          onEnter: () => item.classList.add('proof-item--visible'),
          onLeaveBack: () => {
            if (!reducedMotion) item.classList.remove('proof-item--visible');
          },
        });
        triggersRef.current.push(t);
      });

      // ── Initial state ──
      driveScene(0);
      applyBeat(1);

      // Hero reveals forced visible on mount (truthful initial state)
      const heroReveals = document.querySelectorAll('.hero .reveal');
      heroReveals.forEach((el: Element, i: number) => {
        setTimeout(() => el.classList.add('in'), 60 + i * 90);
      });
      // Stat pop — guard for zero [data-count-to] elements (stat strip may be deleted by another lane)
      document.querySelectorAll('[data-count-to]').forEach((el: Element, i: number) => {
        setTimeout(() => el.classList.add('stat-pop'), 300 + i * 80);
      });
      // Belt-and-braces hard timeout
      setTimeout(() => {
        if (!cancelled) {
          document.querySelectorAll('.hero .reveal').forEach((el) => el.classList.add('in'));
        }
      }, 1200);
    }

    init();

    return () => {
      cancelled = true;
      triggersRef.current.forEach((t) => t.kill());
      triggersRef.current = [];
      if (lenisRef.current) {
        lenisRef.current.destroy();
        lenisRef.current = null;
      }
    };
  }, []);

  return null; // Logic-only island, no DOM output
}
