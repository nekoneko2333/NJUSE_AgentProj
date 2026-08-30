import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhCN from './locales/zh-CN.json'
import enUS from './locales/en-US.json'

const stored = localStorage.getItem('mosscode.locale')
i18n.use(initReactI18next).init({
  resources: { 'zh-CN': { translation: zhCN }, 'en-US': { translation: enUS } },
  lng: stored ?? 'zh-CN', fallbackLng: 'zh-CN', interpolation: { escapeValue: false }
})
i18n.on('languageChanged', (locale) => localStorage.setItem('mosscode.locale', locale))
export default i18n
