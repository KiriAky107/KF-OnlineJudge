<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, ref, useId } from 'vue'
import { useRoute } from 'vue-router'
import { assistantPage } from '../lib/assistantNavigation'
import UiIcon from './UiIcon.vue'

const LearningAgent = defineAsyncComponent(() => import('../views/LearningAgentView.vue'))
const route = useRoute()
const dialogId = useId()
const open = ref(false)
const launcher = ref<HTMLButtonElement>()
const panel = ref<HTMLElement>()
const fullPage = computed(() => assistantPage(route.query))

async function toggle() {
  open.value = !open.value
  await nextTick()
  if (open.value) panel.value?.focus()
  else launcher.value?.focus()
}
</script>

<template>
  <Teleport to="body">
    <div class="assistant-launcher">
      <section
        v-if="open"
        :id="dialogId"
        ref="panel"
        class="assistant-panel"
        aria-label="学习助手快捷对话"
        role="dialog"
        tabindex="-1"
        @keydown.esc.stop.prevent="toggle"
      >
        <header class="assistant-panel-bar">
          <strong><UiIcon name="assistant" /> 学习助手</strong>
          <div class="assistant-panel-actions">
            <RouterLink :to="fullPage" aria-label="在完整页面继续对话" title="在完整页面继续对话">
              <UiIcon name="expand" />
            </RouterLink>
            <button type="button" aria-label="收起学习助手" title="收起学习助手" @click="toggle">
              <UiIcon name="close" />
            </button>
          </div>
        </header>
        <div class="assistant-panel-body">
          <LearningAgent embedded floating />
        </div>
      </section>
      <button
        ref="launcher"
        class="assistant-launcher-button"
        type="button"
        :aria-label="open ? '收起学习助手' : '打开学习助手'"
        :aria-expanded="open"
        :aria-controls="open ? dialogId : undefined"
        :title="open ? '收起学习助手' : '打开学习助手'"
        @click="toggle"
      >
        <UiIcon name="assistant" />
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.assistant-launcher {
  position: fixed;
  z-index: 90;
  bottom: max(1rem, env(safe-area-inset-bottom));
  left: max(1rem, env(safe-area-inset-left));
  display: grid;
  justify-items: start;
  gap: 0.55rem;
}
.assistant-launcher-button {
  display: grid;
  place-items: center;
  width: 3rem;
  height: 3rem;
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--surface);
  color: var(--accent-ink);
  box-shadow: 0 4px 18px #0002;
  font-size: 1.5rem;
}
.assistant-panel {
  display: flex;
  flex-direction: column;
  width: min(29rem, calc(100vw - 2rem));
  height: min(39rem, calc(100dvh - 5.5rem));
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  color: var(--strong-ink);
  box-shadow: 0 12px 36px #0003;
  overflow: hidden;
}
.assistant-panel-bar,
.assistant-panel-actions {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.assistant-panel-bar {
  justify-content: space-between;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--line);
}
.assistant-panel-actions :is(a, button) {
  display: grid;
  place-items: center;
  min-width: 2rem;
  min-height: 2rem;
  padding: 0;
  border: 0;
  border-radius: 0.4rem;
  background: transparent;
  color: var(--secondary-ink);
}
.assistant-panel-actions :is(a, button):hover,
.assistant-launcher-button:hover {
  background: var(--accent-bg);
  color: var(--accent-ink);
}
.assistant-panel-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}
@media (max-width: 480px) {
  .assistant-launcher {
    bottom: max(0.65rem, env(safe-area-inset-bottom));
    left: max(0.65rem, env(safe-area-inset-left));
  }
  .assistant-panel {
    width: min(29rem, calc(100vw - 1.3rem));
  }
}
</style>
