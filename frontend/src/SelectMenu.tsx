import { Check, ChevronDown } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'

export type SelectMenuOption<T extends string> = {
  value: T
  label: string
  description?: string
}

type SelectMenuProps<T extends string> = {
  label: string
  value: T
  options: SelectMenuOption<T>[]
  disabled?: boolean
  onChange: (value: T) => void
}

export function SelectMenu<T extends string>({ label, value, options, disabled = false, onChange }: SelectMenuProps<T>) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const listboxId = useId()
  const selected = options.find((option) => option.value === value) ?? options[0]

  useEffect(() => {
    if (!open) return
    const closeFromOutside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeFromKeyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', closeFromOutside)
    document.addEventListener('keydown', closeFromKeyboard)
    return () => {
      document.removeEventListener('pointerdown', closeFromOutside)
      document.removeEventListener('keydown', closeFromKeyboard)
    }
  }, [open])

  useEffect(() => {
    if (disabled) setOpen(false)
  }, [disabled])

  const moveSelection = (direction: 1 | -1) => {
    const current = Math.max(0, options.findIndex((option) => option.value === value))
    const next = (current + direction + options.length) % options.length
    onChange(options[next].value)
    setOpen(true)
  }

  return <div className={`select-menu ${open ? 'open' : ''}`} ref={root}>
    <button
      type="button"
      className="select-trigger"
      aria-label={`${label}：${selected.label}`}
      aria-haspopup="listbox"
      aria-controls={listboxId}
      aria-expanded={open}
      disabled={disabled}
      onClick={() => setOpen((current) => !current)}
      onKeyDown={(event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault()
          moveSelection(event.key === 'ArrowDown' ? 1 : -1)
        }
      }}
    >
      <span>{selected.label}</span>
      <ChevronDown size={15}/>
    </button>
    {open && <div className="select-popover" id={listboxId} role="listbox" aria-label={label}>
      {options.map((option) => <button
        type="button"
        role="option"
        aria-selected={option.value === value}
        className={option.value === value ? 'selected' : ''}
        onClick={() => {
          onChange(option.value)
          setOpen(false)
        }}
        key={option.value}
      >
        <span className="select-option-copy"><strong>{option.label}</strong>{option.description && <small>{option.description}</small>}</span>
        <span className="select-option-check">{option.value === value && <Check size={14}/>}</span>
      </button>)}
    </div>}
  </div>
}
