import sitemap from '@astrojs/sitemap';
import vue from '@astrojs/vue';
import { defineConfig } from 'astro/config';

const site = process.env.SITE_URL || 'https://gl-professional.invalid';

export default defineConfig({
  site,
  output: 'static',
  integrations: [
    vue(),
    sitemap({
      filter: (page) =>
        !page.endsWith('/404/') &&
        !page.endsWith('/contacts/') &&
        !page.includes('/design-review/') &&
        !page.endsWith('/webinars/') &&
        !page.includes('/webinars/demo-'),
    }),
  ],
  build: {
    format: 'directory',
  },
  vite: {
    build: {
      cssMinify: 'lightningcss',
    },
  },
});
