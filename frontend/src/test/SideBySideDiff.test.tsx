import { describe, expect, it } from 'vitest'
import { parseUnifiedDiff } from '../SideBySideDiff'

describe('side-by-side diff parser', () => {
  it('aligns replacement, context, and insertion rows with line numbers', () => {
    const rows = parseUnifiedDiff(`--- a/note.txt
+++ b/note.txt
@@ -2,2 +2,3 @@
-old value
+new value
 context
+extra`)
    expect(rows[0]).toMatchObject({ kind:'hunk' })
    expect(rows[1]).toMatchObject({ kind:'change', leftNumber:2, rightNumber:2, leftText:'old value', rightText:'new value' })
    expect(rows[2]).toMatchObject({ kind:'context', leftNumber:3, rightNumber:3, leftText:'context', rightText:'context' })
    expect(rows[3]).toMatchObject({ kind:'change', rightNumber:4, rightText:'extra' })
  })
})
