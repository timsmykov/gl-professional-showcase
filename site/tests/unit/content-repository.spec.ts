import { describe, expect, it } from 'vitest';

import { fixtureContent } from '../../src/content/fixtures';
import { FixtureContentRepository } from '../../src/lib/content/fixture-content-repository';

describe('FixtureContentRepository', () => {
  const repository = new FixtureContentRepository();

  it('returns public products and finds one by slug', async () => {
    const products = await repository.listProducts();
    const product = await repository.findProduct('nad-skin-matrix');

    expect(products.length).toBeGreaterThan(0);
    expect(product?.title).toBe('NAD+ Skin Matrix');
  });

  it('returns undefined for an unknown slug', async () => {
    await expect(repository.findProduct('missing')).resolves.toBeUndefined();
  });

  it('maps every full brand description to its own product', async () => {
    const products = await repository.listProducts();
    const bySlug = new Map(products.map((product) => [product.slug, product]));
    expect(bySlug.size).toBe(4);

    expect(bySlug.get('nad-skin-matrix')?.brandCopy).toMatchObject({
      nameInSource: 'G&L NAD+ Skin matrix',
      composition: expect.stringContaining('NAD+комплекс 1%'),
      uniqueness: expect.arrayContaining([
        expect.stringContaining('фундаментального клеточного'),
      ]),
    });
    expect(bySlug.get('plla-activator')?.brandCopy).toMatchObject({
      nameInSource: 'G&L PLLA + Активатор',
      composition: expect.stringContaining('флакон 150 мг ПЛЛА'),
      uniqueness: expect.arrayContaining([
        expect.stringContaining('канюлю 25g'),
      ]),
    });
    expect(bySlug.get('cute')?.brandCopy).toMatchObject({
      nameInSource: 'G&L Cute',
      composition: expect.stringContaining('аргилерин 0.002%'),
      uniqueness: expect.arrayContaining([
        expect.stringContaining('синтезе АТФ'),
      ]),
    });
    expect(bySlug.get('cute-ultra')?.brandCopy).toMatchObject({
      nameInSource: 'G&L Cute Ultra',
      composition: expect.stringContaining('L-карнитин 0.1%'),
      uniqueness: expect.arrayContaining([
        expect.stringContaining('отечно – деформационного'),
      ]),
    });
    for (const product of products) {
      expect(product.brandCopy.description).not.toBe('');
      expect(product.brandCopy.uniqueness.length).toBeGreaterThan(0);
      expect(JSON.stringify(product.brandCopy)).not.toMatch(
        /\b(?:ИНН|ОГРН|БИК)\b/,
      );
    }
  });

  it('keeps the explicitly excluded PLLA case out of public content', async () => {
    const product = await repository.findProduct('plla-activator');

    expect(product?.clinicalImages).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          src: expect.stringContaining('clinical-11'),
        }),
      ]),
    );
  });

  it('never exposes draft or archived records', async () => {
    const hiddenProduct = {
      ...fixtureContent.products[0]!,
      slug: 'hidden-product',
      status: 'draft' as const,
    };
    const hiddenWebinar = {
      slug: 'hidden-webinar',
      title: 'Черновик',
      format: 'recording' as const,
      formatLabel: 'Запись',
      summary: 'Черновая запись',
      status: 'archived' as const,
    };
    const guardedRepository = new FixtureContentRepository({
      products: [...fixtureContent.products, hiddenProduct],
      webinars: [...fixtureContent.webinars, hiddenWebinar],
    });

    await expect(
      guardedRepository.findProduct('hidden-product'),
    ).resolves.toBeUndefined();
    await expect(
      guardedRepository.findWebinar('hidden-webinar'),
    ).resolves.toBeUndefined();
    await expect(guardedRepository.listProducts()).resolves.not.toContainEqual(
      hiddenProduct,
    );
    await expect(guardedRepository.listWebinars()).resolves.not.toContainEqual(
      hiddenWebinar,
    );
  });
});
