import { computed, onScopeDispose, ref, watch } from 'vue'
export type Language = 'c' | 'cpp' | 'python' | 'java' | 'rust'
export const languageLabels: Record<Language, string> = {
  c: 'C17',
  cpp: 'C++20',
  python: 'Python 3.13',
  java: 'Java 21',
  rust: 'Rust',
}
export const codeTemplates: Record<Language, string> = {
  c: '#include <stdio.h>\n\nint main(void) {\n    return 0;\n}\n',
  cpp: '#include <iostream>\nusing namespace std;\n\nint main() {\n    // 在这里编写你的解法\n    return 0;\n}\n',
  python: '# 在这里编写你的解法\n\n',
  java: 'public class Main {\n    public static void main(String[] args) throws Exception {\n        // 在这里编写你的解法\n    }\n}\n',
  rust: 'use std::io::{self, Read};\n\nfn main() {\n    let mut input = String::new();\n    io::stdin().read_to_string(&mut input).unwrap();\n    // 在这里编写你的解法\n}\n',
}
export function useCodeDraft(problemId: string) {
  const key = 'kf-oj:code-draft:' + problemId
  const language = ref<Language>('cpp')
  const drafts = ref({ ...codeTemplates })
  const state = ref<'initial' | 'saved' | 'changed' | 'unavailable'>('initial')
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const raw = localStorage.getItem(key)
    if (raw) {
      const saved: unknown = JSON.parse(raw)
      if (
        saved &&
        typeof saved === 'object' &&
        'version' in saved &&
        saved.version === 1 &&
        'drafts' in saved &&
        saved.drafts &&
        typeof saved.drafts === 'object'
      ) {
        const storedDrafts = saved.drafts as Record<string, unknown>
        for (const lang of Object.keys(codeTemplates) as Language[]) {
          const value = storedDrafts[lang]
          if (typeof value === 'string') drafts.value[lang] = value
        }
        if ('language' in saved && Object.hasOwn(codeTemplates, String(saved.language)))
          language.value = saved.language as Language
        state.value = 'saved'
      }
    }
  } catch {
    state.value = 'unavailable'
  }
  const code = computed({
    get: () => drafts.value[language.value],
    set: (value) => {
      drafts.value[language.value] = value
    },
  })
  function save() {
    clearTimeout(timer)
    try {
      localStorage.setItem(
        key,
        JSON.stringify({ version: 1, language: language.value, drafts: drafts.value }),
      )
      state.value = 'saved'
    } catch {
      state.value = 'unavailable'
    }
  }
  watch(
    [drafts, language],
    () => {
      state.value = 'changed'
      clearTimeout(timer)
      timer = setTimeout(save, 400)
    },
    // Mark pending changes before a lifecycle event can run in the same tick.
    { deep: true, flush: 'sync' },
  )
  function flushPendingDraft() {
    if (state.value === 'changed') save()
  }
  function onVisibilityChange() {
    if (document.visibilityState === 'hidden') flushPendingDraft()
  }
  window.addEventListener('pagehide', flushPendingDraft)
  document.addEventListener('visibilitychange', onVisibilityChange)
  onScopeDispose(() => {
    flushPendingDraft()
    clearTimeout(timer)
    window.removeEventListener('pagehide', flushPendingDraft)
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })
  return { language, code, state, save }
}
