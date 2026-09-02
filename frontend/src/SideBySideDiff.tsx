import { Columns2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export type DiffRow = {
  kind: 'context' | 'change' | 'hunk' | 'meta'
  leftNumber?: number
  rightNumber?: number
  leftText?: string
  rightText?: string
  label?: string
}

export function parseUnifiedDiff(diff: string): DiffRow[] {
  const lines = diff.split('\n')
  const rows: DiffRow[] = []
  let leftNumber = 0
  let rightNumber = 0
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (line.startsWith('--- ') || line.startsWith('+++ ')) { index += 1; continue }
    if (line.startsWith('@@')) {
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line)
      if (match) { leftNumber = Number(match[1]); rightNumber = Number(match[2]) }
      rows.push({ kind:'hunk', label:line })
      index += 1
      continue
    }
    if (line.startsWith('-') && !line.startsWith('---')) {
      const removed: string[] = []
      const added: string[] = []
      while (index < lines.length && lines[index].startsWith('-') && !lines[index].startsWith('---')) { removed.push(lines[index].slice(1)); index += 1 }
      while (index < lines.length && lines[index].startsWith('+') && !lines[index].startsWith('+++')) { added.push(lines[index].slice(1)); index += 1 }
      const count = Math.max(removed.length, added.length)
      for (let offset = 0; offset < count; offset += 1) {
        rows.push({ kind:'change', leftNumber:removed[offset] === undefined ? undefined : leftNumber++, rightNumber:added[offset] === undefined ? undefined : rightNumber++, leftText:removed[offset], rightText:added[offset] })
      }
      continue
    }
    if (line.startsWith('+') && !line.startsWith('+++')) {
      rows.push({ kind:'change', rightNumber:rightNumber++, rightText:line.slice(1) })
      index += 1
      continue
    }
    if (line.startsWith(' ')) {
      rows.push({ kind:'context', leftNumber:leftNumber++, rightNumber:rightNumber++, leftText:line.slice(1), rightText:line.slice(1) })
      index += 1
      continue
    }
    if (line && !line.startsWith('\\ No newline')) rows.push({ kind:'meta', label:line })
    index += 1
  }
  return rows
}

export function SideBySideDiff({ diff }: { diff: string }) {
  const { t } = useTranslation()
  const rows = parseUnifiedDiff(diff)
  const removed = rows.filter((row) => row.kind === 'change' && row.leftText !== undefined).length
  const added = rows.filter((row) => row.kind === 'change' && row.rightText !== undefined).length
  if (!rows.length) return <p className="diff-empty">{t('emptyDiff')}</p>
  return <div className="split-diff-shell">
    <header className="split-diff-summary"><span><Columns2 size={13}/>{t('diffReview.sideBySide')}</span><small className="diff-removed">−{removed}</small><small className="diff-added">+{added}</small></header>
    <div className="split-diff-scroll">
      <div className="split-diff" role="table" aria-label={t('diffReview.sideBySide')}>
        <div className="split-diff-head" role="row"><span role="columnheader">{t('diffReview.before')}</span><span role="columnheader">{t('diffReview.after')}</span></div>
        {rows.map((row, index) => row.kind === 'hunk' || row.kind === 'meta'
          ? <div className={`diff-wide-row ${row.kind}`} role="row" key={`${row.kind}-${index}`}>{row.label}</div>
          : <div className={`split-diff-row ${row.kind}`} role="row" key={`${row.leftNumber ?? 'x'}-${row.rightNumber ?? 'x'}-${index}`}>
              <div className={`diff-cell ${row.leftText === undefined ? 'empty' : row.kind === 'change' ? 'removed' : ''}`} role="cell"><span className="diff-line-number">{row.leftNumber ?? ''}</span><code>{row.leftText ?? ''}</code></div>
              <div className={`diff-cell ${row.rightText === undefined ? 'empty' : row.kind === 'change' ? 'added' : ''}`} role="cell"><span className="diff-line-number">{row.rightNumber ?? ''}</span><code>{row.rightText ?? ''}</code></div>
            </div>)}
      </div>
    </div>
  </div>
}
