import { mountDisclosures } from './disclosures';
import { stateMotion } from './tokens';
import { microFrame } from '@/content/microworld';

// Motion Standard v1.0. CSS owns presence; WAAPI owns response/state;
// the scroll handler alone owns proximity. No perpetual JavaScript ticker.
const motion = {
  feedback: stateMotion.feedback,
  content: stateMotion.content,
  contentY: 8,
  contentDelay: 90,
  product: stateMotion.product,
  productY: 20,
  productOvershoot: -3,
  productScale: 0.94,
  productOvershootScale: 1.01,
  reveal: 680,
  revealY: 18,
  heroEnter: 1100,
  heroY: 35,
  heroMobileY: 18,
  heroScale: 0.95,
  pointer: 140,
  pointerReturn: 650,
  tiltX: 4,
  tiltY: 5,
  pointerX: 8,
  pointerY: 6,
  scrollX: -80,
  scrollRotation: -18,
  scrollTurn: 42,
  scrollScale: 0.85,
  scrollGrowth: 0.55,
  compactGrowth: 0.08,
} as const;
const clamp = (value: number) => Math.max(0, Math.min(1, value));
let unmount: (() => void) | undefined;

export function mountMotion(): void {
  unmount?.();
  const root = document.documentElement;
  const abort = new AbortController();
  const { signal } = abort;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const fine = matchMedia('(hover: hover) and (pointer: fine)');
  const compact = matchMedia('(max-width: 999px), (max-height: 760px)');
  const query = <T extends Element>(selector: string) =>
    document.querySelector<T>(selector);
  const all = <T extends Element>(selector: string) =>
    Array.from(document.querySelectorAll<T>(selector));
  const hero = query<HTMLElement>('.hero');
  const zone = query<HTMLElement>('.hero-art');
  const response = query<HTMLElement>('.hero-response');
  const score = query<HTMLElement>('.proximity');
  const stage = query<HTMLElement>('.proximity-stage');
  const text = query<HTMLElement>('.proximity-text');
  const ribbon = query<SVGElement>('.proximity-ribbon');
  const track = query<HTMLElement>('.proximity-track i');
  const ease = getComputedStyle(root).getPropertyValue('--motion-ease').trim();
  const animations = new Map<Element, Animation>();
  let heroVisible = false;
  let frame = 0;
  let pointer: { x: number; y: number } | undefined;
  let fitDirty = true;
  let disposed = false;
  const disabled = () => reduce.matches;
  const active = () => !disposed && !disabled() && !document.hidden;

  function cancel(element: Element | null): void {
    if (!element) return;
    animations.get(element)?.cancel();
    animations.delete(element);
  }
  function animate(
    element: Element | null,
    frames: Keyframe[],
    duration: number,
    delay = 0,
    easing = ease,
  ): void {
    if (!element || !active()) return;
    cancel(element);
    const animation = element.animate(frames, {
      duration,
      delay,
      easing,
      fill: 'backwards',
    });
    animations.set(element, animation);
    void animation.finished.then(
      () => {
        if (animations.get(element) === animation) animations.delete(element);
      },
      () => {},
    );
  }
  function clear(): void {
    for (const animation of animations.values()) animation.cancel();
    animations.clear();
    cancelAnimationFrame(frame);
    frame = 0;
    pointer = undefined;
    response?.style.removeProperty('transform');
  }
  function syncBreath(): void {
    root.classList.toggle('motion-breathing', active() && heroVisible);
  }
  function updateScore(): void {
    if (!score || !stage || !text || !ribbon) return;
    if (fitDirty) {
      // Measure the actual desktop text column, including enlarged fonts.
      score.classList.toggle('is-sticky', !compact.matches && !disabled());
      const style = getComputedStyle(stage);
      const available =
        stage.clientHeight -
        parseFloat(style.paddingTop) -
        parseFloat(style.paddingBottom);
      if (text.scrollHeight > available || compact.matches || disabled())
        score.classList.remove('is-sticky');
      fitDirty = false;
    }
    if (disabled()) {
      ['--cell-scale', '--ligand-x', '--ligand-y', '--cell-signal'].forEach(
        (name) => score.style.removeProperty(name),
      );
      ribbon.style.removeProperty('transform');
      track?.style.removeProperty('transform');
      all('.proximity-chapters li').forEach((el) =>
        el.classList.remove('current'),
      );
      return;
    }
    let bounds = score.getBoundingClientRect();
    if (score.classList.contains('is-sticky') && bounds.height <= innerHeight) {
      score.classList.remove('is-sticky');
      bounds = score.getBoundingClientRect();
    }
    const sticky = score.classList.contains('is-sticky');
    const p = sticky
      ? clamp(-bounds.top / (bounds.height - innerHeight))
      : clamp((innerHeight - bounds.top) / (innerHeight + bounds.height));
    if (score.dataset.scene === 'micro-world') {
      const state = microFrame(sticky ? p : 1);
      score.style.setProperty('--cell-scale', String(state.scale));
      score.style.setProperty('--ligand-x', `${state.ligandX}px`);
      score.style.setProperty('--ligand-y', `${state.ligandY}px`);
      score.style.setProperty('--cell-signal', String(state.signal));
      score.dataset.step = String(state.step);
      all('.proximity-chapters li').forEach((el, index) =>
        el.classList.toggle('current', sticky && index === state.step),
      );
      if (track) track.style.transform = `scaleX(${sticky ? p : 1})`;
      return;
    }
    ribbon.style.transform = sticky
      ? `translateX(${motion.scrollX * p}px) rotate(${motion.scrollRotation + motion.scrollTurn * p}deg) scale(${motion.scrollScale + motion.scrollGrowth * p})`
      : `scale(${1 + motion.compactGrowth * p})`;
    if (track) track.style.transform = `scaleX(${p})`;
    all('.proximity-chapters li').forEach((el, index) =>
      el.classList.toggle('current', index === Math.min(2, Math.floor(p * 3))),
    );
  }
  function schedule(): void {
    if (frame || !active()) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!active()) return;
      updateScore();
      if (pointer && zone && response && fine.matches) {
        const bounds = zone.getBoundingClientRect();
        const x = clamp((pointer.x - bounds.left) / bounds.width) * 2 - 1;
        const y = clamp((pointer.y - bounds.top) / bounds.height) * 2 - 1;
        const from = getComputedStyle(response).transform;
        const to = `rotateX(${-y * motion.tiltX}deg) rotateY(${x * motion.tiltY}deg) translate(${x * motion.pointerX}px, ${y * motion.pointerY}px)`;
        response.style.transform = to;
        animate(
          response,
          [{ transform: from }, { transform: to }],
          motion.pointer,
          0,
          'ease-out',
        );
        pointer = undefined;
      }
    });
  }
  function resetPointer(): void {
    pointer = undefined;
    const from = response ? getComputedStyle(response).transform : 'none';
    cancel(response);
    response?.style.removeProperty('transform');
    animate(
      response,
      [{ transform: from }, { transform: 'none' }],
      motion.pointerReturn,
    );
  }
  function sync(): void {
    clear();
    root.classList.toggle('motion-static', disabled());
    fitDirty = true;
    if (!document.hidden) updateScore();
    syncBreath();
  }
  root.classList.add('motion-ready');
  zone?.addEventListener(
    'pointermove',
    (event) => {
      if (!active() || !fine.matches || event.pointerType === 'touch') return;
      pointer = { x: event.clientX, y: event.clientY };
      schedule();
    },
    { signal, passive: true },
  );
  zone?.addEventListener('pointerleave', resetPointer, { signal });
  fine.addEventListener('change', resetPointer, { signal });
  reduce.addEventListener('change', sync, { signal });
  compact.addEventListener('change', sync, { signal });
  window.addEventListener('scroll', schedule, { signal, passive: true });
  window.addEventListener(
    'resize',
    () => {
      fitDirty = true;
      schedule();
    },
    { signal, passive: true },
  );
  document.addEventListener(
    'visibilitychange',
    () => {
      if (document.hidden) clear();
      else {
        fitDirty = true;
        schedule();
      }
      syncBreath();
    },
    { signal },
  );
  all<HTMLInputElement>('.product-radio').forEach((input) =>
    input.addEventListener(
      'change',
      () => {
        all(
          '.product-panel .product-visual img, .product-panel .product-copy',
        ).forEach(cancel);
        if (!input.checked) return;
        const panel = query(`.panel-${input.id}`);
        animate(
          panel?.querySelector('.product-visual img') ?? null,
          [
            {
              opacity: 0,
              transform: `translateY(${motion.productY}px) scale(${motion.productScale})`,
            },
            {
              opacity: 1,
              transform: `translateY(${motion.productOvershoot}px) scale(${motion.productOvershootScale})`,
              offset: 0.7,
            },
            { opacity: 1, transform: 'none' },
          ],
          motion.product,
        );
        animate(
          panel?.querySelector('.product-copy') ?? null,
          [
            { opacity: 0, transform: `translateY(${motion.contentY}px)` },
            { opacity: 1, transform: 'none' },
          ],
          motion.content,
          motion.contentDelay,
        );
      },
      { signal },
    ),
  );
  const stopDisclosures = mountDisclosures(signal, reduce);
  const heroObserver = new IntersectionObserver(
    (entries) => {
      heroVisible = entries.some((entry) => entry.intersectionRatio >= 0.15);
      syncBreath();
    },
    { threshold: [0, 0.15] },
  );
  if (hero) heroObserver.observe(hero);
  const revealObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries)
        if (entry.isIntersecting) {
          revealObserver.unobserve(entry.target);
          animate(
            entry.target,
            [
              { opacity: 0, transform: `translateY(${motion.revealY}px)` },
              { opacity: 1, transform: 'none' },
            ],
            motion.reveal,
          );
        }
    },
    { threshold: 0.12 },
  );
  all('.lesson-art').forEach((el) => revealObserver.observe(el));
  const resizeObserver = new ResizeObserver(() => {
    fitDirty = true;
    schedule();
  });
  if (text) resizeObserver.observe(text);
  const textObserver = new MutationObserver(() => {
    fitDirty = true;
    schedule();
  });
  if (text)
    textObserver.observe(text, {
      subtree: true,
      childList: true,
      characterData: true,
    });
  void document.fonts.ready.then(() => {
    if (!disposed) {
      fitDirty = true;
      schedule();
    }
  });
  sync();
  // A single restrained entrance per tab. Static HTML is never hidden by CSS.
  // No replay during page navigation or BFCache restore; storage is optional.
  let entered = false;
  try {
    entered = sessionStorage.getItem('gl-entry-seen') === 'true';
    sessionStorage.setItem('gl-entry-seen', 'true');
  } catch {
    // Private/restricted storage must not prevent content or interaction.
  }
  if (!entered && !disabled()) {
    for (const [selector, duration, delay] of [
      ['.header-inner', 700, 0],
      ['main > :first-child', 900, 80],
    ] as const) {
      animate(
        query(selector),
        [
          { opacity: 0, transform: 'translateY(10px)' },
          { opacity: 1, transform: 'none' },
        ],
        duration,
        delay,
      );
    }
  }
  animate(
    response,
    [
      {
        opacity: 0,
        transform: `translateY(${innerWidth < 760 ? motion.heroMobileY : motion.heroY}px) scale(${motion.heroScale})`,
      },
      { opacity: 1, transform: 'none' },
    ],
    motion.heroEnter,
  );

  const cleanup = () => {
    disposed = true;
    stopDisclosures();
    abort.abort();
    clear();
    heroObserver.disconnect();
    revealObserver.disconnect();
    resizeObserver.disconnect();
    textObserver.disconnect();
    removalObserver.disconnect();
    root.classList.remove('motion-ready', 'motion-breathing', 'motion-static');
    score?.classList.remove('is-sticky');
    ['--cell-scale', '--ligand-x', '--ligand-y', '--cell-signal'].forEach(
      (name) => score?.style.removeProperty(name),
    );
    score?.removeAttribute('data-step');
    ribbon?.style.removeProperty('transform');
    track?.style.removeProperty('transform');
    all('.proximity-chapters li').forEach((el) =>
      el.classList.remove('current'),
    );
  };
  const shell = query('.shell');
  const removalObserver = new MutationObserver(() => {
    if (!shell?.isConnected || (hero && !hero.isConnected)) cleanup();
  });
  removalObserver.observe(document.body, { childList: true, subtree: true });
  unmount = cleanup;
  window.addEventListener('pagehide', cleanup, { signal, once: true });
}

// One module-level listener survives pagehide for BFCache; mounts stay idempotent.
window.addEventListener('pageshow', (event) => {
  if (event.persisted) mountMotion();
});
