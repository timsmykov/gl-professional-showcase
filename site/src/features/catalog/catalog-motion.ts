import { stateMotion } from '../motion/tokens';

/** FLIP positions plus one container height animation. Leaving copies are inert. */
export function createCatalogMotion(root: HTMLElement) {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const animations = new Set<Animation>();
  const ghosts = new Set<HTMLElement>();
  const abort = new AbortController();
  const enabled = () => !reduce.matches && !document.hidden;
  const clear = () => {
    animations.forEach((animation) => animation.cancel());
    animations.clear();
    ghosts.forEach((ghost) => ghost.remove());
    ghosts.clear();
  };
  const animate = (
    el: HTMLElement,
    frames: Keyframe[],
    duration: number = stateMotion.content,
  ) => {
    const animation = el.animate(frames, {
      duration,
      easing: stateMotion.ease,
    });
    animations.add(animation);
    void animation.finished.then(
      () => animations.delete(animation),
      () => {},
    );
    return animation;
  };
  const capture = () => {
    const bounds = root.getBoundingClientRect();
    const cards = new Map(
      Array.from(root.querySelectorAll<HTMLElement>('[data-catalog-key]')).map(
        (el) => [
          el.dataset.catalogKey!,
          {
            bounds: el.getBoundingClientRect(),
            opacity: getComputedStyle(el).opacity,
            copy: el.cloneNode(true) as HTMLElement,
          },
        ],
      ),
    );
    clear();
    return { bounds, cards };
  };
  const play = (before: ReturnType<typeof capture>) => {
    if (!enabled()) return;
    const bounds = root.getBoundingClientRect();
    const cards = Array.from(
      root.querySelectorAll<HTMLElement>('[data-catalog-key]'),
    );
    const currentKeys = new Set(cards.map((el) => el.dataset.catalogKey));
    for (const [key, previous] of before.cards) {
      if (currentKeys.has(key)) continue;
      const ghost = previous.copy;
      ghost.removeAttribute('data-catalog-key');
      ghost.setAttribute('aria-hidden', 'true');
      ghost.inert = true;
      Object.assign(ghost.style, {
        position: 'absolute',
        margin: '0',
        pointerEvents: 'none',
        top: `${previous.bounds.top - before.bounds.top}px`,
        left: `${previous.bounds.left - before.bounds.left}px`,
        width: `${previous.bounds.width}px`,
        height: `${previous.bounds.height}px`,
      });
      // Keep list semantics even for the decorative leaving snapshot.
      root.querySelector('ul')?.append(ghost);
      ghosts.add(ghost);
      const animation = animate(
        ghost,
        [{ opacity: previous.opacity }, { opacity: 0 }],
        stateMotion.feedback,
      );
      void animation.finished.then(
        () => {
          ghost.remove();
          ghosts.delete(ghost);
        },
        () => {},
      );
    }
    for (const card of cards) {
      const previous = before.cards.get(card.dataset.catalogKey!);
      const next = card.getBoundingClientRect();
      animate(
        card,
        previous
          ? [
              {
                opacity: previous.opacity,
                transform: `translate(${previous.bounds.left - next.left}px, ${previous.bounds.top - next.top}px)`,
              },
              { opacity: 1, transform: 'none' },
            ]
          : [
              { opacity: 0, transform: 'translateY(8px)' },
              { opacity: 1, transform: 'none' },
            ],
      );
    }
    if (Math.abs(bounds.height - before.bounds.height) > 1)
      animate(root, [
        { height: `${before.bounds.height}px` },
        { height: `${bounds.height}px` },
      ]);
  };
  window.addEventListener('resize', clear, { signal: abort.signal });
  window.addEventListener('pagehide', clear, { signal: abort.signal });
  reduce.addEventListener('change', clear, { signal: abort.signal });
  document.addEventListener('visibilitychange', clear, {
    signal: abort.signal,
  });
  return {
    capture,
    play,
    dispose: () => {
      clear();
      abort.abort();
    },
  };
}
