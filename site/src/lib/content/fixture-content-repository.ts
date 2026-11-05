import { fixtureContent } from '@/content/fixtures';
import type {
  ProductSummary,
  PublicSiteContent,
  WebinarSummary,
} from '@/content/site.types';

import type { ContentRepository } from './content-repository';

export class FixtureContentRepository implements ContentRepository {
  constructor(private readonly content: PublicSiteContent = fixtureContent) {}

  async listProducts(): Promise<ProductSummary[]> {
    return this.content.products.filter(
      (product) => product.status === 'published',
    );
  }

  async findProduct(slug: string): Promise<ProductSummary | undefined> {
    return this.content.products.find(
      (product) => product.slug === slug && product.status === 'published',
    );
  }

  async listWebinars(): Promise<WebinarSummary[]> {
    return this.content.webinars.filter(
      (webinar) => webinar.status === 'published',
    );
  }

  async findWebinar(slug: string): Promise<WebinarSummary | undefined> {
    return this.content.webinars.find(
      (webinar) => webinar.slug === slug && webinar.status === 'published',
    );
  }
}
