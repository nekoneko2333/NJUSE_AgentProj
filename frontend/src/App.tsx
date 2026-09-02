import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowDown, Bot, Check, ChevronRight, CircleAlert, CircleCheck, CircleX, Code2, FileText, Folder, FolderTree, GitBranch, KeyRound, LoaderCircle, LogOut, MessageSquareText, Moon, Play, Plus, Redo2, Settings2, ShieldCheck, Sparkles, Square, Sun, TerminalSquare, UserRound, X } from 'lucide-react'
import { appendTurn, cancelExecution, createSession, DEFAULT_AGENT_WORKFLOW, getAuthStatus, getExecution, getLatestExecution, getSession, getWorkspaceFiles, listSessions, login, logout, resolveApproval, retryTurn, runAgent, setAgentWorkflow, setCommandMode, setCrossSessionMemory, subscribe, type AgentEvent, type AgentRoleName, type AgentWorkflow, type CommandMode, type Session, type SessionListItem, type WorkspaceEntry } from './api'
import { FileWorkbench } from './FileWorkbench'
import { SelectMenu } from './SelectMenu'
import { SettingsPanel } from './SettingsPanel'
import { SideBySideDiff } from './SideBySideDiff'
import './styles/app.css'

type Tab = 'files' | 'diff' | 'terminal'
type Theme = 'light' | 'dusk'
type ToolResult = { ok?: boolean; code?: string; content?: string; meta?: Record<string, unknown> }
type TreeNode = { name: string; path: string; kind: 'file' | 'directory'; children: TreeNode[] }
type DiffEntry = { path: string; diff: string }
type TerminalRun = { command: string; output: string; ok: boolean; exitCode: string | number }
type ConversationMarker = { id: string; label: string; top: number }
const normalizePath = (value: string) => value.trim().replace(/[\\/]+$/, '').toLowerCase()
const formatElapsed = (seconds: number) => seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
const appendUniqueEvent = (current: AgentEvent[], event: AgentEvent) => current.some((item) => item.type === event.type && item.turn_id === event.turn_id && item.role === event.role && item.summary === event.summary && JSON.stringify(item.payload) === JSON.stringify(event.payload)) ? current : [...current, event]
const terminalEventTypes = new Set(['task_finished', 'task_failed', 'task_cancelled'])

export function selectDisplayEvents(events: AgentEvent[]) {
  const terminalIndexes = new Map<string, number[]>()
  const latestMilestones = new Map<string, number>()
  const latestAttemptStarts = new Map<string, number>()
  events.forEach((event, index) => {
    const turnKey = event.turn_id ?? 'session'
    if (terminalEventTypes.has(event.type)) terminalIndexes.set(turnKey, [...(terminalIndexes.get(turnKey) ?? []), index])
    if (event.type === 'agent_started') latestAttemptStarts.set(turnKey, index)
    if (event.type !== 'tool_finished') return
    const tool = String(event.payload.tool ?? '')
    if (tool === 'write_file' || tool === 'run_command') latestMilestones.set(`${turnKey}:${tool}`, index)
  })
  return events.map((event, sourceIndex) => ({ event, sourceIndex })).filter(({ event, sourceIndex }) => {
    const turnKey = event.turn_id ?? 'session'
    const terminals = terminalIndexes.get(turnKey) ?? []
    const latestTerminal = terminals.at(-1)
    const previousTerminal = terminals.at(-2) ?? -1
    const latestAttemptStart = latestAttemptStarts.get(turnKey) ?? -1
    const previousAttemptBoundary = latestTerminal !== undefined && latestAttemptStart > latestTerminal ? latestTerminal : previousTerminal
    if (event.type === 'task_created') return true
    if (terminalEventTypes.has(event.type)) return sourceIndex === latestTerminal && latestAttemptStart <= sourceIndex
    if (sourceIndex <= previousAttemptBoundary) return false
    if (event.type === 'agent_started' || event.type === 'approval_requested' || event.type === 'approval_resolved') return true
    if (event.type !== 'tool_finished') return false
    const tool = String(event.payload.tool ?? '')
    if (tool !== 'write_file' && tool !== 'run_command') return false
    return latestMilestones.get(`${turnKey}:${tool}`) === sourceIndex
  })
}
const normalizeWorkflow = (session?: Pick<Session, 'agent_mode' | 'agent_config'>): AgentWorkflow => ({
  agent_mode:session?.agent_mode ?? DEFAULT_AGENT_WORKFLOW.agent_mode,
  agent_config:Object.fromEntries((Object.keys(DEFAULT_AGENT_WORKFLOW.agent_config) as AgentRoleName[]).map((role) => [role, { ...DEFAULT_AGENT_WORKFLOW.agent_config[role], ...(session?.agent_config?.[role] ?? {}) }])) as AgentWorkflow['agent_config'],
})

export function normalizeMarkdownLinks(value: string) {
  return value
    .replace(/(\*\*https?:\/\/[^\s*]+\*\*)(?=[（），。；：！？])/g, '$1 ')
    .replace(/(https?:\/\/[^\s<>()\]）*，。；：！？]+)(?=[（），。；：！？])/g, '$1 ')
}

function Markdown({ children }: { children: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{normalizeMarkdownLinks(children)}</ReactMarkdown></div>
}

function buildTree(entries: WorkspaceEntry[]): TreeNode[] {
  const roots: TreeNode[] = []
  for (const entry of entries) {
    const parts = entry.path.split(/[\\/]+/).filter(Boolean)
    let level = roots
    parts.forEach((name, index) => {
      const path = parts.slice(0, index + 1).join('/')
      let node = level.find((candidate) => candidate.name === name)
      if (!node) {
        node = { name, path, kind: index === parts.length - 1 ? entry.kind : 'directory', children: [] }
        level.push(node)
      } else if (index === parts.length - 1) node.kind = entry.kind
      level = node.children
    })
  }
  const sort = (nodes: TreeNode[]): TreeNode[] => nodes.sort((a, b) => Number(a.kind === 'file') - Number(b.kind === 'file') || a.name.localeCompare(b.name)).map((node) => ({ ...node, children: sort(node.children) }))
  return sort(roots)
}

export default function App() {
  const { t, i18n } = useTranslation()
  const [task, setTask] = useState('')
  const [workspace, setWorkspace] = useState('')
  const [submittedTask, setSubmittedTask] = useState('')
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState<Tab>('files')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [error, setError] = useState('')
  const [workspaceEntries, setWorkspaceEntries] = useState<WorkspaceEntry[]>([])
  const [selectedDiff, setSelectedDiff] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [executionId, setExecutionId] = useState<string | null>(null)
  const [retryableTurnId, setRetryableTurnId] = useState<string | null>(null)
  const [sessionWorkspace, setSessionWorkspace] = useState('')
  const [sessionItems, setSessionItems] = useState<SessionListItem[]>([])
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const [commandMode, setCommandModeState] = useState<CommandMode>('auto')
  const [crossSessionMemory, setCrossSessionMemoryState] = useState(() => window.localStorage.getItem('mosscode.crossSessionMemory') === 'true')
  const [agentWorkflow, setAgentWorkflowState] = useState<AgentWorkflow>(() => normalizeWorkflow())
  const [theme, setTheme] = useState<Theme>(() => window.localStorage.getItem('mosscode.theme') === 'dusk' ? 'dusk' : 'light')
  const [authReady, setAuthReady] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [username, setUsername] = useState('')
  const [loginName, setLoginName] = useState('moss')
  const [loginPassword, setLoginPassword] = useState('')
  const [loginError, setLoginError] = useState('')
  const [loginBusy, setLoginBusy] = useState(false)
  const messageStream = useRef<HTMLDivElement>(null)
  const followsLatest = useRef(true)
  const [hasNewProgress, setHasNewProgress] = useState(false)
  const [conversationMarkers, setConversationMarkers] = useState<ConversationMarker[]>([])
  const [activeUserTurn, setActiveUserTurn] = useState('')
  const pollTimer = useRef<number | null>(null)
  const loadVersion = useRef(0)
  const activeSession = useRef<string | null>(null)

  const stopPolling = () => {
    if (pollTimer.current !== null) window.clearInterval(pollTimer.current)
    pollTimer.current = null
  }

  useEffect(() => {
    const stream = messageStream.current
    if (!stream) return
    if (!followsLatest.current) { setHasNewProgress(true); return }
    const frame = window.requestAnimationFrame(() => stream.scrollTo({ top: stream.scrollHeight, behavior: 'smooth' }))
    return () => window.cancelAnimationFrame(frame)
  }, [events, busy])

  const refreshConversationRail = useCallback(() => {
    const stream = messageStream.current
    if (!stream) return
    const nodes = [...stream.querySelectorAll<HTMLElement>('[data-user-turn]')]
    const range = Math.max(1, stream.scrollHeight - stream.clientHeight)
    const markers = nodes.map((node, index) => ({
      id: node.dataset.userTurn ?? String(index),
      label: node.dataset.turnLabel ?? String(index + 1),
      top: Math.min(97, Math.max(3, ((node.offsetTop - 12) / range) * 100)),
    }))
    setConversationMarkers(markers)
    let active = markers[0]?.id ?? ''
    const readingLine = stream.scrollTop + Math.min(160, stream.clientHeight * .3)
    nodes.forEach((node, index) => { if (node.offsetTop <= readingLine) active = markers[index]?.id ?? active })
    setActiveUserTurn(active)
  }, [])

  useEffect(() => {
    const stream = messageStream.current
    if (!stream) return
    const frame = window.requestAnimationFrame(refreshConversationRail)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(refreshConversationRail)
    observer?.observe(stream)
    stream.querySelectorAll<HTMLElement>('[data-user-turn]').forEach((node) => observer?.observe(node))
    window.addEventListener('resize', refreshConversationRail)
    return () => { window.cancelAnimationFrame(frame); observer?.disconnect(); window.removeEventListener('resize', refreshConversationRail) }
  }, [events, submittedTask, busy, refreshConversationRail])

  const handleStreamScroll = useCallback(() => {
    const stream = messageStream.current
    if (!stream) return
    const nearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 72
    followsLatest.current = nearBottom
    if (nearBottom) setHasNewProgress(false)
    const nodes = [...stream.querySelectorAll<HTMLElement>('[data-user-turn]')]
    const readingLine = stream.scrollTop + Math.min(160, stream.clientHeight * .3)
    let active = nodes[0]?.dataset.userTurn ?? ''
    nodes.forEach((node) => { if (node.offsetTop <= readingLine) active = node.dataset.userTurn ?? active })
    setActiveUserTurn((current) => current === active ? current : active)
  }, [])

  const jumpToUserTurn = useCallback((turnId: string) => {
    const stream = messageStream.current
    if (!stream) return
    const target = [...stream.querySelectorAll<HTMLElement>('[data-user-turn]')].find((node) => node.dataset.userTurn === turnId)
    if (!target) return
    followsLatest.current = false
    setHasNewProgress(false)
    setActiveUserTurn(turnId)
    stream.scrollTo({ top: Math.max(0, target.offsetTop - 18), behavior: 'smooth' })
  }, [])

  const jumpToLatest = useCallback(() => {
    const stream = messageStream.current
    if (!stream) return
    followsLatest.current = true
    setHasNewProgress(false)
    stream.scrollTo({ top: stream.scrollHeight, behavior: 'smooth' })
  }, [])

  const reportApprovalError = useCallback(() => setError(t('errors.approval_not_found')), [t])

  useEffect(() => {
    if (!busy || startedAt === null) return
    const update = () => setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [busy, startedAt])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const status = await getAuthStatus()
        if (cancelled) return
        setAuthenticated(status.authenticated); setUsername(status.username); setAuthReady(true)
        if (status.authenticated) {
          const savedSession = window.localStorage.getItem('mosscode.lastSession')
          await refreshSessions()
          if (savedSession) await loadSession(savedSession).catch(() => window.localStorage.removeItem('mosscode.lastSession'))
        }
      } catch { if (!cancelled) setAuthReady(true) }
    })()
    return () => { cancelled = true; stopPolling() }
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    window.localStorage.setItem('mosscode.theme', theme)
  }, [theme])

  useEffect(() => {
    if (!settingsOpen) return
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setSettingsOpen(false) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [settingsOpen])

  const refreshSessions = async () => setSessionItems(await listSessions().catch(() => []))

  const loadSession = async (id: string) => {
    const version = ++loadVersion.current
    activeSession.current = id
    stopPolling()
    const session = await getSession(id)
    if (version !== loadVersion.current || activeSession.current !== id) return
    setSessionId(session.id); setSessionWorkspace(session.workspace); setTask(''); setSubmittedTask(session.task); setWorkspace(session.workspace); setEvents(session.events); setError('')
    setCommandModeState(session.command_mode)
    setCrossSessionMemoryState(session.cross_session_memory_enabled)
    setAgentWorkflowState(normalizeWorkflow(session))
    setRetryableTurnId([...session.turns].reverse().find((turn) => ['failed', 'cancelled', 'interrupted'].includes(turn.status))?.id ?? null)
    if (session.status === 'interrupted') setError(t('errors.execution_interrupted'))
    const files = await getWorkspaceFiles(session.id).catch(() => [])
    if (version !== loadVersion.current || activeSession.current !== id) return
    setWorkspaceEntries(files)
    window.localStorage.setItem('mosscode.lastSession', session.id)
    if (session.status === 'running') {
      setBusy(true)
      setStartedAt(Number.isFinite(Date.parse(session.updated_at)) ? Date.parse(session.updated_at) : Date.now())
      const latestExecution = await getLatestExecution(session.id).catch(() => null)
      if (latestExecution && ['queued', 'running', 'waiting_approval', 'cancel_requested'].includes(latestExecution.status)) setExecutionId(latestExecution.id)
      let polling = false
      pollTimer.current = window.setInterval(async () => {
        if (polling) return
        polling = true
        try {
          const refreshed = await getSession(session.id)
          if (activeSession.current !== session.id || version !== loadVersion.current) return
          setEvents(refreshed.events); setSubmittedTask(refreshed.task)
          if (refreshed.status !== 'running') {
            stopPolling(); setBusy(false); setStartedAt(null); setExecutionId(null)
            setWorkspaceEntries(await getWorkspaceFiles(session.id).catch(() => []))
            await refreshSessions()
          }
        } catch {
          // 短暂断线时保留运行态，下一次轮询自动恢复，避免误报任务结束。
        } finally { polling = false }
      }, 2000)
    } else {
      setBusy(false); setStartedAt(null); setExecutionId(null)
    }
  }

  const newConversation = () => {
    if (busy) return
    stopPolling()
    loadVersion.current += 1; activeSession.current = null
    setSessionId(null); setSessionWorkspace(''); setTask(''); setSubmittedTask(''); setEvents([]); setWorkspaceEntries([]); setSelectedDiff(''); setError(''); setSettingsOpen(false)
    window.localStorage.removeItem('mosscode.lastSession')
  }

  const submitLogin = async () => {
    if (!loginName.trim() || !loginPassword) return
    setLoginBusy(true); setLoginError('')
    try {
      const result = await login(loginName.trim(), loginPassword)
      setAuthenticated(true); setUsername(result.username); setLoginPassword('')
      await refreshSessions()
      const savedSession = window.localStorage.getItem('mosscode.lastSession')
      if (savedSession) await loadSession(savedSession).catch(() => window.localStorage.removeItem('mosscode.lastSession'))
    } catch (reason) {
      const code = reason instanceof Error ? reason.message : String(reason)
      setLoginError(t(`errors.${code}`, { defaultValue: code }))
    } finally { setLoginBusy(false) }
  }

  const submitLogout = async () => {
    await logout().catch(() => undefined)
    stopPolling(); loadVersion.current += 1; activeSession.current = null
    setAuthenticated(false); setUsername(''); setSessionId(null); setEvents([]); setWorkspaceEntries([])
  }

  const changeCommandMode = async (mode: CommandMode) => {
    const previous = commandMode
    setCommandModeState(mode); setError('')
    if (!sessionId || workspaceChanged) return
    try { await setCommandMode(sessionId, mode); await refreshSessions() }
    catch (reason) {
      setCommandModeState(previous)
      const code = reason instanceof Error ? reason.message : String(reason)
      setError(t(`errors.${code}`, { defaultValue: code }))
    }
  }

  const changeAgentWorkflow = async (workflow: AgentWorkflow) => {
    const previous = agentWorkflow
    setAgentWorkflowState(workflow); setError('')
    if (!sessionId || workspaceChanged) return
    try { await setAgentWorkflow(sessionId, workflow); await refreshSessions() }
    catch (reason) {
      setAgentWorkflowState(previous)
      const code = reason instanceof Error ? reason.message : String(reason)
      setError(t(`errors.${code}`, { defaultValue: code }))
      throw reason
    }
  }

  const changeCrossSessionMemory = async (enabled: boolean) => {
    const previous = crossSessionMemory
    setCrossSessionMemoryState(enabled); setError('')
    window.localStorage.setItem('mosscode.crossSessionMemory', String(enabled))
    if (!sessionId || workspaceChanged) return
    try { await setCrossSessionMemory(sessionId, enabled); await refreshSessions() }
    catch (reason) {
      setCrossSessionMemoryState(previous)
      window.localStorage.setItem('mosscode.crossSessionMemory', String(previous))
      const code = reason instanceof Error ? reason.message : String(reason)
      setError(t(`errors.${code}`, { defaultValue: code }))
      throw reason
    }
  }

  const launch = async () => {
    if (!task.trim() || !workspace.trim()) return
    const requestedTask = task.trim()
    followsLatest.current = true
    setHasNewProgress(false)
    if (busy) {
      if (!sessionId) return
      try {
        setTask(''); setError('')
        const queued = await appendTurn(sessionId, requestedTask, i18n.language)
        const queuedEvent = queued.events.filter((event) => event.type === 'task_created').at(-1)
        if (queuedEvent) setEvents((current) => current.some((event) => event.type === 'task_created' && event.turn_id === queuedEvent.turn_id) ? current : [...current, queuedEvent])
        await refreshSessions()
      } catch (reason) {
        setTask(requestedTask)
        const code = reason instanceof Error ? reason.message : String(reason)
        setError(reason instanceof TypeError ? t('networkError') : t(`errors.${code}`, { defaultValue: code }))
      }
      return
    }
    const continuesSession = Boolean(sessionId) && normalizePath(workspace) === normalizePath(sessionWorkspace)
    setStartedAt(Date.now()); setElapsedSeconds(0)
    setBusy(true); setError(''); setSubmittedTask(requestedTask); setWorkspaceEntries([]); setSelectedDiff('')
    let close: () => void = () => undefined
    try {
      const session = continuesSession && sessionId
        ? await appendTurn(sessionId, requestedTask, i18n.language)
        : await createSession(requestedTask, workspace.trim(), i18n.language, commandMode, crossSessionMemory, agentWorkflow)
      setSessionId(session.id)
      activeSession.current = session.id
      setSessionWorkspace(session.workspace)
      window.localStorage.setItem('mosscode.lastSession', session.id)
      setTask('')
      setEvents(session.events)
      setWorkspaceEntries(await getWorkspaceFiles(session.id).catch(() => []))
      close = subscribe(session.id, (event) => {
        if (activeSession.current !== session.id) return
      setEvents((current) => appendUniqueEvent(current, event))
      })
      let execution = await runAgent(session.id)
      setExecutionId(execution.id)
      while (['queued', 'running', 'waiting_approval', 'cancel_requested'].includes(execution.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 500))
        execution = await getExecution(execution.id)
      }
      close()
      const refreshed = await getSession(session.id)
      if (activeSession.current === session.id) { setEvents(refreshed.events); setSubmittedTask(refreshed.task); setCommandModeState(refreshed.command_mode); setCrossSessionMemoryState(refreshed.cross_session_memory_enabled) }
      setRetryableTurnId([...refreshed.turns].reverse().find((turn) => ['failed', 'cancelled', 'interrupted'].includes(turn.status))?.id ?? null)
      setWorkspaceEntries(await getWorkspaceFiles(session.id).catch(() => []))
      await refreshSessions()
    } catch (reason) {
      close()
      if (reason instanceof TypeError) setError(t('networkError'))
      else {
        const code = reason instanceof Error ? reason.message : String(reason)
        setError(t(`errors.${code}`, { defaultValue: code }))
      }
    } finally { setBusy(false); setStartedAt(null); setExecutionId(null) }
  }

  const retryFailedTurn = useCallback(async (turnId: string) => {
    if (busy || !sessionId) return
    setBusy(true); setStartedAt(Date.now()); setElapsedSeconds(0); setError('')
    const close = subscribe(sessionId, (event) => {
      if (activeSession.current !== sessionId) return
      setEvents((current) => appendUniqueEvent(current, event))
    })
    try {
      let execution = await retryTurn(turnId)
      setExecutionId(execution.id)
      while (['queued', 'running', 'waiting_approval', 'cancel_requested'].includes(execution.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 500))
        execution = await getExecution(execution.id)
      }
      const refreshed = await getSession(sessionId)
      setEvents(refreshed.events); setSubmittedTask(refreshed.task)
      setRetryableTurnId([...refreshed.turns].reverse().find((turn) => ['failed', 'cancelled', 'interrupted'].includes(turn.status))?.id ?? null)
      setWorkspaceEntries(await getWorkspaceFiles(sessionId).catch(() => []))
      await refreshSessions()
    } catch (reason) {
      const code = reason instanceof Error ? reason.message : String(reason)
      setError(reason instanceof TypeError ? t('networkError') : t(`errors.${code}`, { defaultValue: code }))
    } finally { close(); setBusy(false); setStartedAt(null); setExecutionId(null) }
  }, [busy, sessionId, t])

  const toolEvents = events.filter((event) => event.type === 'tool_finished')
  const diffEntries = useMemo(() => {
    const latest = new Map<string, string>()
    toolEvents.filter((event) => ['write_file', 'replace_text'].includes(String(event.payload.tool))).forEach((event) => {
      const result = event.payload.result as ToolResult
      const path = String(result?.meta?.path ?? '')
      const diff = String(result?.meta?.diff ?? '')
      if (path) latest.set(path.replaceAll('\\', '/'), diff)
    })
    return [...latest].map(([path, diff]) => ({ path, diff }))
  }, [events])
  const terminalRuns = useMemo(() => toolEvents.filter((event) => event.payload.tool === 'run_command').map((event) => {
    const result = event.payload.result as ToolResult
    const arguments_ = event.payload.arguments as { command?: string } | undefined
    return { command: arguments_?.command ?? '', output: result?.content ?? '', ok: result?.ok !== false, exitCode: String(result?.meta?.exit_code ?? '?') } satisfies TerminalRun
  }), [events])
  const changedFiles = useMemo(() => (events.filter((event) => event.type === 'task_finished').at(-1)?.payload.changed_files ?? []) as string[], [events])
  const diffTree = useMemo(() => buildTree(diffEntries.map(({ path }) => ({ path, kind: 'file' }))), [diffEntries])
  const activeDiff = diffEntries.find((entry) => entry.path === selectedDiff) ?? diffEntries[0]
  const displayEvents = useMemo(() => selectDisplayEvents(events), [events])
  const hasStoredUserEvents = events.some((event) => event.type === 'task_created' && typeof event.payload.task === 'string')
  const queuedCount = Math.max(0, events.filter((event) => event.type === 'task_created').length - events.filter((event) => event.type === 'task_finished' || event.type === 'task_failed').length - (busy ? 1 : 0))
  const workspaceChanged = Boolean(sessionId) && normalizePath(workspace) !== normalizePath(sessionWorkspace)
  const commandModeOptions = useMemo(() => ([
    { value:'auto' as CommandMode, label:t('permissions.auto'), description:t('permissions.autoMenuDetail') },
    { value:'ask' as CommandMode, label:t('permissions.ask'), description:t('permissions.askMenuDetail') },
    { value:'deny' as CommandMode, label:t('permissions.deny'), description:t('permissions.denyMenuDetail') },
  ]), [t])

  if (!authReady) return <div className="auth-shell"><div className="auth-loader"><span className="brand-mark"><Bot size={24}/></span><LoaderCircle className="spin" size={20}/><p>{t('auth.checking')}</p></div></div>
  if (!authenticated) return <div className="auth-shell">
    <div className="auth-blob auth-blob-one"/><div className="auth-blob auth-blob-two"/>
    <button className="theme-float" type="button" onClick={() => setTheme(theme === 'light' ? 'dusk' : 'light')} aria-label={t('theme.toggle')}>{theme === 'light' ? <Moon size={18}/> : <Sun size={18}/>}</button>
    <section className="login-card">
      <div className="login-emblem"><Bot size={28}/></div>
      <p className="login-kicker">MossCode · Local workspace</p>
      <h1>{t('auth.welcome')}</h1><p className="login-copy">{t('auth.detail')}</p>
      <label><span>{t('auth.username')}</span><div className="login-input"><UserRound size={17}/><input value={loginName} onChange={(event) => setLoginName(event.target.value)} autoComplete="username"/></div></label>
      <label><span>{t('auth.password')}</span><div className="login-input"><KeyRound size={17}/><input type="password" value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void submitLogin() }} autoComplete="current-password"/></div></label>
      {loginError && <p className="login-error"><CircleAlert size={15}/>{loginError}</p>}
      <button className="login-button" type="button" disabled={loginBusy || !loginName.trim() || !loginPassword} onClick={() => void submitLogin()}>{loginBusy ? <LoaderCircle className="spin" size={17}/> : <ShieldCheck size={17}/>} {t(loginBusy ? 'auth.signingIn' : 'auth.signIn')}</button>
      <p className="login-note">{t('auth.localOnly')}</p>
    </section>
  </div>

  return <main className="app-shell">
    <div className="ambient ambient-one"/><div className="ambient ambient-two"/>
    <header className="topbar">
      <div className="brand"><span className="brand-mark"><Bot size={21}/></span><div><h1>{t('appName')}</h1><p>{t('tagline')}</p></div></div>
      <div className="run-state"><span className={busy ? 'pulse' : 'ready-dot'}/>{busy ? `${t('running')} · ${formatElapsed(elapsedSeconds)}` : t('ready')}</div>
      <div className="topbar-actions"><div className="locale" aria-label={t('language')}><button className={i18n.language === 'zh-CN' ? 'active' : ''} onClick={() => i18n.changeLanguage('zh-CN')}>中文</button><button className={i18n.language === 'en-US' ? 'active' : ''} onClick={() => i18n.changeLanguage('en-US')}>EN</button></div><button className="icon-action" type="button" onClick={() => setTheme(theme === 'light' ? 'dusk' : 'light')} title={t('theme.toggle')}>{theme === 'light' ? <Moon size={16}/> : <Sun size={16}/>}</button><div className="user-chip"><span><UserRound size={14}/>{username}</span><button type="button" onClick={() => void submitLogout()} title={t('auth.signOut')}><LogOut size={14}/></button></div></div>
    </header>
    <section className={`workspace-layout ${tab === 'diff' ? 'diff-focused' : ''}`}>
      <aside className="task-sidebar">
        <div className="sidebar-title"><MessageSquareText size={17}/><span>{t('conversations')}</span><button type="button" onClick={newConversation} disabled={busy} title={t('newTask')}><Plus size={15}/></button></div>
        <div className="session-list">{sessionItems.length ? sessionItems.map((session) => <button type="button" className={session.id === sessionId ? 'active' : ''} onClick={() => void loadSession(session.id)} disabled={busy} key={session.id}><strong>{session.title}</strong><small>{t('turnCount', { count: session.turn_count })}</small></button>) : <p>{t('noConversations')}</p>}</div>
        <button className="new-conversation" type="button" onClick={newConversation} disabled={busy}><Plus size={15}/>{t('newTask')}</button>
        <label className="field"><span>{t('workspace')}</span><input value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder={t('workspacePlaceholder')} disabled={busy}/><small>{workspaceChanged ? t('workspaceChanged') : sessionId ? t('workspaceActive') : t('workspaceHint')}</small></label>
        <div className="field permission-field"><span><ShieldCheck size={14}/>{t('permissions.title')}</span><SelectMenu label={t('permissions.title')} value={commandMode} options={commandModeOptions} disabled={busy} onChange={(value) => void changeCommandMode(value)}/><small>{t(`permissions.${commandMode}Detail`)}</small></div>
        <p className="safe-note"><CircleAlert size={15}/><span>{t('safeExecution')}</span></p>
        <button className={`sidebar-settings ${settingsOpen ? 'active' : ''}`} type="button" onClick={() => setSettingsOpen(true)}><Settings2 size={15}/><span>{t('settings.title')}</span><ChevronRight size={14}/></button>
      </aside>
      <section className="conversation">
        <header className="conversation-head"><div><h2>{t('conversation')}</h2><p>{submittedTask ? t(busy ? 'privateProgress' : 'conversationReady') : t('conversationHint')}</p></div><Sparkles size={19}/></header>
        <div className="message-stream" ref={messageStream} onScroll={handleStreamScroll}>
          {!submittedTask && <div className="welcome"><span><Sparkles size={24}/></span><h3>{t('welcome')}</h3><p>{t('welcomeDetail')}</p></div>}
          {submittedTask && !hasStoredUserEvents && <article className="message user-message" data-user-turn="initial" data-turn-label="1"><div className="message-label">{t('you')}</div><p>{submittedTask}</p></article>}
          {displayEvents.map(({ event, sourceIndex }) => <EventMessage event={event} events={events} onApprovalError={reportApprovalError} onRetry={retryFailedTurn} retryDisabled={busy} key={`${event.turn_id ?? 'session'}-${event.type}-${sourceIndex}`} />)}
          {busy && <div className="thinking"><LoaderCircle size={16}/><span>{t('agentThinking')} · {t('elapsed', { time: formatElapsed(elapsedSeconds) })}{queuedCount > 0 ? ` · ${t('queuedCount', { count: queuedCount })}` : ''}</span></div>}
          {error && <article className="message error-message" role="alert"><CircleAlert size={17}/><div><strong>{t('runFailed')}</strong><p>{error}</p>{retryableTurnId && <button className="retry-button" type="button" disabled={busy} onClick={() => void retryFailedTurn(retryableTurnId)}><Redo2 size={13}/>{t('retryRun')}</button>}</div></article>}
        </div>
        {conversationMarkers.length > 0 && <nav className="conversation-rail" aria-label={t('conversationNavigator')}>{conversationMarkers.map((marker) => <button type="button" className={activeUserTurn === marker.id ? 'active' : ''} style={{ '--marker-top': marker.top } as React.CSSProperties} title={t('jumpToTurn', { count: marker.label })} aria-label={t('jumpToTurn', { count: marker.label })} onClick={() => jumpToUserTurn(marker.id)} key={marker.id}><span/></button>)}</nav>}
        {hasNewProgress && <button className="new-progress" type="button" onClick={jumpToLatest}><ArrowDown size={14}/>{t('newProgress')}</button>}
        <div className="composer-wrap"><div className="composer"><textarea value={task} onChange={(e) => setTask(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) void launch() }} placeholder={t(busy ? 'interruptPlaceholder' : sessionId ? 'followupPlaceholder' : 'taskPlaceholder')}/><div className="composer-actions"><span>{busy ? t('interruptHint') : sessionId ? t('memoryHint') : t('submitHint')}</span><div className="composer-buttons">{busy && executionId && <button className="cancel-button" type="button" onClick={() => void cancelExecution(executionId)}><Square size={13}/>{t('cancelRun')}</button>}<button className="send-button" disabled={!task.trim() || !workspace.trim() || (busy && !sessionId)} onClick={() => void launch()}>{busy ? <Plus size={17}/> : <Play size={16}/>}<span>{busy ? t('queueMessage') : workspaceChanged ? t('startNewWorkspace') : sessionId ? t('continueConversation') : t('start')}</span></button></div></div></div></div>
      </section>
      <aside className="inspector">
        <nav className="tabs"><button className={tab === 'files' ? 'active' : ''} onClick={() => setTab('files')}><FolderTree size={15}/>{t('files')}</button><button className={tab === 'diff' ? 'active' : ''} onClick={() => setTab('diff')}><Code2 size={15}/>{t('diff')}</button><button className={tab === 'terminal' ? 'active' : ''} onClick={() => setTab('terminal')}><TerminalSquare size={15}/>{t('terminal')}</button></nav>
        <div className="inspector-content">
          {tab === 'files' && <>{changedFiles.length > 0 && <div className="changed-summary"><Check size={15}/>{t('changedCount', { count: changedFiles.length })}</div>}<FileWorkbench sessionId={sessionId} entries={workspaceEntries} onFilesChanged={async () => setWorkspaceEntries(sessionId ? await getWorkspaceFiles(sessionId).catch(() => []) : [])}/></>}
          {tab === 'diff' && (diffTree.length ? <div className="diff-layout"><section className="tree-panel change-tree"><div className="inspector-section-title"><GitBranch size={14}/><span>{t('changeTree')}</span><small>{t('fileCount', { count: diffEntries.length })}</small></div><TreeView nodes={diffTree} selected={activeDiff?.path} onSelect={setSelectedDiff}/></section>{activeDiff && <details className="diff-file" open><summary><FileText size={14}/><span>{activeDiff.path}</span><ChevronRight className="diff-collapse-chevron" size={14}/></summary><SideBySideDiff diff={activeDiff.diff}/></details>}</div> : <EmptyInspector text={t('noDiff')}/>)}
          {tab === 'terminal' && (terminalRuns.length ? <div className="terminal-runs">{terminalRuns.map((run, index) => <section className={`terminal-card ${run.ok ? 'passed' : 'failed'}`} key={`${run.command}-${index}`}><header>{run.ok ? <CircleCheck size={15}/> : <CircleX size={15}/>}<div><strong>{run.command || t('commandNumber', { count: index + 1 })}</strong><small>{t(run.ok ? 'commandPassed' : 'commandFailed')} · {t('exitCode', { code: run.exitCode })}</small></div></header><pre>{run.output || t('noCommandOutput')}</pre></section>)}</div> : <EmptyInspector text={t('terminalWaiting')}/>)}
        </div>
      </aside>
    </section>
    {settingsOpen && <div className="settings-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setSettingsOpen(false) }}>
      <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-dialog-title">
        <header className="settings-dialog-head"><div><span className="settings-dialog-icon"><Settings2 size={18}/></span><span><strong id="settings-dialog-title">{t('settings.title')}</strong><small>{t('settings.projectDetail')}</small></span></div><button type="button" onClick={() => setSettingsOpen(false)} aria-label={t('workbench.backToEdit')} title={t('workbench.backToEdit')}><X size={17}/></button></header>
        <SettingsPanel sessionId={sessionId} crossSessionMemory={crossSessionMemory} onCrossSessionMemoryChange={changeCrossSessionMemory} agentWorkflow={agentWorkflow} onAgentWorkflowChange={changeAgentWorkflow} busy={busy}/>
      </section>
    </div>}
  </main>

}

const EventMessage = memo(function EventMessage({ event, events, onApprovalError, onRetry, retryDisabled }: { event: AgentEvent; events: AgentEvent[]; onApprovalError: () => void; onRetry: (turnId: string) => Promise<void>; retryDisabled: boolean }) {
  const { t } = useTranslation()
  const position = Number(events.find((candidate) => candidate.type === 'task_created' && candidate.turn_id === event.turn_id)?.payload.position ?? 0)
  if (event.type === 'task_created' && typeof event.payload.task === 'string') return <article className="message user-message" data-user-turn={event.turn_id ?? `turn-${position}`} data-turn-label={String(event.payload.position ?? position ?? 1)}><div className="message-label">{t('you')} · {t('turnLabel', { count: Number(event.payload.position ?? 1) })}</div><p>{String(event.payload.task)}</p></article>
  if (event.type === 'agent_started' && event.role) {
    const progressKey = event.payload?.repairing ? 'publicProgress.repair' : `publicProgress.${event.role}`
    return <div className="progress-update"><span/><p>{t(progressKey)}</p></div>
  }
  if (event.type === 'tool_finished') {
    const result = event.payload.result as ToolResult
    const tool = String(event.payload.tool ?? '')
    const eventIndex = events.indexOf(event)
    const recovered = result?.ok !== false && events.slice(0, eventIndex).some((candidate) => candidate.type === 'tool_finished' && candidate.turn_id === event.turn_id && candidate.payload.tool === tool && (candidate.payload.result as ToolResult)?.ok === false)
    const state = result?.ok === false ? 'failed' : recovered ? 'recovered' : 'done'
    return <div className={`progress-update ${result?.ok === false ? 'warning' : recovered ? 'recovered' : ''}`}><span/><p>{t(`publicProgress.${tool}.${state}`)}</p></div>
  }
  if (event.type === 'approval_requested') {
    const approvalId = String(event.payload.approval_id ?? '')
    const resolution = events.find((candidate) => candidate.type === 'approval_resolved' && candidate.payload.approval_id === approvalId)
    const allowed = resolution?.payload.allowed === true
    return <article className={`approval-card ${resolution ? allowed ? 'approved' : 'denied' : ''}`}><div className="approval-icon"><TerminalSquare size={18}/></div><div className="approval-content"><strong>{resolution ? t(allowed ? 'permissions.allowed' : 'permissions.deniedResult') : t('permissions.requestTitle')}</strong><p>{t('permissions.requestDetail')}</p><code>{String(event.payload.command ?? '')}</code>{!resolution && <div className="approval-actions"><button type="button" className="deny" onClick={() => void resolveApproval(approvalId, false).catch(onApprovalError)}>{t('permissions.reject')}</button><button type="button" className="allow" onClick={() => void resolveApproval(approvalId, true).catch(onApprovalError)}>{t('permissions.allowOnce')}</button></div>}</div></article>
  }
  if (event.type === 'approval_resolved') return null
  if (event.type === 'task_cancelled') return <article className="message completion cancelled-message"><CircleX size={17}/><div><strong>{t('runCancelled')}</strong><p>{t('runCancelledDetail')}</p>{event.turn_id && <button className="retry-button" type="button" disabled={retryDisabled} onClick={() => void onRetry(event.turn_id!)}><Redo2 size={13}/>{t('retryRun')}</button>}</div></article>
  if (event.type === 'task_failed') { const reason = String(event.payload.reason ?? event.summary); const detail = String(event.payload.content ?? ''); return <article className="message error-message"><CircleAlert size={17}/><div><strong>{t('runFailed')}{position > 0 ? ` · ${t('turnLabel', { count: position })}` : ''}</strong><p>{t(`errors.${reason}`, { defaultValue: reason })}</p>{detail && <Markdown>{detail}</Markdown>}{event.turn_id && <button className="retry-button" type="button" disabled={retryDisabled} onClick={() => void onRetry(event.turn_id!)}><Redo2 size={13}/>{t('retryRun')}</button>}</div></article> }
  if (event.type === 'task_finished') return <article className="message assistant-message final-response"><div className="assistant-avatar"><Bot size={16}/></div><div className="assistant-body"><div className="message-label">{t('appName')}</div><Markdown>{event.summary}</Markdown></div></article>
  return null
})

function TreeView({ nodes, selected, onSelect, depth = 0 }: { nodes: TreeNode[]; selected?: string; onSelect?: (path: string) => void; depth?: number }) {
  return <ul className="file-tree">{nodes.map((node) => <li key={node.path}>{node.kind === 'directory' ? <details open={depth < 2}><summary><ChevronRight className="tree-chevron" size={13}/><Folder size={15}/><span>{node.name}</span><small>{node.children.length}</small></summary><TreeView nodes={node.children} selected={selected} onSelect={onSelect} depth={depth + 1}/></details> : <button className={selected === node.path ? 'selected' : ''} onClick={() => onSelect?.(node.path)} type="button"><FileText size={14}/><span>{node.name}</span></button>}</li>)}</ul>
}

function EmptyInspector({ text }: { text: string }) { return <div className="inspector-empty"><Code2 size={24}/><p>{text}</p></div> }
