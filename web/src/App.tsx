import { Toaster } from 'react-hot-toast'
import { useAppStore } from './store'
import { Login } from './components/Login'
import { MainPage } from './components/MainPage'

function App() {
  const token = useAppStore((s) => s.token)

  return (
    <>
      {token ? <MainPage /> : <Login />}
      <Toaster
        position="top-right"
        toastOptions={{
          duration: 4000,
          style: {
            fontSize: '14px',
          },
        }}
      />
    </>
  )
}

export default App
