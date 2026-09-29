import { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import { NavLink, Outlet } from 'react-router-dom'
import { useRuns } from './model'
import './pub.css'

function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(t)
  }, [])
  return <div className="clk">{now.toLocaleString('ru-RU')}</div>
}

const THEME_KEY = 'wsignal-theme'

function savedLight() {
  try {
    return window.localStorage.getItem(THEME_KEY) === 'light'
  } catch {
    return false
  }
}

function useTheme() {
  const [light, setLight] = useState(savedLight)
  const [printing, setPrinting] = useState(false)
  useEffect(() => {
    const before = () => flushSync(() => setPrinting(true))
    const after = () => setPrinting(false)
    window.addEventListener('beforeprint', before)
    window.addEventListener('afterprint', after)
    return () => {
      window.removeEventListener('beforeprint', before)
      window.removeEventListener('afterprint', after)
    }
  }, [])
  const toggle = () => {
    const next = !light
    setLight(next)
    try {
      window.localStorage.setItem(THEME_KEY, next ? 'light' : 'dark')
    } catch {
      return
    }
  }
  return { light: light || printing, dark: !light, toggle }
}

export function Shell() {
  const theme = useTheme()
  const runs = useRuns()
  const list = runs.data ?? []
  const live = list.find((r) => r.state === 'running') ?? list[0]
  const done = list.find((r) => r.state === 'finished')
  return (
    <div className={theme.light ? 'pub light' : 'pub'}>
      <div className="top">
        <div className="topin">
          <div className="brand">
            WSIGNAL <span>/ слабые сигналы</span>
          </div>
          <nav>
            <NavLink to="/" end className={({ isActive }) => (isActive ? 'on' : '')}>Запрос</NavLink>
            <NavLink to={live ? `/runs/${live.id}/live` : '/'} className={({ isActive }) => (isActive ? 'on' : '')}>Исследование</NavLink>
            <NavLink to={done ? `/runs/${done.id}` : '/'} end className={({ isActive }) => (isActive ? 'on' : '')}>Отчёт</NavLink>
          </nav>
          <Clock />
          <button className="theme" onClick={theme.toggle}>{theme.dark ? 'светлая тема' : 'тёмная тема'}</button>
        </div>
      </div>
      <main>
        <Outlet />
      </main>
    </div>
  )
}
