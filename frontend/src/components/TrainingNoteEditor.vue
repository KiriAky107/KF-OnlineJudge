<script setup lang="ts">
import { computed, nextTick, ref, watch, type ComponentPublicInstance } from 'vue'
import { prepareNoteBlocks } from '../lib/statementMarkdown'
import StatementMarkdown from './StatementMarkdown.vue'
import UiIcon from './UiIcon.vue'

const props = defineProps<{
  itemId: string
  title: string
  saved: string
  saving: boolean
  disabled: boolean
}>()
const note = defineModel<string>({ required: true })
const emit = defineEmits<{ save: [] }>()
const limit = 1000
type Range = { start: number; end: number }
const active = ref<(Range & { separator: string }) | null>(null)
const editor = ref<HTMLTextAreaElement>()
const toolbar = ref<HTMLElement>()
const append = ref<HTMLButtonElement>()
const editOriginal = ref<HTMLButtonElement>()
function bindEditor(element: Element | ComponentPublicInstance | null) {
  editor.value = element instanceof HTMLTextAreaElement ? element : undefined
}
const source = computed(() =>
  active.value ? note.value.slice(active.value.start, active.value.end) : '',
)
const available = computed(() =>
  Math.max(
    0,
    limit - note.value.length + source.value.length - (active.value?.separator.length ?? 0),
  ),
)
const blocks = computed(() => {
  const range = active.value
  if (!range) return prepareNoteBlocks(note.value)
  return [
    ...prepareNoteBlocks(note.value.slice(0, range.start)),
    { start: range.start, end: range.end, content: null },
    ...prepareNoteBlocks(note.value.slice(range.end), range.end),
  ]
})

const tools = [
  { label: '标题', glyph: 'H', before: '## ', after: '', placeholder: '标题' },
  { label: '粗体', glyph: 'B', before: '**', after: '**', placeholder: '重点' },
  { label: '斜体', glyph: 'I', before: '*', after: '*', placeholder: '说明' },
  { label: '无序列表', glyph: '•≡', before: '- ', after: '', placeholder: '列表项' },
  { label: '有序列表', glyph: '1≡', before: '1. ', after: '', placeholder: '列表项' },
  { label: '引用', glyph: '❞', before: '> ', after: '', placeholder: '引用' },
  { label: '代码块', glyph: '{}', before: '```cpp\n', after: '\n```', placeholder: '代码' },
  { label: 'LaTeX 公式', glyph: 'ƒx', before: '$', after: '$', placeholder: 'x^2' },
  { label: '链接', icon: 'connection', before: '[', after: '](https://)', placeholder: '链接文字' },
  {
    label: '图片链接',
    icon: 'picture',
    before: '![',
    after: '](https://)',
    placeholder: '图片说明',
  },
]
type Format = (typeof tools)[number]

async function begin(range?: Range) {
  if (props.disabled) return
  const end = note.value.length
  const separator = note.value.trim()
    ? note.value.endsWith('\n\n')
      ? ''
      : note.value.endsWith('\n')
        ? '\n'
        : '\n\n'
    : ''
  active.value = range ? { ...range, separator: '' } : { start: end, end, separator }
  await nextTick()
  editor.value?.focus()
  editor.value?.setSelectionRange(source.value.length, source.value.length)
}

function setSource(value: string) {
  const range = active.value
  if (!range || value.length > available.value) return
  const separator = value ? range.separator : ''
  const start = range.start + separator.length
  note.value = note.value.slice(0, range.start) + separator + value + note.value.slice(range.end)
  active.value = { start, end: start + value.length, separator: value ? '' : range.separator }
}

function finish(event?: FocusEvent) {
  if (event?.relatedTarget instanceof Node && toolbar.value?.contains(event.relatedTarget)) return
  active.value = null
}

async function format(tool: Format) {
  if (!active.value) await begin()
  const input = editor.value
  if (!input) return
  const { selectionStart: start, selectionEnd: end } = input
  const inner = source.value.slice(start, end) || tool.placeholder
  const value =
    source.value.slice(0, start) + tool.before + inner + tool.after + source.value.slice(end)
  if (value.length > available.value) return
  setSource(value)
  await nextTick()
  input.focus()
  input.setSelectionRange(start + tool.before.length, start + tool.before.length + inner.length)
}

function editBlock(event: MouseEvent, range: Range) {
  if (event.target instanceof Element && event.target.closest('a, button')) return
  if (!window.getSelection()?.isCollapsed) return
  void begin(range)
}

function closeBlock() {
  finish()
  void nextTick(() => (append.value?.disabled ? editOriginal.value : append.value)?.focus())
}

function save() {
  finish()
  emit('save')
}

watch(
  () => props.itemId,
  () => finish(),
)
</script>

<template>
  <section class="training-note-editor" :aria-label="title + '的笔记'">
    <header class="note-heading">
      <h3>{{ title }}</h3>
      <div ref="toolbar" class="note-toolbar" role="group" aria-label="笔记格式">
        <button
          v-for="tool in tools"
          :key="tool.label"
          type="button"
          :title="tool.label"
          :aria-label="tool.label"
          :disabled="disabled"
          @mousedown.prevent
          @click="format(tool)"
        >
          <UiIcon v-if="tool.icon" :name="tool.icon" />
          <span v-else aria-hidden="true">{{ tool.glyph }}</span>
        </button>
        <button
          ref="editOriginal"
          type="button"
          :disabled="disabled"
          @click="begin({ start: 0, end: note.length })"
        >
          编辑原文
        </button>
      </div>
    </header>
    <div class="note-body">
      <template v-for="block in blocks" :key="block.content ? 'block-' + block.start : 'editing'">
        <div v-if="block.content" class="note-block" @click="editBlock($event, block)">
          <StatementMarkdown :prepared="block.content" highlight-code />
          <button
            v-if="!disabled"
            class="note-edit-block"
            type="button"
            aria-label="编辑此段"
            title="编辑此段"
            @click="begin(block)"
          >
            <UiIcon name="document" />
          </button>
        </div>
        <textarea
          v-else
          :ref="bindEditor"
          class="note-input"
          :value="source"
          :maxlength="available"
          :rows="Math.min(10, Math.max(3, source.split('\n').length + 1))"
          :disabled="disabled"
          aria-label="编辑 Markdown 笔记段落"
          placeholder="输入 Markdown，可直接写公式和代码块"
          @input="setSource(($event.target as HTMLTextAreaElement).value)"
          @blur="finish"
          @keydown.esc.stop="closeBlock"
          @keydown.ctrl.enter.prevent="closeBlock"
        />
      </template>
      <button
        ref="append"
        type="button"
        class="note-add"
        :disabled="disabled || note.length >= limit"
        @click="begin()"
      >
        {{ note.trim() ? '添加一段' : '写下你的思路…' }}
      </button>
    </div>
    <footer class="note-actions">
      <span class="small muted">仅本人可见 · {{ note.length }} / {{ limit }}</span>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="disabled || note === saved"
        @click="save"
      >
        保存笔记
      </el-button>
    </footer>
  </section>
</template>

<style scoped>
.training-note-editor {
  min-width: 0;
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 0.65rem;
}
.note-heading,
.note-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.45rem;
}
.note-heading {
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.4rem;
}
.note-heading h3 {
  margin: 0;
  font-size: 0.95rem;
}
.note-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.12rem;
}
.note-toolbar button,
.note-edit-block,
.note-add {
  border: 0;
  border-radius: 5px;
  padding: 0.32rem 0.4rem;
  background: transparent;
  color: var(--secondary-ink);
  font: inherit;
}
.note-toolbar button {
  min-width: 1.7rem;
  font-size: 0.85rem;
}
.note-toolbar button:not(:disabled):hover,
.note-edit-block:hover,
.note-add:not(:disabled):hover {
  color: var(--accent-ink);
  background: var(--accent-bg);
}
.note-body {
  flex: 1;
  min-height: min(9rem, 30vh);
  overflow: auto;
  overscroll-behavior: contain;
  padding: 0.3rem 0;
}
.note-block {
  position: relative;
  min-width: 0;
  padding-inline: 0.4rem 2rem;
  border-radius: 5px;
}
.note-block:hover,
.note-block:focus-within {
  background: var(--surface-soft);
}
.note-edit-block {
  position: absolute;
  top: 0;
  right: 0;
  display: grid;
  place-items: center;
  opacity: 0;
}
.note-block:hover .note-edit-block,
.note-edit-block:focus-visible {
  opacity: 1;
}
.note-input {
  display: block;
  width: 100%;
  max-height: 32vh;
  padding: 0.55rem;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface-soft);
  color: var(--strong-ink);
  font: 0.9rem/1.6 var(--font-mono, monospace);
  resize: vertical;
}
.note-actions {
  flex: none;
  border-top: 1px solid var(--line);
  padding-top: 0.45rem;
}
@media (hover: none) {
  .note-edit-block {
    opacity: 1;
  }
}
</style>
