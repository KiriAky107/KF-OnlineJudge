<script setup lang="ts">
import { computed, ref } from 'vue'
import type { components } from '../api/generated/schema'

const props = defineProps<{ activity: components['schemas']['SubmissionActivity'] }>()
const selected = ref('')
// The API supplies calendar days in its declared time zone. UTC is used here only for date arithmetic.
const millisPerDay = 86_400_000
const weeks = computed(() => {
  const start = Date.parse(props.activity.start)
  const end = Date.parse(props.activity.end)
  const monday = start - ((new Date(start).getUTCDay() + 6) % 7) * millisPerDay
  const counts = new Map(props.activity.days.map(({ day, count }) => [day, count]))
  const weeks = []
  for (let first = monday; first <= end; first += 7 * millisPerDay) {
    const days = Array.from({ length: 7 }, (_, offset) => {
      const time = first + offset * millisPerDay
      if (time < start || time > end) return null
      const date = new Date(time)
      const key = date.toISOString().slice(0, 10)
      const count = counts.get(key) ?? 0
      return {
        key,
        month: time === start || date.getUTCDate() === 1 ? date.getUTCMonth() + 1 : null,
        count,
        level: count >= 10 ? 4 : count >= 5 ? 3 : count >= 2 ? 2 : count,
      }
    })
    weeks.push({ days, month: days.find((day) => day?.month)?.month })
  }
  return weeks
})
const submissions = computed(() => props.activity.days.reduce((total, day) => total + day.count, 0))
</script>

<template>
  <div class="activity-summary">
    <strong>{{ submissions.toLocaleString('zh-CN') }} 次提交</strong>
    <span>{{ activity.days.length }} 个活跃日</span>
    <span>{{ activity.start }} 至 {{ activity.end }}</span>
    <span>北京时间</span>
  </div>
  <div class="calendar-scroll" tabindex="0" aria-label="最近 365 天每日提交">
    <div class="calendar">
      <div class="weekdays" aria-hidden="true"><span>一</span><span>三</span><span>五</span></div>
      <div v-for="(week, index) in weeks" :key="index" class="week">
        <span class="month" aria-hidden="true">{{ week.month ? week.month + '月' : '' }}</span>
        <template v-for="(day, offset) in week.days" :key="day?.key ?? offset">
          <button
            v-if="day"
            type="button"
            class="day"
            :data-level="day.level"
            :aria-label="`${day.key} · ${day.count} 次提交`"
            :title="`${day.key} · ${day.count} 次提交`"
            :tabindex="day.count ? 0 : -1"
            @mouseenter="selected = `${day.key} · ${day.count} 次提交`"
            @focus="selected = `${day.key} · ${day.count} 次提交`"
            @click="selected = `${day.key} · ${day.count} 次提交`"
          />
          <span v-else aria-hidden="true" />
        </template>
      </div>
    </div>
  </div>
  <div class="calendar-footer">
    <span>{{ selected || '选择日期查看提交次数' }}</span>
    <div class="legend" aria-label="颜色对应 0、1、2 到 4、5 到 9、10 次及以上">
      少<span
        v-for="level in 5"
        :key="level"
        class="day"
        :data-level="level - 1"
        aria-hidden="true"
      />多
    </div>
  </div>
</template>

<style scoped>
.activity-summary,
.calendar-footer,
.legend {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem 0.8rem;
  font-size: 0.8rem;
  color: var(--secondary-ink);
}
.activity-summary strong {
  color: var(--strong-ink);
}
.calendar-scroll {
  container-type: inline-size;
  overflow-x: auto;
  padding: 0.7rem 2px 0.4rem;
}
.calendar {
  display: flex;
  gap: 4px;
  --cell-width: clamp(11px, calc((100cqw - 15rem) / 54), 26px);
  min-width: max-content;
}
.week,
.weekdays {
  display: grid;
  grid-template-rows: 20px repeat(7, var(--cell-width));
  gap: 4px;
}
.week {
  width: var(--cell-width);
}
.weekdays {
  width: 16px;
  text-align: center;
  color: var(--muted);
  font-size: 0.7rem;
}
.weekdays span:nth-child(1) {
  grid-row: 2;
}
.weekdays span:nth-child(2) {
  grid-row: 4;
}
.weekdays span:nth-child(3) {
  grid-row: 6;
}
.month {
  width: max-content;
  align-self: end;
  font-size: 0.7rem;
  color: var(--secondary-ink);
}
.day {
  width: var(--cell-width, 13px);
  height: var(--cell-width, 13px);
  border: 1px solid color-mix(in srgb, var(--line) 60%, transparent);
  border-radius: 3px;
  padding: 0;
  background: var(--surface-raised);
}
.day[data-level='1'] {
  background: color-mix(in srgb, var(--el-color-success) 30%, var(--surface-raised));
}
.day[data-level='2'] {
  background: color-mix(in srgb, var(--el-color-success) 52%, var(--surface-raised));
}
.day[data-level='3'] {
  background: color-mix(in srgb, var(--el-color-success) 75%, var(--surface-raised));
}
.day[data-level='4'] {
  background: var(--el-color-success);
}
.calendar-footer {
  justify-content: space-between;
  min-height: 1.5rem;
}
.legend {
  gap: 4px;
}
</style>
