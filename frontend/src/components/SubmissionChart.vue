<script setup lang="ts">
import { computed, ref } from 'vue'
const showAccepted = ref(true)
const showTotal = ref(true)
const points = [
  { day: '09-28', total: 44, accepted: 28 },
  { day: '09-29', total: 68, accepted: 43 },
  { day: '09-30', total: 52, accepted: 32 },
  { day: '10-01', total: 91, accepted: 57 },
  { day: '10-02', total: 77, accepted: 46 },
  { day: '10-03', total: 116, accepted: 74 },
  { day: '10-04', total: 96, accepted: 62 },
]
const active = ref<number | null>(null)
const x = (index: number) => 55 + index * 111
const y = (value: number) => 192 - (value / 120) * 155
const line = (field: 'total' | 'accepted') =>
  points.map((point, index) => x(index) + ',' + y(point[field])).join(' ')
const selected = computed(() => (active.value === null ? undefined : points[active.value]))
</script>
<template>
  <div class="submission-chart">
    <div class="chart-legend">
      <button :aria-pressed="showAccepted" @click="showAccepted = !showAccepted">
        <span class="chart-dot accepted" />通过</button
      ><button :aria-pressed="showTotal" @click="showTotal = !showTotal">
        <span class="chart-dot total" />总提交</button
      ><span class="muted">单位：次 · 示例统计</span>
    </div>
    <svg
      viewBox="0 0 770 240"
      role="img"
      aria-label="近七天示例提交趋势：总提交44、68、52、91、77、116、96次"
    >
      <g v-for="tick in [0, 30, 60, 90, 120]" :key="tick">
        <line x1="55" x2="721" :y1="y(tick)" :y2="y(tick)" stroke="#edf1f6" />
        <text x="42" :y="y(tick) + 4" text-anchor="end">{{ tick }}</text>
      </g>
      <g v-for="(point, index) in points" :key="point.day">
        <text :x="x(index)" y="219" text-anchor="middle">{{ point.day }}</text>
      </g>
      <polyline
        v-if="showTotal"
        :points="line('total')"
        fill="none"
        stroke="#76b5ee"
        stroke-width="2"
        stroke-linejoin="round"
      />
      <polyline
        v-if="showAccepted"
        :points="line('accepted')"
        fill="none"
        stroke="#8ccdab"
        stroke-width="2"
        stroke-linejoin="round"
      />
      <g v-for="(point, index) in points" :key="index">
        <circle
          v-if="showTotal"
          :cx="x(index)"
          :cy="y(point.total)"
          r="3"
          fill="white"
          stroke="#76b5ee"
        />
        <circle
          v-if="showAccepted"
          :cx="x(index)"
          :cy="y(point.accepted)"
          r="3"
          fill="white"
          stroke="#8ccdab"
        />
        <rect
          :x="x(index) - 20"
          y="25"
          width="40"
          height="174"
          fill="transparent"
          @mouseenter="active = index"
          @mouseleave="active = null"
        >
          <title>{{ point.day }}：总提交 {{ point.total }}，通过 {{ point.accepted }}</title>
        </rect>
      </g>
    </svg>
    <p class="chart-detail" aria-live="polite">
      {{
        selected
          ? selected.day + ' · 总提交 ' + selected.total + ' 次 · 通过 ' + selected.accepted + ' 次'
          : '近 7 天的提交与通过情况'
      }}
    </p>
  </div>
</template>
<style scoped>
.chart-legend {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 20px;
  flex-wrap: wrap;
  font-size: 11px;
  margin-bottom: 10px;
}
.chart-legend button {
  border: 0;
  background: none;
  color: #8797ac;
  font-size: 12px;
  display: flex;
  gap: 7px;
  align-items: center;
  padding: 3px;
}
.chart-legend button[aria-pressed='false'] {
  opacity: 0.4;
}
.chart-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  display: inline-block;
}
.accepted {
  background: #8ccdab;
}
.total {
  background: #76b5ee;
}
.submission-chart svg {
  display: block;
  width: 100%;
  height: auto;
}
.submission-chart text {
  font:
    11px Inter,
    'Microsoft YaHei',
    sans-serif;
  fill: #9aa8b9;
}
.chart-detail {
  text-align: center;
  color: #9cabbd;
  font-size: 11px;
  min-height: 20px;
}
</style>
