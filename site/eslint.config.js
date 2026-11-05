import eslint from '@eslint/js';
import astro from 'eslint-plugin-astro';
import vue from 'eslint-plugin-vue';
import tseslint from 'typescript-eslint';

export default [
  {
    ignores: [
      'dist/**',
      '.astro/**',
      'playwright-report/**',
      'test-results/**',
      'docs/design-studies/**',
      'output/**',
      '.playwright-cli/**',
      '.artifacts/**',
      'public/design-studies/microworld/player.js',
    ],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  ...astro.configs.recommended,
  ...vue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    files: ['**/*.d.ts'],
    rules: {
      '@typescript-eslint/triple-slash-reference': 'off',
    },
  },
];
