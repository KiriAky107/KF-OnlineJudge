<script setup lang="ts">
import { computed } from 'vue'
import {
  House,
  Grid,
  Collection,
  Trophy,
  Tickets,
  Histogram,
  ChatDotRound,
  UserFilled,
  InfoFilled,
  Setting,
  ArrowRight,
  Refresh,
  Search,
  Monitor,
  Bell,
  ArrowDown,
  Reading,
  Document,
  Clock,
  Connection,
  FolderOpened,
  Moon,
  Sunny,
  ChatLineRound,
  FullScreen,
  Close,
  Operation,
  Position,
  DArrowRight,
  View,
} from '@element-plus/icons-vue'
const props = defineProps<{ name: string }>()
const icons = {
  home: House,
  problems: Grid,
  trainings: Collection,
  contests: Trophy,
  submissions: Tickets,
  rankings: Histogram,
  discussions: ChatDotRound,
  teams: UserFilled,
  about: InfoFilled,
  settings: Setting,
  arrow: ArrowRight,
  refresh: Refresh,
  search: Search,
  monitor: Monitor,
  bell: Bell,
  down: ArrowDown,
  reading: Reading,
  document: Document,
  clock: Clock,
  connection: Connection,
  folder: FolderOpened,
  moon: Moon,
  sun: Sunny,
  chat: ChatLineRound,
  expand: FullScreen,
  close: Close,
  dock: Operation,
  send: Position,
  collapse: DArrowRight,
  view: View,
}
const icon = computed(() => icons[props.name as keyof typeof icons] ?? Document)
</script>
<template>
  <el-icon aria-hidden="true">
    <svg v-if="name === 'assistant'" viewBox="0 0 24 24" fill="none">
      <path
        d="M7 4H4v16h3m10-16h3v16h-3"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <path
        d="M12 6.5l1.6 3.9 3.9 1.6-3.9 1.6-1.6 3.9-1.6-3.9-3.9-1.6 3.9-1.6z"
        fill="currentColor"
      />
    </svg>
    <svg v-else-if="name === 'assistant-face'" viewBox="0 0 24 24" fill="none">
      <path
        d="M7 4H4v16h3m10-16h3v16h-3m-7-5h4"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <circle cx="9" cy="10" r="1" fill="currentColor" />
      <circle cx="15" cy="10" r="1" fill="currentColor" />
    </svg>
    <component :is="icon" v-else />
  </el-icon>
</template>
