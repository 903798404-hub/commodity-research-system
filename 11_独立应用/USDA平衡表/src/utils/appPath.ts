const basePath = import.meta.env.BASE_URL

export function appPath(path = ''): string {
  return `${basePath}${path.replace(/^\/+/, '')}`
}

export function currentAppPathname(): string {
  const normalizedBase = basePath.replace(/\/$/, '')
  const pathname = window.location.pathname
  if (!normalizedBase || normalizedBase === '/') return pathname
  return pathname.startsWith(normalizedBase) ? pathname.slice(normalizedBase.length) || '/' : pathname
}
