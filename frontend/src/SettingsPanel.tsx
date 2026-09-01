import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CircleCheck, KeyRound, PlugZap, Save, ScrollText, Workflow } from 'lucide-react'
import { getModelSettings, getProjectConfig, saveModelSettings, type ModelSettings, type ProjectConfig } from './api'

export function SettingsPanel({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation()
  const [model, setModel] = useState<ModelSettings | null>(null)
  const [project, setProject] = useState<ProjectConfig | null>(null)
  const [notice, setNotice] = useState('')
  useEffect(() => {
    void getModelSettings().then(setModel).catch(() => setNotice(t('settings.loadFailed')))
    if (sessionId) void getProjectConfig(sessionId).then(setProject).catch(() => setProject(null))
    else setProject(null)
  }, [sessionId, t])
  const save = async () => {
    if (!model) return
    setNotice('')
    try {
      setModel(await saveModelSettings({ base_url:model.base_url, model:model.model, context_budget_chars:model.context_budget_chars, max_turns:model.max_turns, command_timeout_seconds:model.command_timeout_seconds }))
      setNotice(t('settings.saved'))
    } catch { setNotice(t('settings.saveFailed')) }
  }
  if (!model) return <div className="inspector-empty"><Workflow size={24}/><p>{notice || t('settings.loading')}</p></div>
  return <div className="settings-panel">
    <section className="settings-card"><header><Workflow size={15}/><div><strong>{t('settings.modelTitle')}</strong><small>{t('settings.modelDetail')}</small></div></header><label><span>{t('settings.baseUrl')}</span><input value={model.base_url} onChange={(event) => setModel({ ...model, base_url:event.target.value })}/></label><label><span>{t('settings.modelName')}</span><input value={model.model} onChange={(event) => setModel({ ...model, model:event.target.value })}/></label><div className="settings-numbers"><label><span>{t('settings.contextBudget')}</span><input type="number" min="2000" value={model.context_budget_chars} onChange={(event) => setModel({ ...model, context_budget_chars:Number(event.target.value) })}/></label><label><span>{t('settings.maxTurns')}</span><input type="number" min="1" max="50" value={model.max_turns} onChange={(event) => setModel({ ...model, max_turns:Number(event.target.value) })}/></label><label><span>{t('settings.commandTimeout')}</span><input type="number" min="1" max="600" value={model.command_timeout_seconds} onChange={(event) => setModel({ ...model, command_timeout_seconds:Number(event.target.value) })}/></label></div><p className={`key-state ${model.api_key_configured ? 'ok' : ''}`}>{model.api_key_configured ? <CircleCheck size={13}/> : <KeyRound size={13}/>} {t(model.api_key_configured ? 'settings.keyConfigured' : 'settings.keyMissing')}</p><button className="settings-save" type="button" onClick={() => void save()}><Save size={13}/>{t('settings.save')}</button>{notice && <p className="settings-notice">{notice}</p>}</section>
    <section className="settings-card"><header><ScrollText size={15}/><div><strong>{t('settings.rulesTitle')}</strong><small>{t('settings.rulesDetail')}</small></div></header>{project?.rules.sources.length ? <><ul className="config-list">{project.rules.sources.map((source) => <li key={source.path}><span>{source.path}</span><small>{source.chars}</small></li>)}</ul><pre className="rules-preview">{project.rules.text}</pre></> : <p className="config-empty">{t('settings.noRules')}</p>}</section>
    <section className="settings-card config-columns"><div><header><PlugZap size={15}/><div><strong>Hooks</strong><small>{t('settings.hooksDetail')}</small></div></header><p className="config-state">{t(project?.hooks.enabled ? 'settings.enabled' : project?.hooks.configured ? 'settings.disabled' : 'settings.notConfigured')}</p></div><div><header><Workflow size={15}/><div><strong>MCP</strong><small>{t('settings.mcpDetail')}</small></div></header><p className="config-state">{project?.mcp.tools.length ? project.mcp.tools.join(', ') : t(project?.mcp.configured ? 'settings.noTools' : 'settings.notConfigured')}</p></div></section>
  </div>
}
