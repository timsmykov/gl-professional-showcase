export type PublicationStatus = 'draft' | 'published' | 'archived';

export type ProductLine = 'nad' | 'plla' | 'cute';

export interface ContentImage {
  src: string;
  srcSet?: string;
  alt: string;
  width: number;
  height: number;
  caption?: string;
  caseKey?: string;
  caseLabel?: string;
}

export interface ProductFact {
  label: string;
  value: string;
}

export interface ProductBrandCopy {
  nameInSource: string;
  composition: string;
  description: string;
  uniqueness: string[];
}

export interface ProductSummary {
  slug: string;
  title: string;
  line: ProductLine;
  lineLabel: string;
  summary: string;
  description: string;
  facts: ProductFact[];
  distinguishingFact: string;
  contentNote: string;
  brandCopy: ProductBrandCopy;
  cover: ContentImage;
  packagingPhoto?: {
    hero: ContentImage;
    card: ContentImage;
  };
  visuals: ContentImage[];
  clinicalImages: ContentImage[];
  status: PublicationStatus;
}

export interface WebinarSummary {
  demo?: boolean;
  publicCode?: string;
  speaker?: string;
  date?: string;
  duration?: string;
  program?: string[];
  audience?: string;
  priceLabel?: string;
  terms?: string;
  slug: string;
  title: string;
  format: 'live' | 'recording';
  formatLabel: string;
  summary: string;
  status: PublicationStatus;
}

export interface PublicSiteContent {
  products: ProductSummary[];
  webinars: WebinarSummary[];
}
