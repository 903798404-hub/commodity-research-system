import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { PresentationPage } from './components/PresentationPage'
import { UsdaModuleNav } from './components/UsdaModuleNav'
import './index.css'
import { currentAppPathname } from './utils/appPath'

const isPresentationPage = currentAppPathname().replace(/\/+$/, '') === '/presentation'
const page = isPresentationPage ? <PresentationPage /> : <App />

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <UsdaModuleNav activePage={isPresentationPage ? 'presentation' : 'dashboard'} />
    {page}
  </StrictMode>,
)
