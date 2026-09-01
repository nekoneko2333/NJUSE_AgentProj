import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Check, ChevronRight, CircleAlert, FileText, Folder, FolderTree, RotateCcw, Save, Search, X } from 'lucide-react'
import { getFileContent, listCheckpoints, restoreCheckpoint, saveFileContent, searchWorkspace, type Checkpoint, type FileContent, type SearchMatch, type WorkspaceEntry } from './api'

type TreeNode = { name: string; path: string; kind: 'file' | 'directory'; children: TreeNode[] }
type OpenFile = FileContent & { draft: string }

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
      }
      level = node.children
    })
  }
  const sort = (nodes: TreeNode[]): TreeNode[] => nodes.sort((a, b) => Number(a.kind === 'file') - Number(b.kind === 'file') || a.name.localeCompare(b.name)).map((node) => ({ ...node, children: sort(node.children) }))
  return sort(roots)
}

function Tree({ nodes, selected, onSelect, depth = 0 }: { nodes: TreeNode[]; selected: string; onSelect: (path: string) => void; depth?: number }) {
  return <ul className="file-tree">{nodes.map((node) => <li key={node.path}>{node.kind === 'directory'
    ? <details open={depth < 2}><summary><ChevronRight className="tree-chevron" size={13}/><Folder size={15}/><span>{node.name}</span><small>{node.children.length}</small></summary><Tree nodes={node.children} selected={selected} onSelect={onSelect} depth={depth + 1}/></details>
    : <button className={selected === node.path ? 'selected' : ''} type="button" onClick={() => onSelect(node.path)}><FileText size={14}/><span>{node.name}</span></button>}</li>)}</ul>
}

function previewDiff(file: OpenFile): string {
  const before = file.content.split('\n')
  const after = file.draft.split('\n')
  const lines = [`--- ${file.path}`, `+++ ${file.path}`]
  const length = Math.max(before.length, after.length)
  for (let index = 0; index < length; index += 1) {
    if (before[index] === after[index]) lines.push(`  ${before[index] ?? ''}`)
    else {
      if (before[index] !== undefined) lines.push(`- ${before[index]}`)
      if (after[index] !== undefined) lines.push(`+ ${after[index]}`)
    }
  }
  return lines.join('\n')
}

export function FileWorkbench({ sessionId, entries, onFilesChanged }: { sessionId: string | null; entries: WorkspaceEntry[]; onFilesChanged: () => Promise<void> }) {
  const { t } = useTranslation()
  const [files, setFiles] = useState<OpenFile[]>([])
  const [activePath, setActivePath] = useState('')
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<SearchMatch[]>([])
  const [searching, setSearching] = useState(false)
  const [confirmingSave, setConfirmingSave] = useState(false)
  const [notice, setNotice] = useState('')
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const tree = useMemo(() => buildTree(entries), [entries])
  const active = files.find((file) => file.path === activePath)

  useEffect(() => { setFiles([]); setActivePath(''); setMatches([]); setNotice(''); setConfirmingSave(false) }, [sessionId])
  useEffect(() => { if (sessionId) void listCheckpoints(sessionId).then(setCheckpoints).catch(() => setCheckpoints([])) }, [sessionId, entries])

  const open = async (path: string) => {
    if (!sessionId) return
    setNotice(''); setConfirmingSave(false)
    if (files.some((file) => file.path === path)) { setActivePath(path); return }
    try {
      const file = await getFileContent(sessionId, path)
      setFiles((current) => [...current, { ...file, draft: file.content }])
      setActivePath(path)
    } catch (reason) { setNotice(t(`errors.${reason instanceof Error ? reason.message : String(reason)}`, { defaultValue: String(reason) })) }
  }

  const close = (path: string) => {
    const file = files.find((candidate) => candidate.path === path)
    if (file && file.draft !== file.content && !window.confirm(t('workbench.discardConfirm'))) return
    const remaining = files.filter((candidate) => candidate.path !== path)
    setFiles(remaining)
    if (activePath === path) setActivePath(remaining.at(-1)?.path ?? '')
  }

  const runSearch = async () => {
    if (!sessionId || !query.trim()) return
    setSearching(true); setNotice('')
    try { setMatches((await searchWorkspace(sessionId, query.trim())).matches) }
    catch (reason) { setNotice(t(`errors.${reason instanceof Error ? reason.message : String(reason)}`, { defaultValue: String(reason) })) }
    finally { setSearching(false) }
  }

  const save = async () => {
    if (!sessionId || !active || active.draft === active.content) return
    if (!confirmingSave) { setConfirmingSave(true); return }
    setNotice('')
    try {
      const result = await saveFileContent(sessionId, active.path, active.draft, active.sha256)
      const refreshed = await getFileContent(sessionId, active.path)
      setFiles((current) => current.map((file) => file.path === active.path ? { ...refreshed, draft: refreshed.content } : file))
      setConfirmingSave(false)
      setNotice(t('workbench.saved'))
      await onFilesChanged()
      setCheckpoints(await listCheckpoints(sessionId))
      if (typeof result.diff === 'string') setNotice(t('workbench.savedCheckpoint'))
    } catch (reason) {
      const code = reason instanceof Error ? reason.message : String(reason)
      setNotice(t(`errors.${code}`, { defaultValue: code }))
      setConfirmingSave(false)
    }
  }

  const restore = async (checkpoint: Checkpoint) => {
    if (!sessionId || !window.confirm(t('workbench.restoreConfirm', { label: checkpoint.label }))) return
    setNotice('')
    try {
      await restoreCheckpoint(checkpoint.id)
      setFiles([]); setActivePath('')
      setCheckpoints(await listCheckpoints(sessionId))
      await onFilesChanged()
      setNotice(t('workbench.restored'))
    } catch (reason) { setNotice(t(`errors.${reason instanceof Error ? reason.message : String(reason)}`, { defaultValue: String(reason) })) }
  }

  if (!sessionId) return <div className="inspector-empty"><FolderTree size={24}/><p>{t('noFiles')}</p></div>
  return <div className="workbench">
    <form className="workspace-search" onSubmit={(event) => { event.preventDefault(); void runSearch() }}><Search size={14}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('workbench.searchPlaceholder')}/><button type="submit" disabled={!query.trim() || searching}>{t('workbench.search')}</button></form>
    {notice && <p className="workbench-notice"><CircleAlert size={13}/>{notice}</p>}
    {matches.length > 0 && <section className="search-results"><header>{t('matchCount', { count: matches.length })}</header>{matches.map((match, index) => <button type="button" onClick={() => void open(match.path)} key={`${match.path}-${match.line}-${index}`}><strong>{match.path}:{match.line}</strong><span>{match.text}</span></button>)}</section>}
    <div className="workbench-grid">
      <section className="tree-panel"><div className="inspector-section-title"><FolderTree size={14}/><span>{t('workspaceTree')}</span><small>{t('fileCount', { count: entries.filter((entry) => entry.kind === 'file').length })}</small></div>{tree.length ? <Tree nodes={tree} selected={activePath} onSelect={(path) => void open(path)}/> : <p className="tree-empty">{t('noFiles')}</p>}</section>
      <section className="editor-panel">
        {files.length > 0 && <nav className="file-tabs">{files.map((file) => <button type="button" className={file.path === activePath ? 'active' : ''} onClick={() => { setActivePath(file.path); setConfirmingSave(false) }} key={file.path}><span>{file.path.split('/').at(-1)}{file.draft !== file.content ? ' •' : ''}</span><X size={12} onClick={(event) => { event.stopPropagation(); close(file.path) }}/></button>)}</nav>}
        {active ? <><header className="editor-head"><div><strong>{active.path}</strong><small>{t('workbench.fileBytes', { count: active.bytes })}{active.readonly ? ` · ${t('errors.file_too_large')}` : ''}</small></div><button type="button" disabled={active.readonly || active.draft === active.content} onClick={() => void save()}><Save size={13}/>{t(confirmingSave ? 'workbench.confirmSave' : 'workbench.save')}</button></header>{confirmingSave ? <div className="save-review"><p>{t('workbench.reviewDiff')}</p><pre>{previewDiff(active)}</pre><button type="button" onClick={() => setConfirmingSave(false)}>{t('workbench.backToEdit')}</button></div> : <textarea className="file-editor" readOnly={active.readonly} spellCheck={false} value={active.draft} onChange={(event) => setFiles((current) => current.map((file) => file.path === active.path ? { ...file, draft: event.target.value } : file))}/>}</> : <div className="editor-empty"><FileText size={23}/><p>{t('workbench.selectFile')}</p></div>}
      </section>
    </div>
    {checkpoints.length > 0 && <details className="checkpoint-panel"><summary><RotateCcw size={14}/>{t('workbench.checkpoints')}<small>{checkpoints.length}</small></summary>{checkpoints.map((checkpoint) => <div className="checkpoint-row" key={checkpoint.id}><div><strong>{checkpoint.label}</strong><small>{checkpoint.files.join(', ') || t('workbench.noCapturedFiles')}</small></div><span className={`checkpoint-status ${checkpoint.status}`}>{t(`workbench.status.${checkpoint.status}`)}</span>{checkpoint.status === 'available' && <button type="button" onClick={() => void restore(checkpoint)}><RotateCcw size={12}/>{t('workbench.restore')}</button>}</div>)}</details>}
  </div>
}
