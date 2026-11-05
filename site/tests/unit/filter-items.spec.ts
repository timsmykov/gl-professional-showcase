import { describe, expect, it } from 'vitest';

import { filterItems } from '../../src/features/catalog/filter-items';

const items = [
  { id: 1, category: 'nad' },
  { id: 2, category: 'cute' },
  { id: 3, category: 'cute' },
];

describe('filterItems', () => {
  it('returns every item for all', () => {
    expect(filterItems(items, 'all')).toEqual(items);
  });

  it('returns only the selected category', () => {
    expect(filterItems(items, 'cute')).toEqual([items[1], items[2]]);
  });

  it('returns an empty array for an absent category', () => {
    expect(filterItems(items, 'plla')).toEqual([]);
  });
});
