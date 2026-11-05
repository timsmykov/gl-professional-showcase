/** Enable the approved theme on G&L hosts; archived studies stay independent. */
export function isMolecularTheme(
  site: URL | undefined,
  pathname: string,
): boolean {
  return (
    site !== undefined &&
    ['https://dev.glprofessional.ru', 'https://glprofessional.ru'].includes(
      site.origin,
    ) &&
    !/^\/design-review(?:\/|$)/.test(pathname)
  );
}

export function molecularPageKind(pathname: string): string {
  if (pathname === '/') return 'home';
  if (pathname === '/products/' || pathname === '/products') return 'catalog';
  if (pathname.startsWith('/products/')) return 'product';
  if (pathname.startsWith('/about')) return 'about';
  if (pathname.startsWith('/webinars')) return 'education';
  return 'utility';
}
