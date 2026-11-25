<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, useId, useSlots } from 'vue'
import UiIcon from './UiIcon.vue'

const props = defineProps<{ startWithAssistant?: boolean }>()

const zones = { left: '左侧', main: '主区', bottom: '下方', assistant: '助手侧栏' } as const
type Zone = keyof typeof zones
const tabs = {
  statement: { label: '题面', icon: 'reading', zone: 'left', closable: false },
  submissions: { label: '提交记录', icon: 'submissions', zone: 'left', closable: false },
  notes: { label: '笔记', icon: 'document', zone: 'left', closable: true },
  editor: { label: '代码', icon: 'document', zone: 'main', closable: false },
  result: { label: '提交详情', icon: 'view', zone: 'main', closable: true },
  selftest: { label: '自测', icon: 'monitor', zone: 'bottom', closable: false },
  assistant: { label: '学习助手', icon: 'assistant', zone: 'assistant', closable: true },
} satisfies Record<string, { label: string; icon: string; zone: Zone; closable: boolean }>
type Tab = keyof typeof tabs
const slots = useSlots()
const tabIds = (Object.keys(tabs) as Tab[]).filter((tab) => tab !== 'notes' || slots.notes)
const order = ref([...tabIds])
const zoneIds = Object.keys(zones) as Zone[]
const id = useId()
const targets = (zone: Zone) => id + '-dock-' + zone
const positions = reactive(
  Object.fromEntries(tabIds.map((key) => [key, tabs[key].zone])) as Record<Tab, Zone>,
)
const opened = reactive(new Set<Tab>(['statement', 'submissions', 'editor']))
const visited = reactive(new Set<Tab>(['statement', 'editor']))
const selected = reactive<Record<Zone, Tab | undefined>>({
  left: 'statement',
  main: 'editor',
  bottom: undefined,
  assistant: undefined,
})
const mounted = ref(false)
const dragging = ref<Tab>()
const over = ref<Zone>()
const moving = ref<Tab>()
const root = ref<HTMLElement>()
const leftWidth = ref(44)
const minEditorHeight = 340
const minTestHeight = 210
const splitSize = ref<{ editor: number; test: number }>()
const editorOnTop = computed(() => selected.main === 'editor')
const splitAvailable = computed(() => {
  const top = selected.main
  const bottom = selected.bottom
  return (top === 'editor' && bottom === 'selftest') || (top === 'selftest' && bottom === 'editor')
})
const rightWidth = ref(27)
const visibleTabs = (zone: Zone) =>
  order.value.filter((key) => opened.has(key) && positions[key] === zone)
const visible = (zone: Zone) => visibleTabs(zone).length > 0 || Boolean(dragging.value)
onMounted(() => {
  mounted.value = true
  if (props.startWithAssistant) open('assistant')
})

function open(tab: Tab, reveal = false) {
  opened.add(tab)
  visited.add(tab)
  selected[positions[tab]] = tab
  if (reveal) {
    void nextTick(() =>
      document
        .getElementById(id + '-tab-' + tab)
        ?.scrollIntoView({ block: 'nearest', inline: 'nearest' }),
    )
  }
}
function selectFallback(zone: Zone) {
  const fallback = visibleTabs(zone)[0]
  selected[zone] = fallback
  if (fallback) visited.add(fallback)
}
function close(tab: Tab) {
  opened.delete(tab)
  const zone = positions[tab]
  if (selected[zone] === tab) selectFallback(zone)
  if (moving.value === tab) moving.value = undefined
}
function toggle(tab: 'assistant' | 'selftest' | 'notes') {
  if (opened.has(tab) && selected[positions[tab]] === tab) close(tab)
  else open(tab, true)
}
function move(tab: Tab, zone: Zone, before?: Tab) {
  const previous = positions[tab]
  positions[tab] = zone
  if (before && before !== tab) {
    const ordered = order.value.filter((key) => key !== tab)
    ordered.splice(ordered.indexOf(before), 0, tab)
    order.value = ordered
  }
  if (previous !== zone && selected[previous] === tab) selectFallback(previous)
  open(tab)
  moving.value = undefined
  focusTab(tab)
}
function focusTab(tab: Tab) {
  void nextTick(() => document.getElementById(id + '-tab-' + tab)?.focus())
}
function navigateTabs(event: KeyboardEvent, zone: Zone) {
  const choices = visibleTabs(zone)
  const current = choices.indexOf(selected[zone]!)
  const direction = event.key === 'ArrowLeft' ? -1 : 1
  const index =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? choices.length - 1
        : (current + direction + choices.length) % choices.length
  const tab = choices[index]
  if (tab) {
    open(tab)
    focusTab(tab)
  }
}
function startDrag(event: DragEvent, tab: Tab) {
  if (!event.dataTransfer) return
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('text/plain', tab)
  dragging.value = tab
}
function allowDrop(event: DragEvent, zone: Zone) {
  if (!dragging.value) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  over.value = zone
}
function stopDrag() {
  dragging.value = undefined
  over.value = undefined
}
function drop(event: DragEvent, zone: Zone, before?: Tab) {
  if (!dragging.value) return
  event.preventDefault()
  event.stopPropagation()
  move(dragging.value, zone, before)
  stopDrag()
}

const limit = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value))
const resizeLeft = (value: number) => {
  leftWidth.value = limit(value, 24, visible('assistant') ? 72 - rightWidth.value : 68)
}
const resizeRight = (value: number) => {
  rightWidth.value = limit(value, 22, 34)
}
function currentSplitSize() {
  const editor = root.value?.querySelector('[data-dock-tab="editor"]')
  const test = root.value?.querySelector('[data-dock-tab="selftest"]')
  if (!editor || !test) return undefined
  return {
    editor: editor.getBoundingClientRect().height,
    test: test.getBoundingClientRect().height,
  }
}
function resizeSplit(start: NonNullable<typeof splitSize.value>, delta: number) {
  const total = start.editor + start.test
  const editor = Math.round(
    limit(
      start.editor + (editorOnTop.value ? delta : -delta),
      minEditorHeight,
      total - minTestHeight,
    ),
  )
  splitSize.value = { editor, test: Math.round(total - editor) }
}
function resizeSplitByKey(delta: number) {
  const size = currentSplitSize()
  if (size) resizeSplit(size, delta)
}
function captureDrag(event: PointerEvent, move: (event: PointerEvent) => void) {
  const target = event.currentTarget as HTMLElement
  const release = () => target.removeEventListener('pointermove', move)
  event.preventDefault()
  target.focus({ preventScroll: true })
  target.setPointerCapture(event.pointerId)
  target.addEventListener('pointermove', move)
  target.addEventListener('lostpointercapture', release, { once: true })
}
function startSplitResize(event: PointerEvent) {
  const size = currentSplitSize()
  if (!size || event.button !== 0 || !event.isPrimary) return
  captureDrag(event, (next) => resizeSplit(size, next.pageY - event.pageY))
}
function resize(event: PointerEvent, axis: 'left' | 'right') {
  const width = root.value?.getBoundingClientRect().width
  if (!width || event.button !== 0 || !event.isPrimary) return
  const left = axis === 'left'
  const start = left
    ? Math.min(leftWidth.value, visible('assistant') ? 72 - rightWidth.value : 68)
    : rightWidth.value
  const apply = left ? resizeLeft : resizeRight
  captureDrag(event, (next) => {
    const delta = ((next.clientX - event.clientX) / width) * 100
    apply(start + (left ? delta : -delta))
  })
}
defineExpose({ open })
</script>

<template>
  <div class="problem-dock">
    <div class="dock-toolbar">
      <slot name="toolbar" />
      <div class="dock-actions">
        <button
          type="button"
          class="dock-action"
          :aria-pressed="opened.has('selftest')"
          @click="toggle('selftest')"
        >
          <UiIcon name="monitor" /> 自测
        </button>
        <button
          type="button"
          class="dock-action"
          :aria-expanded="opened.has('assistant')"
          aria-label="学习助手"
          title="学习助手"
          @click="toggle('assistant')"
        >
          <UiIcon name="assistant" />
        </button>
        <button
          v-if="slots.notes"
          type="button"
          class="dock-action"
          :aria-expanded="opened.has('notes')"
          aria-label="笔记"
          title="笔记"
          @click="toggle('notes')"
        >
          <UiIcon name="document" />
        </button>
        <el-popover trigger="click" width="165">
          <template #reference>
            <button type="button" class="dock-action" title="打开标签" aria-label="打开标签">
              <UiIcon name="dock" /> 布局
            </button>
          </template>
          <div class="dock-menu">
            <button v-for="tab in tabIds" :key="tab" type="button" @click="open(tab)">
              <UiIcon :name="tabs[tab].icon" /> {{ tabs[tab].label }}
            </button>
          </div>
        </el-popover>
      </div>
    </div>
    <div
      ref="root"
      class="dock-columns"
      :class="{
        'has-left': visible('left'),
        'has-assistant': visible('assistant'),
        'has-center': visible('main') || visible('bottom'),
        'has-both-rows': visible('main') && visible('bottom'),
        'has-workbench-split': splitAvailable,
        'bottom-only': visible('bottom') && !visible('main'),
        'editor-below': selected.bottom === 'editor',
      }"
      :style="{
        '--left-width':
          (visible('assistant') ? Math.min(leftWidth, 72 - rightWidth) : leftWidth) + '%',
        '--right-width': rightWidth + '%',
        '--dock-editor-height': splitAvailable && splitSize ? splitSize.editor + 'px' : undefined,
        '--dock-selftest-height': splitSize ? splitSize.test + 'px' : undefined,
        '--dock-editor-min-height': minEditorHeight + 'px',
        '--dock-test-min-height': minTestHeight + 'px',
        '--dock-bottom-content-height': splitSize
          ? (selected.bottom === 'editor' ? splitSize.editor : splitSize.test) + 'px'
          : undefined,
      }"
    >
      <button
        v-show="visible('left') && (visible('main') || visible('bottom'))"
        type="button"
        class="dock-splitter dock-splitter--left"
        role="separator"
        aria-label="调整左侧区域宽度"
        aria-orientation="vertical"
        aria-valuemin="24"
        :aria-valuemax="visible('assistant') ? 72 - rightWidth : 68"
        :aria-valuenow="
          Math.round(visible('assistant') ? Math.min(leftWidth, 72 - rightWidth) : leftWidth)
        "
        @pointerdown="resize($event, 'left')"
        @keydown.left.prevent="resizeLeft(leftWidth - 3)"
        @keydown.right.prevent="resizeLeft(leftWidth + 3)"
      />
      <button
        v-show="splitAvailable"
        type="button"
        class="dock-splitter dock-splitter--row"
        role="separator"
        aria-label="调整代码编辑器与自测高度"
        aria-orientation="horizontal"
        :aria-controls="id + '-content-editor ' + id + '-content-selftest'"
        title="上下拖动调整代码编辑器与自测高度"
        @pointerdown="startSplitResize"
        @keydown.up.prevent="resizeSplitByKey(-32)"
        @keydown.down.prevent="resizeSplitByKey(32)"
      />
      <button
        v-show="visible('assistant') && (visible('left') || visible('main') || visible('bottom'))"
        type="button"
        class="dock-splitter dock-splitter--right"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整右侧区域宽度"
        aria-valuemin="22"
        aria-valuemax="34"
        :aria-valuenow="Math.round(rightWidth)"
        @pointerdown="resize($event, 'right')"
        @keydown.left.prevent="resizeRight(rightWidth + 3)"
        @keydown.right.prevent="resizeRight(rightWidth - 3)"
      />
      <section
        v-for="zone in zoneIds"
        v-show="visible(zone)"
        :key="zone"
        class="dock-zone"
        :class="['dock-zone--' + zone, { 'drop-target': over === zone }]"
        :aria-label="zones[zone]"
        @dragover="allowDrop($event, zone)"
        @dragenter="allowDrop($event, zone)"
        @drop="drop($event, zone)"
      >
        <div v-if="visibleTabs(zone).length" class="dock-tab-bar">
          <div class="dock-tabs" role="tablist" :aria-label="zones[zone] + '标签'">
            <div
              v-for="tab in visibleTabs(zone)"
              :key="tab"
              class="dock-tab"
              :class="{ 'active-tab': selected[zone] === tab }"
              draggable="true"
              @dragstart="startDrag($event, tab)"
              @dragend="stopDrag"
              @drop="drop($event, zone, tab)"
            >
              <button
                :id="id + '-tab-' + tab"
                type="button"
                role="tab"
                :title="tabs[tab].label"
                :tabindex="selected[zone] === tab ? 0 : -1"
                :aria-selected="selected[zone] === tab"
                :aria-controls="id + '-content-' + tab"
                @click="open(tab)"
                @keydown.left.prevent="navigateTabs($event, zone)"
                @keydown.right.prevent="navigateTabs($event, zone)"
                @keydown.home.prevent="navigateTabs($event, zone)"
                @keydown.end.prevent="navigateTabs($event, zone)"
              >
                <UiIcon :name="tabs[tab].icon" /> {{ tabs[tab].label }}
              </button>
              <button
                v-if="tabs[tab].closable"
                type="button"
                class="tab-close"
                :title="'关闭' + tabs[tab].label"
                :aria-label="'关闭' + tabs[tab].label"
                @click="close(tab)"
              >
                ×
              </button>
            </div>
          </div>
          <button
            v-if="selected[zone] === 'assistant' || selected[zone] === 'selftest'"
            type="button"
            class="dock-icon"
            :title="'折叠' + tabs[selected[zone]!].label"
            :aria-label="'折叠' + tabs[selected[zone]!].label"
            @click="close(selected[zone]!)"
          >
            <UiIcon name="collapse" />
          </button>
          <button
            v-if="selected[zone]"
            type="button"
            class="dock-icon"
            :aria-expanded="moving === selected[zone]"
            :aria-label="'移动' + tabs[selected[zone]!].label"
            :title="'移动' + tabs[selected[zone]!].label"
            @click="moving = moving === selected[zone] ? undefined : selected[zone]"
          >
            <UiIcon name="dock" />
          </button>
        </div>
        <div v-if="moving && moving === selected[zone]" class="move-menu">
          <button
            v-for="target in zoneIds.filter((candidate) => candidate !== zone)"
            :key="target"
            type="button"
            @click="move(moving!, target)"
          >
            移到{{ zones[target] }}
          </button>
        </div>
        <div v-if="!visibleTabs(zone).length" class="drop-empty">拖放到{{ zones[zone] }}</div>
        <div :id="targets(zone)" class="dock-content" />
      </section>
      <template v-if="mounted">
        <Teleport v-for="tab in tabIds" :key="tab" :to="'#' + targets(positions[tab])">
          <div
            v-show="opened.has(tab) && selected[positions[tab]] === tab"
            :id="id + '-content-' + tab"
            class="dock-tab-content"
            :data-dock-tab="tab"
            role="tabpanel"
            :aria-labelledby="id + '-tab-' + tab"
            :tabindex="tab === 'statement' || tab === 'result' || tab === 'notes' ? 0 : undefined"
          >
            <slot v-if="visited.has(tab)" :name="tab" />
          </div>
        </Teleport>
      </template>
    </div>
  </div>
</template>

<style scoped>
.problem-dock {
  display: flex;
  flex-direction: column;
  min-height: 0;
  gap: 8px;
}
.dock-toolbar,
.dock-actions,
.dock-tab-bar,
.dock-tabs,
.dock-tab {
  display: flex;
  align-items: center;
  min-width: 0;
}
.dock-toolbar {
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.dock-actions {
  margin-inline-start: auto;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.dock-action,
.dock-icon,
.dock-tab button,
.dock-menu button,
.move-menu button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.35rem;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--secondary-ink);
  font-size: 0.82rem;
  white-space: nowrap;
  padding: 0.35rem 0.55rem;
}
.dock-action {
  border: 1px solid var(--line);
  background: var(--surface);
}
.dock-action:hover,
.dock-action[aria-pressed='true'],
.dock-action[aria-expanded='true'],
.dock-icon:hover,
.dock-menu button:hover,
.move-menu button:hover,
.dock-tab button:hover {
  background: var(--accent-bg);
  color: var(--accent-ink);
}
.dock-menu {
  display: grid;
  gap: 0.25rem;
}
.dock-menu button {
  justify-content: flex-start;
}
.dock-columns {
  --dock-tab-bar-height: 34px;
  --dock-panel-chrome: calc(var(--dock-tab-bar-height) + 2px);
  --dock-selftest-default-height: clamp(13rem, 30svh, 21rem);
  --dock-height: var(--workspace-height);
  display: grid;
  height: var(--dock-height);
  grid-template-columns:
    var(--left-track, 0px) var(--left-gutter, 0px) var(--center-track, 0px)
    var(--right-gutter, 0px) var(--right-track, 0px);
  grid-template-rows: minmax(0, 1fr) var(--row-gutter, 0px) var(--dock-bottom-track, 0px);
}
.has-left {
  --left-track: minmax(0, 1fr);
}
.has-center {
  --center-track: minmax(0, 1fr);
}
.has-assistant {
  --right-track: minmax(0, 1fr);
}
.has-left.has-center {
  --left-track: minmax(0, var(--left-width));
  --left-gutter: 10px;
}
.has-assistant:is(.has-left, .has-center) {
  --right-track: minmax(0, var(--right-width));
  --right-gutter: 10px;
}
.has-both-rows {
  --row-gutter: 10px;
  --dock-height: max(
    var(--workspace-height),
    calc(
      var(--dock-editor-min-height) + var(--dock-test-min-height) + 2 * var(--dock-panel-chrome) +
        var(--row-gutter)
    )
  );
  --dock-top-min-height: var(--dock-editor-min-height);
  --dock-bottom-min-height: var(--dock-test-min-height);
  --dock-bottom-default: var(--dock-selftest-default-height);
  --dock-bottom-track: clamp(
    calc(var(--dock-bottom-min-height) + var(--dock-panel-chrome)),
    calc(var(--dock-bottom-content-height, var(--dock-bottom-default)) + var(--dock-panel-chrome)),
    calc(
      var(--dock-height) - var(--row-gutter) - var(--dock-top-min-height) - var(--dock-panel-chrome)
    )
  );
}
.has-both-rows.editor-below {
  --dock-top-min-height: var(--dock-test-min-height);
  --dock-bottom-min-height: var(--dock-editor-min-height);
  --dock-bottom-default: var(--dock-editor-min-height);
}
.dock-zone {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
}
.dock-zone--left {
  grid-area: 1 / 1 / 4 / 2;
  align-self: stretch;
}
.dock-zone--main {
  grid-area: 1 / 3 / 2 / 4;
}
.dock-zone--bottom {
  grid-area: 3 / 3 / 4 / 4;
}
.bottom-only .dock-zone--bottom {
  grid-row: 1;
}
.dock-zone--assistant {
  grid-area: 1 / 5 / 4 / 6;
}
.dock-tab-bar {
  gap: 3px;
  flex-shrink: 0;
  min-height: var(--dock-tab-bar-height);
  padding: 1px 5px;
  border-bottom: 1px solid var(--line);
  border-radius: 10px 10px 0 0;
  background: var(--surface-soft);
}
.dock-tabs {
  flex: 1;
  gap: 3px;
  overflow: auto;
}
.dock-tab {
  border-radius: 6px;
  flex-shrink: 0;
}
.active-tab {
  background: var(--accent-bg);
}
.active-tab button {
  color: var(--accent-ink);
}
.dock-tab .tab-close {
  padding-inline: 0.25rem;
  margin-inline-end: 0.15rem;
  font-size: 0.95rem;
}
.dock-content {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template: minmax(0, 1fr) / minmax(0, 1fr);
}
.dock-tab-content {
  min-width: 0;
  min-height: 0;
  overflow: auto;
}
.dock-tab-content:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: -2px;
}
.dock-splitter {
  border: 0;
  border-radius: 6px;
  padding: 0;
  display: grid;
  place-items: center;
  background: transparent;
  color: var(--muted);
  touch-action: none;
  cursor: col-resize;
}
.dock-splitter::after {
  content: '';
  width: 3px;
  height: 24px;
  border-radius: 3px;
  background: currentColor;
}
.dock-splitter:hover,
.dock-splitter:focus-visible {
  background: var(--accent-bg);
}
.dock-splitter--left {
  grid-area: 1 / 2 / 4 / 3;
  align-self: stretch;
}
.dock-splitter--right {
  grid-area: 1 / 4 / 4 / 5;
  align-self: stretch;
}
.dock-splitter--row {
  grid-area: 2 / 3 / 3 / 4;
  min-height: 10px;
  width: 100%;
  background: var(--surface-soft);
  cursor: row-resize;
}
.dock-splitter--row::after {
  width: 25px;
  height: 3px;
}
.move-menu {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  padding: 3px 6px;
  background: var(--surface-raised);
}
.drop-empty {
  margin: auto;
  padding: 0.8rem;
  color: var(--muted);
  text-align: center;
}
.drop-empty + .dock-content {
  flex: 0;
}
.drop-target {
  outline: 2px solid var(--blue);
  outline-offset: -2px;
}
@media (max-width: 1199px) {
  .has-assistant {
    --right-track: 0px;
    --right-gutter: 0px;
  }
  .dock-columns.has-assistant {
    height: auto;
    grid-template-rows:
      minmax(0, calc(var(--dock-height) - var(--row-gutter, 0px) - var(--dock-bottom-track, 0px)))
      var(--row-gutter, 0px) var(--dock-bottom-track, 0px) auto;
  }
  .dock-zone--assistant {
    grid-area: 4 / 1 / 5 / 4;
    margin-top: 14px;
    --assistant-min-height: 26rem;
  }
  .dock-splitter--right {
    display: none;
  }
}
@media (max-width: 880px) {
  .dock-columns {
    height: auto;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 10px;
  }
  .dock-content {
    grid-template-rows: auto;
  }
  .dock-tab-content:is(
    [data-dock-tab='statement'],
    [data-dock-tab='result'],
    [data-dock-tab='notes']
  ) {
    max-height: clamp(22rem, 70svh, 42rem);
  }
  .dock-tab-content[data-dock-tab='selftest'] {
    height: var(--dock-selftest-height, 18rem);
  }
  .dock-splitter--left,
  .dock-splitter--right {
    display: none;
  }
  .dock-zone--main {
    order: 1;
  }
  .dock-splitter--row {
    order: 2;
  }
  .dock-zone--bottom {
    order: 3;
  }
  .dock-zone--assistant {
    order: 4;
    margin-top: 0;
  }
  .has-workbench-split {
    gap: 8px;
  }
  .dock-zone:has(.drop-empty) {
    min-height: 5rem;
  }
}
</style>
