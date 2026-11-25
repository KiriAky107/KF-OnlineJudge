<script setup lang="ts">
import { ref, watch } from 'vue'
import StatementMarkdown from './StatementMarkdown.vue'

const props = defineProps<{
  itemId: string
  title: string
  saved: string
  saving: boolean
  disabled: boolean
}>()
const note = defineModel<string>({ required: true })
defineEmits<{ save: [] }>()
const preview = ref(false)
watch(
  () => props.itemId,
  () => (preview.value = false),
)
</script>

<template>
  <section class="training-note-editor" :aria-label="title + '的笔记'">
    <header class="note-heading">
      <h3>{{ title }}</h3>
      <el-button-group aria-label="笔记视图">
        <el-button
          :type="preview ? 'default' : 'primary'"
          :aria-pressed="!preview"
          @click="preview = false"
        >
          编辑
        </el-button>
        <el-button
          :type="preview ? 'primary' : 'default'"
          :aria-pressed="preview"
          @click="preview = true"
        >
          预览
        </el-button>
      </el-button-group>
    </header>
    <div v-if="preview" class="note-preview">
      <StatementMarkdown v-if="note.trim()" :source="note" :links="false" highlight-code />
      <p v-else class="muted">暂无笔记</p>
    </div>
    <el-input
      v-else
      v-model="note"
      type="textarea"
      :rows="10"
      maxlength="1000"
      show-word-limit
      aria-label="笔记内容"
      placeholder="记录解法、容易出错的边界或下一次想验证的思路"
      :disabled="disabled"
    />
    <footer class="note-actions">
      <span class="small muted">仅本人可见 · 支持 Markdown 和 LaTeX</span>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="disabled || note === saved"
        @click="$emit('save')"
      >
        保存笔记
      </el-button>
    </footer>
  </section>
</template>

<style scoped>
.training-note-editor {
  min-width: 0;
  padding: 0.85rem;
  display: grid;
  gap: 0.75rem;
}
.note-heading,
.note-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.6rem;
}
.note-heading h3 {
  margin: 0;
}
.note-preview {
  min-height: 13rem;
  border: 1px solid var(--line);
  border-radius: var(--el-border-radius-base);
  background: var(--surface-soft);
  padding: 0.5rem 0.8rem;
}
.training-note-editor :deep(textarea) {
  line-height: 1.65;
}
</style>
