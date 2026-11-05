import { describe, expect, it } from 'vitest';
import { microFrame, microSteps } from '../../src/content/microworld';

describe('educational micro scene', () => {
  it('clamps input and has five deterministic reversible states', () => {
    expect(microSteps).toHaveLength(5);
    expect(microFrame(-1)).toEqual(microFrame(0));
    expect(microFrame(Infinity)).toEqual(microFrame(0));
    expect(microFrame(2)).toEqual(microFrame(1));
    const forward = [0, 0.25, 0.5, 0.75, 1].map(microFrame);
    expect([1, 0.75, 0.5, 0.25, 0].map(microFrame).reverse()).toEqual(forward);
    expect(forward.map((x) => x.step)).toEqual([0, 1, 2, 3, 4]);
  });
  it('keeps the ligand outside and transmits only after binding', () => {
    for (let i = 0; i <= 100; i++) {
      const state = microFrame(i / 100);
      expect(state.ligandY).toBeLessThanOrEqual(0);
      expect(state.ligandX).toBeLessThanOrEqual(0);
      if (state.signal > 0) {
        expect(state.ligandX).toBe(0);
        expect(state.ligandY).toBe(0);
      }
    }
    expect(microFrame(1).signal).toBe(1);
  });
});
