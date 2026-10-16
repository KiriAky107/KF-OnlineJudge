import type { components } from '../api/generated/schema'
import type { Language } from './useCodeDraft'

export type WireLanguage = components['schemas']['SubmissionSummary']['language']
type ProblemCapability = Pick<
  components['schemas']['ProblemDetail'],
  'languages' | 'language_configurations' | 'run_limits'
>
const wire: Record<Language, WireLanguage | null> = {
  c: 'c17',
  cpp: 'cpp20',
  python: 'python313',
  java: 'java21',
  rust: null,
}

export function languageCapability(problem: ProblemCapability | null, language: Language) {
  const id = wire[language]
  if (!problem || !id || !problem.languages.includes(id)) return null
  const configuration = problem.language_configurations.find((c) => c.language === id)
  if (configuration) {
    const validEntry =
      id === 'python313'
        ? configuration.preparation_kind === 'syntax_check' &&
          configuration.source_name === 'main.py' &&
          configuration.entrypoint === 'main.py'
        : id === 'java21'
          ? configuration.preparation_kind === 'compile' &&
            configuration.source_name === 'Main.java' &&
            configuration.entrypoint === 'Main'
          : configuration.preparation_kind === 'compile'
    if (!validEntry || !configuration.runtime_version) return null
    return { wire: id, configuration, runLimits: configuration.run_limits }
  }
  // Only legacy C/C++ versions expose one shared budget. A managed language
  // without its server-provided configuration must not inherit that budget.
  if (
    (id === 'c17' || id === 'cpp20') &&
    problem.language_configurations.length === 0 &&
    problem.run_limits
  )
    return { wire: id, configuration: null, runLimits: problem.run_limits }
  return null
}

export function preparationName(language?: WireLanguage) {
  return language === 'python313' ? '语法检查' : '编译'
}

export function preparingStatus(language?: WireLanguage) {
  return language === 'python313' ? '正在检查语法' : '正在编译'
}
