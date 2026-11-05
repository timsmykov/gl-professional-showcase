export const siteConfig = {
  name: 'G&L Professional',
  shortName: 'G&L',
  description:
    'Профессиональная платформа G&L: информация о бренде, продуктах и вебинарах.',
  navigation: [
    { href: '/', label: 'Главная' },
    { href: '/products/', label: 'Продукты' },
    { href: '/webinars/', label: 'Вебинары' },
    { href: '/about/', label: 'О бренде' },
  ],
} as const;
