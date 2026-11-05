import { stateMotion } from './tokens';

/** Native details fallback with interruptible height animation. */
export function mountDisclosures(
  signal: AbortSignal,
  reduce: MediaQueryList,
): () => void {
  const disposers = Array.from(document.querySelectorAll('details')).map(
    (detail) => {
      const summary = detail.querySelector('summary');
      if (!summary) return () => {};
      let animation: Animation | undefined;
      let expanded = detail.open;
      const settle = () => {
        animation?.cancel();
        animation = undefined;
        detail.open = expanded;
        detail.style.removeProperty('height');
        detail.style.removeProperty('overflow');
        detail.removeAttribute('data-expanded');
      };
      summary.addEventListener(
        'click',
        (event) => {
          event.preventDefault();
          const start = detail.getBoundingClientRect().height;
          animation?.cancel();
          expanded = !expanded;
          if (reduce.matches) {
            settle();
            return;
          }
          detail.open = true;
          detail.dataset.expanded = String(expanded);
          detail.style.height = 'auto';
          const border =
            parseFloat(getComputedStyle(detail).borderBottomWidth) +
            parseFloat(getComputedStyle(detail).borderTopWidth) +
            parseFloat(getComputedStyle(detail).paddingTop) +
            parseFloat(getComputedStyle(detail).paddingBottom);
          const end = expanded
            ? detail.getBoundingClientRect().height
            : summary.getBoundingClientRect().height + border;
          detail.style.height = `${start}px`;
          detail.style.overflow = 'hidden';
          animation = detail.animate(
            [{ height: `${start}px` }, { height: `${end}px` }],
            {
              duration: stateMotion.disclosure,
              easing: stateMotion.ease,
              fill: 'forwards',
            },
          );
          animation.onfinish = settle;
        },
        { signal },
      );
      window.addEventListener('resize', settle, { signal });
      document.addEventListener(
        'visibilitychange',
        () => {
          if (document.hidden) settle();
        },
        { signal },
      );
      reduce.addEventListener('change', settle, { signal });
      return settle;
    },
  );
  return () => {
    disposers.forEach((dispose) => dispose());
  };
}
