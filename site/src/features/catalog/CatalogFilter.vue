<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import { filterItems } from './filter-items';
import { createCatalogMotion } from './catalog-motion';

interface CatalogItem {
  slug: string;
  title: string;
  category: string;
  categoryLabel: string;
  summary: string;
  distinguishingFact?: string;
  packagingPhoto?: boolean;
  image?: {
    src: string;
    srcSet?: string;
    alt: string;
    width: number;
    height: number;
  };
}

interface FilterOption {
  value: string;
  label: string;
}

const props = defineProps<{
  items: CatalogItem[];
  filters: FilterOption[];
  basePath: string;
  emptyText: string;
}>();

const activeFilter = ref('all');
const isHydrated = ref(false);
const results = ref<globalThis.HTMLElement>();
let motion: ReturnType<typeof createCatalogMotion> | undefined;
let revision = 0;
const abort = new globalThis.AbortController();
const visibleItems = computed(() =>
  filterItems(props.items, activeFilter.value),
);

function readFilter() {
  const initial = new globalThis.URLSearchParams(
    globalThis.location.search,
  ).get('line');
  return initial && props.filters.some((filter) => filter.value === initial)
    ? initial
    : 'all';
}
async function selectFilter(value: string, updateHistory = true) {
  if (value === activeFilter.value) return;
  const current = ++revision;
  const before = motion?.capture();
  activeFilter.value = value;
  if (updateHistory) {
    const url = new globalThis.URL(globalThis.location.href);
    if (value === 'all') url.searchParams.delete('line');
    else url.searchParams.set('line', value);
    globalThis.history.pushState(null, '', url);
  }
  await nextTick();
  if (current === revision && before) motion?.play(before);
}
onMounted(() => {
  activeFilter.value = readFilter();
  isHydrated.value = true;
  if (results.value) motion = createCatalogMotion(results.value);
  globalThis.addEventListener(
    'popstate',
    () => void selectFilter(readFilter(), false),
    { signal: abort.signal },
  );
});
onBeforeUnmount(() => {
  revision++;
  abort.abort();
  motion?.dispose();
});
</script>

<template>
  <section
    class="catalog"
    aria-labelledby="catalog-heading"
    :data-hydrated="isHydrated ? 'true' : 'false'"
  >
    <h2 id="catalog-heading" class="visually-hidden">Каталог</h2>

    <div
      v-if="items.length && isHydrated"
      class="filter"
      role="group"
      aria-label="Фильтр каталога"
    >
      <button
        v-for="filter in filters"
        :key="filter.value"
        type="button"
        class="filter__button"
        :class="{ 'filter__button--active': activeFilter === filter.value }"
        :aria-pressed="activeFilter === filter.value"
        @click="selectFilter(filter.value)"
      >
        {{ filter.label }}
      </button>
    </div>

    <p v-if="items.length" class="result-count" role="status">
      Найдено: {{ visibleItems.length }}
    </p>
    <div ref="results" class="catalog-results">
      <ul
        class="catalog__grid"
        :class="{ 'catalog__grid--webinars': basePath === '/webinars/' }"
      >
        <li
          v-for="item in visibleItems"
          :key="item.slug"
          :data-catalog-key="item.slug"
          class="catalog-card"
        >
          <a class="catalog-card__target" :href="`${basePath}${item.slug}/`">
            <div
              v-if="item.image"
              class="catalog-scene"
              :class="[
                `catalog-scene--${item.category}`,
                { 'catalog-scene--photo': item.packagingPhoto },
              ]"
            >
              <img
                v-if="item.image"
                :src="item.image.src"
                :srcset="item.image.srcSet"
                :sizes="
                  item.packagingPhoto
                    ? '(min-width: 760px) 45vw, 90vw'
                    : undefined
                "
                :alt="item.image.alt"
                :width="item.image.width"
                :height="item.image.height"
                loading="lazy"
                decoding="async"
                draggable="false"
              />
              <span v-else class="catalog-scene__name">{{ item.title }}</span>
            </div>
            <h3>{{ item.title }}</h3>
            <p class="catalog-card__format">{{ item.categoryLabel }}</p>
            <p>{{ item.summary }}</p>
            <p v-if="item.distinguishingFact" class="catalog-card__fact">
              {{ item.distinguishingFact }}
            </p>
            <span class="catalog-card__link"
              >Подробнее<span class="visually-hidden">
                о {{ item.title }}</span
              ></span
            >
          </a>
        </li>
      </ul>

      <p v-if="!visibleItems.length" class="empty-state">{{ emptyText }}</p>
    </div>
  </section>
</template>

<style scoped>
.filter {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  padding-block: 0.25rem 1rem;
  scrollbar-width: thin;
}

.filter__button {
  transition:
    color var(--motion-feedback),
    background-color var(--motion-feedback),
    border-color var(--motion-feedback);
  min-height: var(--control-height);
  flex: 0 0 auto;
  border: 1px solid var(--color-border);
  border-radius: 100px;
  background: var(--color-surface);
  color: var(--color-text);
  padding-inline: var(--control-padding);
  font: inherit;
  font-size: var(--text-action-size);
  cursor: pointer;
}

.filter__button--active {
  border-color: var(--color-accent);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
}

.catalog__grid {
  display: grid;
  gap: 40px;
  padding: 0;
  margin: 0;
  list-style: none;
}
.catalog-results {
  position: relative;
  display: flow-root;
  overflow: clip;
  overflow-clip-margin: 8px;
}
.catalog__grid:empty {
  display: none;
}
@media (prefers-reduced-motion: reduce) {
  .filter__button {
    transition: none;
  }
}

.catalog-card__target {
  display: grid;
  gap: 0.75rem;
  min-width: 0;
  align-content: start;
  padding: 16px 16px 28px;
  border-radius: 18px;
  transition: background-color var(--motion-feedback) var(--motion-ease);
  color: inherit;
  text-decoration: none;
}

.catalog-card__target:is(:hover, :focus-visible) {
  background: var(--interaction-tint);
}

.catalog-card h3,
.catalog-card p {
  margin: 0;
}
.catalog-card h3 {
  font-size: var(--text-card-size);
}
.catalog-card__target:hover .catalog-card__link,
.catalog-card__target:focus-visible .catalog-card__link {
  text-decoration: none;
}
.catalog-scene {
  min-height: 300px;
  border-radius: 48% 48% 18px 18px / 30% 30% 18px 18px;
  background: #dce9e6;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 22px;
  padding: 28px;
  text-align: center;
  margin-bottom: 16px;
  font-size: var(--text-meta-size);
}
.catalog-scene--plla {
  background: #e4e7df;
}
.catalog-scene--cute {
  background: #ebe0c6;
}
.catalog-scene__name {
  font-size: var(--text-summary-size);
  line-height: 1.2;
}
.catalog-scene img {
  display: block;
  width: min(78%, 300px);
  height: 300px;
  border-radius: 4px;
  object-fit: contain;
  box-shadow: none;
}
.catalog-scene.catalog-scene--photo {
  min-height: 300px;
  height: 356px;
  padding: 0;
  border-radius: 24px;
  background: #e0eaf4;
  overflow: hidden;
}
.catalog-scene.catalog-scene--photo img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: 0;
  pointer-events: none;
  user-select: none;
}
.result-count {
  color: var(--color-muted);
  font-size: var(--text-meta-size);
}
.filter__button:hover {
  background: #e3e8de;
  color: var(--color-text);
}
.filter__button--active:hover {
  background: #365963;
  color: white;
}

.catalog-card__format {
  color: var(--color-muted);
  font-size: var(--text-body-size);
  font-weight: 400;
}

.catalog-card__link {
  width: fit-content;
  font-weight: 700;
}
.catalog-card__fact {
  color: var(--color-ink);
  font-size: var(--text-body-size);
}

.empty-state {
  background: var(--color-ice);
  border-radius: var(--radius-card);
  padding: 1.5rem;
  color: var(--color-ink);
}

@media (min-width: 760px) {
  .catalog__grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (min-width: 1100px) {
  .catalog-scene.catalog-scene--photo {
    height: 396px;
  }
  .catalog-scene {
    min-height: 380px;
  }
  .catalog-scene img {
    width: min(82%, 400px);
    height: 340px;
  }
}
</style>
