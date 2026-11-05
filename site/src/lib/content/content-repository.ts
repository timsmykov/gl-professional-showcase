import type { ProductSummary, WebinarSummary } from '@/content/site.types';

export interface ContentRepository {
  listProducts(): Promise<ProductSummary[]>;
  findProduct(slug: string): Promise<ProductSummary | undefined>;
  listWebinars(): Promise<WebinarSummary[]>;
  findWebinar(slug: string): Promise<WebinarSummary | undefined>;
}
