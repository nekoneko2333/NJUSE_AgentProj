import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Bot, BrainCircuit, CircleCheck, CodeXml, KeyRound, PlugZap, Save, ScrollText, ShieldCheck, SlidersHorizontal, Workflow } from 'lucide-react'
import { getModelSettings, getProjectConfig, saveModelSettings, saveProjectConfig, type AgentRoleName, type AgentWorkflow, type ModelSettings, type ProjectConfig } from './api'

type ConfigKind = 'agents' | 'hooks' | 'mcp'
type SettingsSection = 'general' | 'agents' | 'memory' | 'rules' | 'hooks' | 'mcp'

const HOOKS_TEMPLATE = `{
  "enabled": false,
  "hooks": {
    "before_tool": [],
    "after_tool": [],
    "after_write": [],
    "before_finish": []
  }
}`

const MCP_TEMPLATE = `{
  "servers": {
    "example": {
      "enabled": false,
      "command": "node",
      "args": ["path/to/mcp-server.js"],
      "tools": {}
    }
  }
}`

type SettingsPanelProps = {
  sessionId: string | null
  crossSessionMemory: boolean
  onCrossSessionMemoryChange: (enabled: boolean) => Promise<void>
  agentWorkflow: AgentWorkflow
  onAgentWorkflowChange: (workflow: AgentWorkflow) => Promise<void>
  busy: boolean
}

const MULTI_ROLES: AgentRoleName[] = ['planner','explorer','coder','reviewer']

export function SettingsPanel({ sessionId, crossSessionMemory, onCrossSessionMemoryChange, agentWorkflow, onAgentWorkflowChange, busy }: SettingsPanelProps) {
  const { t } = useTranslation()
  const [model, setModel] = useState<ModelSettings | null>(null)
  const [project, setProject] = useState<ProjectConfig | null>(null)
  const [drafts, setDrafts] = useState<Record<ConfigKind, string>>({ agents:'', hooks:HOOKS_TEMPLATE, mcp:MCP_TEMPLATE })
  const [notice, setNotice] = useState('')
  const [savingConfig, setSavingConfig] = useState<ConfigKind | null>(null)
  const [workflowDraft, setWorkflowDraft] = useState<AgentWorkflow>(agentWorkflow)
  const [savingWorkflow, setSavingWorkflow] = useState(false)
  const [section, setSection] = useState<SettingsSection>('general')

  useEffect(() => setWorkflowDraft(agentWorkflow), [agentWorkflow])

  useEffect(() => {
    void getModelSettings().then(setModel).catch(() => setNotice(t('settings.loadFailed')))
  }, [t])

  useEffect(() => {
    if (!sessionId) { setProject(null); return }
    void getProjectConfig(sessionId).then((loaded) => {
      const normalized: ProjectConfig = {
        rules:{ ...loaded.rules, file:loaded.rules.file ?? { path:'AGENTS.md', exists:false, content:'', sha256:'' } },
        hooks:{ ...loaded.hooks, file:loaded.hooks.file ?? { path:'.mosscode/hooks.json', exists:false, content:'', sha256:'' } },
        mcp:{ ...loaded.mcp, file:loaded.mcp.file ?? { path:'.mosscode/mcp.json', exists:false, content:'', sha256:'' } },
      }
      setProject(normalized)
      setDrafts({
        agents:normalized.rules.file.content,
        hooks:normalized.hooks.file.exists ? normalized.hooks.file.content : HOOKS_TEMPLATE,
        mcp:normalized.mcp.file.exists ? normalized.mcp.file.content : MCP_TEMPLATE,
      })
    }).catch(() => setProject(null))
  }, [sessionId])

  const saveModel = async () => {
    if (!model) return
    setNotice('')
    try {
      setModel(await saveModelSettings({ base_url:model.base_url, model:model.model, context_budget_chars:model.context_budget_chars, max_turns:model.max_turns, command_timeout_seconds:model.command_timeout_seconds }))
      setNotice(t('settings.saved'))
    } catch { setNotice(t('settings.saveFailed')) }
  }

  const saveConfig = async (kind: ConfigKind) => {
    if (!sessionId || !project) return
    setNotice(''); setSavingConfig(kind)
    const file = kind === 'agents' ? project.rules.file : project[kind].file
    try {
      const updated = await saveProjectConfig(sessionId, kind, drafts[kind], file.sha256)
      setProject(updated)
      setDrafts({ agents:updated.rules.file.content, hooks:updated.hooks.file.content || HOOKS_TEMPLATE, mcp:updated.mcp.file.content || MCP_TEMPLATE })
      setNotice(t('settings.configSaved'))
    } catch (reason) {
      const code = reason instanceof Error ? reason.message : String(reason)
      setNotice(t(`errors.${code}`, { defaultValue:code }))
    } finally { setSavingConfig(null) }
  }

  const updateRole = (role: AgentRoleName, patch: Partial<AgentWorkflow['agent_config'][AgentRoleName]>) => setWorkflowDraft((current) => ({ ...current, agent_config:{ ...current.agent_config, [role]:{ ...current.agent_config[role], ...patch } } }))

  const saveWorkflow = async () => {
    setNotice(''); setSavingWorkflow(true)
    try { await onAgentWorkflowChange(workflowDraft); setNotice(t('settings.workflowSaved')) }
    catch { setNotice(t('settings.workflowSaveFailed')) }
    finally { setSavingWorkflow(false) }
  }

  const toggleMemory = async () => {
    setNotice('')
    try { await onCrossSessionMemoryChange(!crossSessionMemory); setNotice(t('settings.memorySaved')) }
    catch { setNotice(t('settings.memorySaveFailed')) }
  }

  const visibleRoles: AgentRoleName[] = workflowDraft.agent_mode === 'single' ? ['single'] : MULTI_ROLES
  const navigation = [
    { id:'general' as const, icon:<Workflow size={16}/>, title:t('settings.modelTitle'), detail:t('settings.modelDetail') },
    { id:'agents' as const, icon:<Bot size={16}/>, title:t('settings.workflowTitle'), detail:t('settings.workflowDetail') },
    { id:'memory' as const, icon:<BrainCircuit size={16}/>, title:t('settings.memoryTitle'), detail:t('settings.memoryDetail') },
    { id:'rules' as const, icon:<ScrollText size={16}/>, title:t('settings.rulesTitle'), detail:t('settings.rulesDetail') },
    { id:'hooks' as const, icon:<PlugZap size={16}/>, title:'Hooks', detail:t('settings.hooksDetail') },
    { id:'mcp' as const, icon:<CodeXml size={16}/>, title:'MCP', detail:t('settings.mcpDetail') },
  ]
  const currentNavigation = navigation.find((item) => item.id === section) ?? navigation[0]

  if (!model) return <div className="inspector-empty"><Workflow size={24}/><p>{notice || t('settings.loading')}</p></div>
  return <div className="settings-panel">
    <nav className="settings-nav" aria-label={t('settings.title')}>{navigation.map((item) => <button type="button" className={section === item.id ? 'active' : ''} aria-current={section === item.id ? 'page' : undefined} onClick={() => { setSection(item.id); setNotice('') }} key={item.id}>{item.icon}<span><strong>{item.title}</strong><small>{item.detail}</small></span></button>)}</nav>
    <section className="settings-detail">
      <header className="settings-detail-head">{currentNavigation.icon}<span><strong>{currentNavigation.title}</strong><small>{currentNavigation.detail}</small></span></header>

      {section === 'general' && <section className="settings-card"><label><span>{t('settings.baseUrl')}</span><input value={model.base_url} onChange={(event) => setModel({ ...model, base_url:event.target.value })}/></label><label><span>{t('settings.modelName')}</span><input value={model.model} onChange={(event) => setModel({ ...model, model:event.target.value })}/></label><div className="settings-numbers"><label><span>{t('settings.contextBudget')}</span><input type="number" min="2000" value={model.context_budget_chars} onChange={(event) => setModel({ ...model, context_budget_chars:Number(event.target.value) })}/></label><label><span>{t('settings.maxTurns')}</span><input type="number" min="1" max="50" value={model.max_turns} onChange={(event) => setModel({ ...model, max_turns:Number(event.target.value) })}/></label><label><span>{t('settings.commandTimeout')}</span><input type="number" min="1" max="600" value={model.command_timeout_seconds} onChange={(event) => setModel({ ...model, command_timeout_seconds:Number(event.target.value) })}/></label></div><p className={`key-state ${model.api_key_configured ? 'ok' : ''}`}>{model.api_key_configured ? <CircleCheck size={13}/> : <KeyRound size={13}/>} {t(model.api_key_configured ? 'settings.keyConfigured' : 'settings.keyMissing')}</p><button className="settings-save" type="button" onClick={() => void saveModel()}><Save size={13}/>{t('settings.save')}</button></section>}

      {section === 'agents' && <section className="settings-card workflow-card"><div className="workflow-mode" role="group" aria-label={t('settings.workflowMode')}>{(['multi','single','adaptive'] as const).map((mode) => <button type="button" className={workflowDraft.agent_mode === mode ? 'active' : ''} onClick={() => setWorkflowDraft({ ...workflowDraft, agent_mode:mode })} key={mode}><strong>{t(`settings.mode.${mode}`)}</strong><small>{t(`settings.mode.${mode}Detail`)}</small></button>)}</div><p className="config-help"><ShieldCheck size={13}/>{t(workflowDraft.agent_mode === 'single' ? 'settings.singleComparison' : workflowDraft.agent_mode === 'adaptive' ? 'settings.adaptiveReason' : 'settings.workflowReason')}</p><div className="workflow-roles">{visibleRoles.map((role, index) => { const config = workflowDraft.agent_config[role]; const locked = role === 'coder' || role === 'single'; return <details className={`workflow-role ${role}`} key={role} open={workflowDraft.agent_mode === 'single'}><summary><span>{workflowDraft.agent_mode === 'single' ? <SlidersHorizontal size={13}/> : index + 1}</span><div><strong>{t(`roles.${role}`)}</strong><small>{t(`roleGoals.${role}`)}</small></div><button type="button" className="mini-switch" role="switch" aria-checked={config.enabled} disabled={locked || busy} title={locked ? t('settings.requiredRole') : t('settings.toggleRole')} onClick={(event) => { event.preventDefault(); updateRole(role, { enabled:!config.enabled }) }}><i className={config.enabled ? 'on' : ''}><b/></i></button></summary><div className="role-options"><label><span>{t('settings.roleTurns')}</span><input type="number" min="1" max="30" value={config.max_turns} onChange={(event) => updateRole(role, { max_turns:Math.max(1, Math.min(30, Number(event.target.value) || 1)) })}/></label><label><span>{t('settings.roleInstruction')}</span><textarea maxLength={2000} value={config.instruction} placeholder={t('settings.roleInstructionPlaceholder')} onChange={(event) => updateRole(role, { instruction:event.target.value })}/></label></div></details> })}</div><button className="settings-save" type="button" disabled={savingWorkflow || busy} onClick={() => void saveWorkflow()}><Save size={13}/>{t(savingWorkflow ? 'settings.saving' : 'settings.saveWorkflow')}</button></section>}

      {section === 'memory' && <section className="settings-card"><button className="setting-switch-row" type="button" role="switch" aria-checked={crossSessionMemory} disabled={busy} onClick={() => void toggleMemory()}><span>{t(crossSessionMemory ? 'memory.enabled' : 'memory.disabled')}</span><i className={crossSessionMemory ? 'on' : ''}><b/></i></button><p className="config-help">{t('settings.memoryPrivacy')}</p></section>}

      {['rules','hooks','mcp'].includes(section) && (!sessionId || !project) && <section className="settings-card"><p className="config-empty">{t('settings.selectConversation')}</p></section>}

      {section === 'rules' && sessionId && project && <section className="settings-card config-editor config-editor-page"><p className="config-help">{t('settings.rulesHelp')}</p>{project.rules.sources.length > 0 && <ul className="config-list">{project.rules.sources.map((source) => <li key={source.path}><span>{source.path}</span><small>{source.chars}</small></li>)}</ul>}<label><span>AGENTS.md</span><textarea value={drafts.agents} placeholder={t('settings.agentsPlaceholder')} onChange={(event) => setDrafts({ ...drafts, agents:event.target.value })}/></label><button className="settings-save" type="button" disabled={savingConfig !== null} onClick={() => void saveConfig('agents')}><Save size={13}/>{t(savingConfig === 'agents' ? 'settings.saving' : 'settings.saveRules')}</button></section>}

      {section === 'hooks' && sessionId && project && <section className="settings-card config-editor config-editor-page"><p className="config-help">{t('settings.hooksHelp')}</p><div className="config-event-grid">{['before_tool','after_tool','after_write','before_finish'].map((event) => <span key={event}><code>{event}</code><small>{project.hooks.events[event] ?? 0}</small></span>)}</div><label><span>.mosscode/hooks.json</span><textarea className="json-editor" spellCheck={false} value={drafts.hooks} onChange={(event) => setDrafts({ ...drafts, hooks:event.target.value })}/></label><button className="settings-save" type="button" disabled={savingConfig !== null} onClick={() => void saveConfig('hooks')}><Save size={13}/>{t(savingConfig === 'hooks' ? 'settings.saving' : 'settings.saveHooks')}</button></section>}

      {section === 'mcp' && sessionId && project && <section className="settings-card config-editor config-editor-page"><p className="config-help">{t('settings.mcpHelp')}</p>{project.mcp.tools.length > 0 && <ul className="config-list">{project.mcp.tools.map((tool) => <li key={tool}><span>{tool}</span></li>)}</ul>}<label><span>.mosscode/mcp.json</span><textarea className="json-editor" spellCheck={false} value={drafts.mcp} onChange={(event) => setDrafts({ ...drafts, mcp:event.target.value })}/></label><button className="settings-save" type="button" disabled={savingConfig !== null} onClick={() => void saveConfig('mcp')}><Save size={13}/>{t(savingConfig === 'mcp' ? 'settings.saving' : 'settings.saveMcp')}</button></section>}

      {notice && <p className="settings-notice">{notice}</p>}
    </section>
  </div>
}
