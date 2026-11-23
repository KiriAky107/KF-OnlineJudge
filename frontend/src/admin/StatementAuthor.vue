<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import StatementMarkdown from '../components/StatementMarkdown.vue'

const limit = 256 * 1024
const rasterTypes = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const encoder = new TextEncoder()
defineProps<{ publishing?: boolean }>()
const source = defineModel<string>({ default: '' })
const imageUrl = ref('')
const imageAlt = ref('')
const textarea = ref<HTMLTextAreaElement>()
const error = ref('')
const reading = ref(false)
const size = computed(() => encoder.encode(source.value).byteLength)

async function insertImage(url: string, description: string) {
  const field = textarea.value
  const start = field?.selectionStart ?? source.value.length
  const end = field?.selectionEnd ?? start
  const alt = description
    .trim()
    .replace(/\s+/g, ' ')
    .replace(/[\\[\]]/g, '\\$&')
  const syntax = '\n![' + alt + '](<' + url + '>)\n'
  const next = source.value.slice(0, start) + syntax + source.value.slice(end)
  if (encoder.encode(next).byteLength > limit) {
    throw new Error('含图片的题面最多 256 KiB。请压缩图片或使用图片网址。')
  }
  source.value = next
  imageAlt.value = ''
  imageUrl.value = ''
  await nextTick()
  textarea.value?.focus()
  textarea.value?.setSelectionRange(start + syntax.length, start + syntax.length)
}

async function insertUrl() {
  error.value = ''
  let url: URL
  try {
    url = new URL(imageUrl.value.trim())
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
      throw new Error()
    }
  } catch {
    error.value = '请输入有效的 HTTP 或 HTTPS 图片网址。'
    return
  }
  try {
    await insertImage(url.href.replaceAll('<', '%3C').replaceAll('>', '%3E'), imageAlt.value)
  } catch (cause) {
    error.value = (cause as Error).message
  }
}

function asDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.addEventListener('load', () => resolve(String(reader.result)), { once: true })
    reader.addEventListener('error', () => reject(new Error('无法读取图片。')), { once: true })
    reader.readAsDataURL(file)
  })
}

async function insertFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || reading.value) return
  error.value = ''
  if (!rasterTypes.includes(file.type)) {
    error.value = '请选择 PNG、JPEG、GIF 或 WebP 图片。'
    return
  }
  if (Math.ceil(file.size / 3) * 4 >= limit) {
    error.value = '图片超出题面容量。请压缩图片或使用图片网址。'
    return
  }
  reading.value = true
  try {
    const url = await asDataUrl(file)
    const image = new Image()
    image.src = url
    try {
      await image.decode()
    } catch {
      throw new Error('图片格式无效或文件已损坏。')
    }
    await insertImage(url, imageAlt.value || file.name)
  } catch (cause) {
    error.value = (cause as Error).message
  } finally {
    reading.value = false
  }
}

function download() {
  if (!source.value.trim() || size.value > limit) return
  const url = URL.createObjectURL(new Blob([source.value], { type: 'text/markdown;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = 'statement.md'
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <details class="statement-author" :open="publishing || undefined">
    <summary>编辑题面和插入图片</summary>
    <div class="author-body">
      <p v-if="!publishing" class="admin-muted">编写并预览题面，下载后放入题目导入包。</p>
      <label for="statement-source">Markdown 题面</label>
      <textarea id="statement-source" ref="textarea" v-model="source" rows="9" />
      <div class="author-fields">
        <label>图片说明<input v-model="imageAlt" placeholder="例如：样例流程图" /> </label>
        <label
          >图片网址<input
            v-model="imageUrl"
            inputmode="url"
            placeholder="https://"
            @keydown.enter.prevent="insertUrl"
          />
        </label>
        <button type="button" :disabled="!imageUrl.trim() || reading" @click="insertUrl">
          插入网址图片
        </button>
        <label
          >插入本地图片<input
            type="file"
            :accept="rasterTypes.join(',')"
            :disabled="reading"
            @change="insertFile"
          />
        </label>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <div class="author-actions">
        <span class="admin-muted">{{ (size / 1024).toFixed(1) }} / 256 KiB</span>
        <button
          v-if="!publishing"
          class="admin-primary"
          type="button"
          :disabled="!source.trim() || size > limit"
          @click="download"
        >
          下载 statement.md
        </button>
      </div>
      <section v-if="source.trim()" class="author-preview" aria-label="题面预览">
        <h3>题面预览</h3>
        <StatementMarkdown :source="source" />
      </section>
    </div>
  </details>
</template>

<style scoped>
.statement-author {
  margin-block: 0.85rem;
  padding: 0.7rem;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
}
.statement-author > summary {
  cursor: pointer;
  font-weight: 600;
}
.author-body {
  display: grid;
  gap: 0.6rem;
  margin-block-start: 0.75rem;
}
.author-body textarea {
  width: 100%;
  min-height: 9rem;
  resize: vertical;
  padding: 0.6rem;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-blank);
  color: var(--el-text-color-primary);
  font-family: monospace;
}
.author-fields,
.author-actions {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 0.6rem 0.8rem;
}
.author-fields label {
  display: grid;
  min-width: 0;
  gap: 0.2rem;
}
.author-fields input {
  max-width: 100%;
}
.author-preview {
  padding-top: 0.6rem;
  min-width: 0;
  border-top: 1px solid var(--el-border-color-light);
}
</style>
