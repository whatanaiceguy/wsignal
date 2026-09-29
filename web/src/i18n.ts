import i18next from 'i18next'
import { initReactI18next } from 'react-i18next'
import { en } from './locales/en'
import { ru } from './locales/ru'

const STORAGE_KEY = 'wsignal.lang'

function initial(): 'ru' | 'en' {
  const fromUrl = new URLSearchParams(window.location.search).get('lang')
  if (fromUrl === 'ru' || fromUrl === 'en') return fromUrl
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === 'en' ? 'en' : 'ru'
}

void i18next.use(initReactI18next).init({
  resources: { ru, en },
  lng: initial(),
  fallbackLng: 'ru',
  interpolation: { escapeValue: false },
})

export function setLanguage(lang: 'ru' | 'en') {
  localStorage.setItem(STORAGE_KEY, lang)
  void i18next.changeLanguage(lang)
}

export default i18next

declare module 'i18next' {
  interface CustomTypeOptions {
    resources: typeof ru
  }
}
