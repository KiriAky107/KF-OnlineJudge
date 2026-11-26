<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  useId,
} from 'vue'
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
const position = ref<{ left: number; top: number }>()
const dragging = ref(false)
let pointer: { id: number; left: number; top: number } | undefined
const keyboardDeltas: Record<string, readonly [number, number]> = {
  ArrowLeft: [-16, 0],
  ArrowRight: [16, 0],
  ArrowUp: [0, -16],
  ArrowDown: [0, 16],
}
const panelStyle = computed(() =>
  position.value ? { left: `${position.value.left}px`, top: `${position.value.top}px` } : undefined,
)

function move(left: number, top: number) {
  if (!panel.value) return
  const viewport = window.visualViewport
  const bounds = panel.value.getBoundingClientRect()
  const x = (viewport?.offsetLeft ?? 0) + 8
  const y = (viewport?.offsetTop ?? 0) + 8
  const right = Math.max(x, x + (viewport?.width ?? window.innerWidth) - bounds.width - 16)
  const bottom = Math.max(y, y + (viewport?.height ?? window.innerHeight) - bounds.height - 16)
  position.value = {
    left: Math.min(Math.max(x, left), right),
    top: Math.min(Math.max(y, top), bottom),
  }
}

function clampPosition() {
  if (position.value) move(position.value.left, position.value.top)
}

function startDrag(event: PointerEvent) {
  if (!panel.value || !event.isPrimary || event.button !== 0) return
  const { left, top } = panel.value.getBoundingClientRect()
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture(event.pointerId)
  pointer = { id: event.pointerId, left: event.clientX - left, top: event.clientY - top }
  dragging.value = true
  move(left, top)
}

function drag(event: PointerEvent) {
  if (pointer?.id === event.pointerId)
    move(event.clientX - pointer.left, event.clientY - pointer.top)
}

function stopDrag(event: PointerEvent) {
  if (pointer?.id !== event.pointerId) return
  pointer = undefined
  dragging.value = false
}

function moveWithKey(event: KeyboardEvent) {
  if (!panel.value) return
  const delta = keyboardDeltas[event.key]
  if (!delta) return
  event.preventDefault()
  const { left, top } = panel.value.getBoundingClientRect()
  move(left + delta[0], top + delta[1])
}

onMounted(() => {
  window.addEventListener('resize', clampPosition)
  window.visualViewport?.addEventListener('resize', clampPosition)
  window.visualViewport?.addEventListener('scroll', clampPosition)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', clampPosition)
  window.visualViewport?.removeEventListener('resize', clampPosition)
  window.visualViewport?.removeEventListener('scroll', clampPosition)
})

async function toggle() {
  open.value = !open.value
  await nextTick()
  if (open.value) {
    clampPosition()
    panel.value?.focus()
  } else {
    pointer = undefined
    dragging.value = false
    launcher.value?.focus()
  }
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
        :class="{ 'assistant-panel--positioned': position, dragging }"
        :style="panelStyle"
        aria-label="学习助手快捷对话"
        role="dialog"
        tabindex="-1"
        @keydown.esc.stop.prevent="toggle"
      >
        <header class="assistant-panel-bar">
          <button
            type="button"
            class="assistant-panel-grip"
            aria-label="移动学习助手，可使用方向键"
            title="拖动以移动"
            @pointerdown="startDrag"
            @pointermove="drag"
            @pointerup="stopDrag"
            @pointercancel="stopDrag"
            @lostpointercapture="stopDrag"
            @keydown="moveWithKey"
          >
            <UiIcon name="assistant" /> 学习助手
          </button>
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
  --assistant-inset: 1rem;
  position: fixed;
  z-index: 102;
  bottom: max(var(--assistant-inset), env(safe-area-inset-bottom));
  left: max(var(--assistant-inset), env(safe-area-inset-left));
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
  position: fixed;
  bottom: calc(max(var(--assistant-inset), env(safe-area-inset-bottom)) + 3.55rem);
  left: max(var(--assistant-inset), env(safe-area-inset-left));
  display: flex;
  flex-direction: column;
  width: min(29rem, calc(100% - max(16px, 2 * var(--assistant-inset))));
  height: min(39rem, calc(100dvh - 5.5rem));
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  color: var(--strong-ink);
  box-shadow: 0 12px 36px #0003;
  overflow: hidden;
}
.assistant-panel--positioned {
  bottom: auto;
  z-index: 1;
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
.assistant-panel-grip {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-weight: 650;
  cursor: grab;
  touch-action: none;
  user-select: none;
}
.assistant-panel.dragging .assistant-panel-grip {
  cursor: grabbing;
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
    --assistant-inset: 0.65rem;
  }
}
</style>
