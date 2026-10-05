<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { TagCanvas } from '@misskey-dev/tagcanvas-es'

const props = defineProps<{ tags: readonly string[] }>()
const emit = defineEmits<{ select: [tag: string] }>()
const stage = ref<HTMLDivElement>()
const canvas = ref<HTMLCanvasElement>()
const source = ref<HTMLDivElement>()
const width = ref(300)
const ready = ref(false)
const rotationEnabled = ref(true)
const interaction = ref<'follow' | 'drag'>('follow')
const visible = ref(false)
const hidden = ref(false)
const colors = ['#409eff', '#638ac5', '#588db8', '#7496b8']
const fontSize = computed(() => {
  const textArea = props.tags.reduce((sum, tag) => sum + [...tag].length + 1.8, 0)
  const radius = Math.max(35, (Math.min(width.value, 300) - 68) / 2)
  return Math.max(
    12,
    Math.min(19, Math.sqrt((4 * Math.PI * radius ** 2 * 0.23) / Math.max(1, textArea * 1.35))),
  )
})
function weight(tag: string, index: number) {
  const emphasis = ['动态规划', '图论'].includes(tag) ? 1.2 : index % 3 === 0 ? 1.08 : 1
  return emphasis * Math.min(1, Math.sqrt(4 / Math.max(1, [...tag].length)))
}

let cloud: TagCanvas | undefined
let resizeObserver: ResizeObserver | undefined
let intersectionObserver: IntersectionObserver | undefined
let motionQuery: MediaQueryList | undefined

function syncAnimation() {
  if (!visible.value || hidden.value) cloud?.pause()
  else cloud?.resume()
}
function rebuild() {
  cloud?.destroy()
  cloud = undefined
  ready.value = false
  if (!canvas.value || !stage.value || !source.value || !props.tags.length) return
  const ratio = Math.min(2, window.devicePixelRatio || 1)
  canvas.value.width = Math.round(width.value * ratio)
  canvas.value.height = 300 * ratio
  try {
    cloud = new TagCanvas(canvas.value, {
      tagContainer: source.value,
      hideTags: false,
      shape: 'sphere',
      textColor: null,
      textFont: getComputedStyle(stage.value).fontFamily,
      textHeight: fontSize.value * ratio,
      weight: props.tags.length > 1,
      weightFrom: 'data-weight',
      weightMode: 'size',
      weightSizeMin: (fontSize.value - 1) * ratio,
      weightSizeMax: (fontSize.value + 2) * ratio,
      bgColor: '#f0f7ff',
      bgOutline: '#cfe2f7',
      bgOutlineThickness: ratio,
      bgRadius: 14 * ratio,
      padding: 5 * ratio,
      outlineColor: '#409eff',
      outlineThickness: ratio,
      outlineRadius: 14 * ratio,
      outlineOffset: 1 * ratio,
      outlineMethod: 'outline',
      minBrightness: 0.45,
      maxBrightness: 1,
      depth: 0.55,
      zoom: 0.98,
      maxSpeed: rotationEnabled.value ? 0.02 : 0,
      initial: rotationEnabled.value ? [0.16, -0.08] : [0, 0],
      decel: 0.94,
      dragControl: interaction.value === 'drag',
      dragThreshold: 4,
      freezeActive: true,
      freezeDecel: true,
      wheelZoom: false,
      noTagsMessage: false,
      activeAudio: false,
      txtScale: 2,
    })
    ready.value = true
    syncAnimation()
  } catch {
    // Keep the ordinary links available when canvas rendering is unavailable.
    ready.value = false
  }
}
function updateVisibility() {
  hidden.value = document.hidden
}
function updateMotionPreference() {
  if (motionQuery?.matches) rotationEnabled.value = false
}
watch([() => props.tags, width, rotationEnabled, interaction], rebuild, {
  deep: true,
  flush: 'post',
})
watch([visible, hidden], syncAnimation)
onMounted(() => {
  if (!stage.value) return
  width.value = stage.value.getBoundingClientRect().width
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  rotationEnabled.value = !motionQuery.matches
  if (window.matchMedia('(pointer: coarse)').matches) interaction.value = 'drag'
  motionQuery.addEventListener('change', updateMotionPreference)
  updateVisibility()
  document.addEventListener('visibilitychange', updateVisibility)
  resizeObserver = new ResizeObserver(([entry]) => {
    if (entry) width.value = entry.contentRect.width
  })
  resizeObserver.observe(stage.value)
  intersectionObserver = new IntersectionObserver(([entry]) => {
    visible.value = !!entry?.isIntersecting
  })
  intersectionObserver.observe(stage.value)
  rebuild()
})
onBeforeUnmount(() => {
  cloud?.destroy()
  resizeObserver?.disconnect()
  intersectionObserver?.disconnect()
  motionQuery?.removeEventListener('change', updateMotionPreference)
  document.removeEventListener('visibilitychange', updateVisibility)
})
</script>

<template>
  <div class="tag-sphere">
    <div ref="stage" class="tag-sphere-stage" role="group" aria-label="球形题目标签">
      <canvas
        v-show="ready"
        ref="canvas"
        class="sphere-canvas"
        :class="{ draggable: interaction === 'drag' }"
        role="img"
        :aria-label="`球形标签云，${interaction === 'drag' ? '拖动旋转' : '跟随鼠标旋转'}，点击标签筛选题目。键盘可选择下方标签链接。`"
      />
      <div
        ref="source"
        class="sphere-source"
        :class="{ 'source-hidden': ready }"
        aria-label="题目标签"
      >
        <a
          v-for="(tag, index) in tags"
          :key="tag"
          :href="'/problems?tag=' + encodeURIComponent(tag)"
          :style="{ color: colors[index % colors.length] }"
          :data-weight="weight(tag, index)"
          @click.prevent="emit('select', tag)"
          >{{ tag }}</a
        >
      </div>
      <p v-if="!tags.length" class="sphere-empty">暂无题目标签</p>
    </div>
    <div v-if="ready" class="sphere-controls">
      <span>点击标签查看题目</span>
      <div class="sphere-control-buttons">
        <button
          :aria-pressed="interaction === 'drag'"
          aria-label="切换标签球交互方式"
          @click="interaction = interaction === 'follow' ? 'drag' : 'follow'"
        >
          {{ interaction === 'follow' ? '鼠标跟随' : '拖动旋转' }}
        </button>
        <button :aria-pressed="rotationEnabled" @click="rotationEnabled = !rotationEnabled">
          {{ rotationEnabled ? '暂停旋转' : '开始旋转' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tag-sphere-stage {
  position: relative;
  width: 100%;
  height: 300px;
}
.sphere-canvas {
  width: 100%;
  height: 300px;
  display: block;
  touch-action: none;
  user-select: none;
}
.sphere-canvas.draggable {
  cursor: grab;
}
.sphere-canvas.draggable:active {
  cursor: grabbing;
}
.sphere-source {
  display: flex;
  align-content: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 10px;
  height: 100%;
  padding: 20px;
}
.sphere-source a {
  padding: 5px 10px;
  background: #f0f7ff;
  border: 1px solid #dceaf9;
  border-radius: 999px;
  font-size: 14px;
}
.sphere-source.source-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
}
.sphere-source.source-hidden:focus-within {
  inset: 0;
  width: 100%;
  height: 100%;
  padding: 20px;
  clip-path: none;
  overflow: auto;
  background: #fff;
}
.sphere-source a:focus-visible {
  outline: 2px solid #409eff;
  outline-offset: 2px;
}
.sphere-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #99aabd;
  font-size: 14px;
}
.sphere-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  color: #99aabd;
  font-size: 11px;
  padding-top: 4px;
}
.sphere-control-buttons {
  display: flex;
  gap: 6px;
}
.sphere-controls button {
  color: #629edb;
  background: #f5faff;
  border: 1px solid #e6f0fb;
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 11px;
  cursor: pointer;
}
.sphere-controls button:focus-visible {
  outline: 2px solid #90c2fb;
  outline-offset: 2px;
}
</style>
