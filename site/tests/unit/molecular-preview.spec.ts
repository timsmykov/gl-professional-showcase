import { describe, expect, it } from 'vitest';
import {
  isMolecularTheme,
  molecularPageKind,
} from '../../src/lib/molecular-preview';

describe('molecular theme deployment boundary', () => {
  it('enables public pages only on the exact approved origins', () => {
    for (const path of [
      '/',
      '/products/',
      '/products/cute/',
      '/about/',
      '/webinars/',
      '/404/',
    ]) {
      expect(
        isMolecularTheme(new URL('https://dev.glprofessional.ru'), path),
      ).toBe(true);
      expect(isMolecularTheme(new URL('https://glprofessional.ru'), path)).toBe(
        true,
      );
      expect(isMolecularTheme(undefined, path)).toBe(false);
      expect(isMolecularTheme(new URL('http://glprofessional.ru'), path)).toBe(
        false,
      );
      expect(
        isMolecularTheme(
          new URL('https://glprofessional.ru.example.org'),
          path,
        ),
      ).toBe(false);
      expect(
        isMolecularTheme(
          new URL('https://dev.glprofessional.ru.example.org'),
          path,
        ),
      ).toBe(false);
    }
  });
  it('preserves original and comparative laboratory routes', () => {
    for (const path of [
      '/design-review',
      '/design-review/',
      '/design-review/molecular/',
      '/design-review/archive/',
    ]) {
      for (const origin of [
        'https://dev.glprofessional.ru',
        'https://glprofessional.ru',
      ]) {
        expect(isMolecularTheme(new URL(origin), path)).toBe(false);
      }
    }
  });
  it('selects page compositions without changing routes', () => {
    expect(molecularPageKind('/')).toBe('home');
    expect(molecularPageKind('/products/')).toBe('catalog');
    expect(molecularPageKind('/products/cute/')).toBe('product');
    expect(molecularPageKind('/about/')).toBe('about');
    expect(molecularPageKind('/webinars/demo-recording/')).toBe('education');
    expect(molecularPageKind('/404/')).toBe('utility');
  });
});
