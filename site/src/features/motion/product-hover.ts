export function setupProductHover() {
  document
    .querySelectorAll<HTMLElement>('[data-product-hover-group]')
    .forEach((figure) => {
      if (figure.dataset.ready) return;
      figure.dataset.ready = 'true';
      const controller = new AbortController();
      const { signal } = controller;
      const motion = matchMedia(
        '(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)',
      );
      const boxes = [
        ...figure.querySelectorAll<HTMLElement>('[data-product-hover]'),
      ];
      const reset = (box: HTMLElement) => {
        delete box.dataset.active;
        box.style.removeProperty('--box-x');
        box.style.removeProperty('--box-y');
      };
      const resetAll = () => boxes.forEach(reset);
      boxes.forEach((box) => {
        const move = (event: PointerEvent) => {
          if (
            !motion.matches ||
            event.pointerType !== 'mouse' ||
            document.hidden
          )
            return;
          const bounds = box.getBoundingClientRect();
          const x = Math.max(
            -1,
            Math.min(1, ((event.clientX - bounds.left) / bounds.width) * 2 - 1),
          );
          const y = Math.max(
            -1,
            Math.min(1, ((event.clientY - bounds.top) / bounds.height) * 2 - 1),
          );
          box.dataset.active = 'true';
          box.style.setProperty('--box-x', `${x * 8}px`);
          box.style.setProperty('--box-y', `${y * 6}px`);
        };
        box.addEventListener('pointerenter', move, { signal });
        box.addEventListener('pointermove', move, { signal });
        box.addEventListener('pointerleave', () => reset(box), { signal });
        box.addEventListener('pointercancel', () => reset(box), { signal });
      });
      motion.addEventListener('change', resetAll, { signal });
      window.addEventListener('blur', resetAll, { signal });
      document.addEventListener('visibilitychange', resetAll, { signal });
      document.addEventListener(
        'astro:before-swap',
        () => {
          resetAll();
          controller.abort();
        },
        { once: true, signal },
      );
    });
}
