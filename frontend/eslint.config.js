import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import vue from 'eslint-plugin-vue'
import prettier from 'eslint-config-prettier/flat'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'src/api/generated/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
    // TypeScript checks identifiers and DOM globals in Vue's script setup.
    rules: { 'vue/multi-word-component-names': 'off', 'no-undef': 'off' },
  },
  {
    files: ['vite.config.ts'],
    languageOptions: { globals: { process: 'readonly' } },
  },
  prettier,
)
