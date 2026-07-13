type UsdaModuleNavProps = {
  activePage: 'dashboard' | 'presentation'
}

import { appPath } from '../utils/appPath'

export function UsdaModuleNav({ activePage }: UsdaModuleNavProps) {
  return <nav className="usda-module-nav" aria-label="USDA 模块导航">
    <a className={activePage === 'dashboard' ? 'active' : ''} href={appPath()} aria-current={activePage === 'dashboard' ? 'page' : undefined}>供需平衡表</a>
    <a className={activePage === 'presentation' ? 'active' : ''} href={appPath('presentation')} aria-current={activePage === 'presentation' ? 'page' : undefined}>年度供需</a>
  </nav>
}
