import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { PresentationPage } from './components/PresentationPage'
import { UsdaModuleNav } from './components/UsdaModuleNav'
import './index.css'
import { currentAppPathname } from './utils/appPath'
import { SugarPage } from './components/SugarPage'

const isPresentationPage = currentAppPathname().replace(/\/+$/, '') === '/presentation'
const isSugarPage = currentAppPathname().replace(/\/+$/, '') === '/sugar'
const page = isPresentationPage ? <PresentationPage /> : isSugarPage ? <SugarPage /> : <App />

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <UsdaModuleNav activePage={isPresentationPage ? 'presentation' : isSugarPage ? 'sugar' : 'dashboard'} />
    {page}
  </StrictMode>,
)
