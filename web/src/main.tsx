import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider, createBrowserRouter } from 'react-router-dom'
import './i18n'
import './styles.css'
import { DevPage } from './dev/DevPage'
import { EntryDoc } from './pub/EntryDoc'
import { ReportPage } from './pub/ReportPage'
import { RunLiveRoute } from './pub/RunLive'
import { Shell } from './pub/Shell'
import { StartPage } from './pub/StartPage'

const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 5000, retry: 1, refetchOnWindowFocus: false } },
})

const router = createBrowserRouter([
  {
    element: <Shell />,
    children: [
      { path: '/', element: <StartPage /> },
      { path: '/runs/:runId', element: <ReportPage /> },
      { path: '/runs/:runId/live', element: <RunLiveRoute /> },
      { path: '/runs/:runId/e/:entry', element: <EntryDoc /> },
    ],
  },
  { path: '/dev', element: <DevPage /> },
  { path: '/dev/run/:runId', element: <DevPage /> },
  { path: '/dev/run/:runId/agent/:agentId', element: <DevPage /> },
  { path: '/dev/agent/:agentId', element: <DevPage /> },
  { path: '*', element: <Shell /> },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
)
