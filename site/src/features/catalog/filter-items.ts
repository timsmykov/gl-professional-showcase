export interface FilterableItem {
  category: string;
}

export function filterItems<T extends FilterableItem>(
  items: readonly T[],
  category: string,
): T[] {
  if (category === 'all') {
    return [...items];
  }

  return items.filter((item) => item.category === category);
}
