import '@testing-library/jest-dom/vitest'
import '../i18n'

class EventSourceStub {
  onmessage: ((event: MessageEvent) => void) | null = null
  close() {}
}

Object.defineProperty(globalThis, 'EventSource', { value: EventSourceStub, writable: true })
Object.defineProperty(HTMLElement.prototype, 'scrollTo', { value: () => undefined, writable: true })
