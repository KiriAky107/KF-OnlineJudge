import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { components } from '../src/api/generated/schema'
import {
  languageCapability,
  preparationName,
  preparingStatus,
} from '../src/composables/judgingLanguage.ts'

const limits: components['schemas']['RunLimits'] = {
  cpu_time_us: 1_000_000,
  wall_time_us: 3_000_000,
  memory_bytes: 67_108_864,
  file_bytes: 16_777_216,
  open_files: 64,
  processes: 8,
  stack_bytes: 8_388_608,
  stderr_bytes: 1_048_576,
  stdout_bytes: 1_048_576,
  workdir_bytes: 67_108_864,
}
const python: components['schemas']['PublicLanguageConfiguration'] = {
  language: 'python313',
  runtime_version: '3.13.16',
  source_name: 'main.py',
  entrypoint: 'main.py',
  preparation_kind: 'syntax_check',
  compile_limits: limits,
  run_limits: { ...limits, cpu_time_us: 2_000_000 },
  job_wall_time_us: 12_000_000,
}
const java: components['schemas']['PublicLanguageConfiguration'] = {
  language: 'java21',
  runtime_version: '21.0.12.1+1-LTS',
  source_name: 'Main.java',
  entrypoint: 'Main',
  preparation_kind: 'compile',
  compile_limits: limits,
  run_limits: { ...limits, memory_bytes: 134_217_728 },
  job_wall_time_us: 12_000_000,
}
type Problem = Parameters<typeof languageCapability>[0]

test('server capability controls managed submission and the selected version/budget; legacy C stays supported', () => {
  const legacy: Problem = {
    languages: ['c17', 'cpp20'],
    language_configurations: [],
    run_limits: limits,
  }
  assert.equal(languageCapability(legacy, 'c')?.wire, 'c17')
  assert.equal(languageCapability(legacy, 'cpp')?.runLimits, limits)
  assert.equal(languageCapability(legacy, 'python'), null)
  assert.equal(languageCapability(legacy, 'java'), null)
  assert.equal(languageCapability(legacy, 'rust'), null)
  const current: Problem = {
    languages: ['python313', 'java21'],
    language_configurations: [python, java],
    run_limits: null,
  }
  assert.equal(
    languageCapability(current, 'python')?.configuration?.runtime_version,
    python.runtime_version,
  )
  assert.equal(languageCapability(current, 'python')?.runLimits.cpu_time_us, 2_000_000)
  assert.equal(languageCapability(current, 'java')?.runLimits.memory_bytes, 134_217_728)
  assert.equal(languageCapability(current, 'java')?.configuration?.entrypoint, 'Main')
  assert.equal(languageCapability(current, 'c'), null)
  assert.equal(languageCapability(null, 'python'), null)
})

test('managed language with absent or invalid fixed entry capability cannot inherit legacy budget', () => {
  const missing: Problem = {
    languages: ['python313', 'java21'],
    language_configurations: [],
    run_limits: limits,
  }
  assert.equal(languageCapability(missing, 'python'), null)
  assert.equal(languageCapability(missing, 'java'), null)
  for (const invalid of [
    { ...python, preparation_kind: 'compile' as const },
    { ...python, source_name: 'main.pyc' },
    { ...java, entrypoint: 'Other' },
    { ...java, source_name: 'package/Main.java' },
    { ...java, runtime_version: '' },
  ]) {
    const problem: Problem = {
      languages: [invalid.language],
      language_configurations: [invalid],
      run_limits: limits,
    }
    assert.equal(
      languageCapability(problem, invalid.language === 'python313' ? 'python' : 'java'),
      null,
    )
  }
})

test('Python preparation is displayed as syntax checking while Java and old C/C++ compile', () => {
  assert.equal(preparingStatus('python313'), '正在检查语法')
  assert.equal(preparationName('python313'), '语法检查')
  for (const language of ['c17', 'cpp20', 'java21'] as const) {
    assert.equal(preparingStatus(language), '正在编译')
    assert.equal(preparationName(language), '编译')
  }
})
