import { computed, onScopeDispose, ref, watch } from 'vue'
export type Language = 'cpp' | 'python' | 'rust'
export const languageLabels: Record<Language, string> = {
  cpp: 'C++',
  python: 'Python',
  rust: 'Rust',
}
export const codeTemplates: Record<Language, string> = {
  cpp: '#include <iostream>\nusing namespace std;\n\nint main() {\n    // 在这里编写你的解法\n    return 0;\n}\n',
  python: '# 在这里编写你的解法\n\n',
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
        for (const lang of ['cpp', 'python', 'rust'] as const) {
          const value = storedDrafts[lang]
          if (typeof value === 'string') drafts.value[lang] = value
        }
        if ('language' in saved && ['cpp', 'python', 'rust'].includes(String(saved.language)))
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
    { deep: true },
  )
  onScopeDispose(() => {
    if (state.value === 'changed') save()
    clearTimeout(timer)
  })
  return { language, code, state, save }
}
