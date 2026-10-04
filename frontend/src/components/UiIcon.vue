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
}
const icon = computed(() => icons[props.name as keyof typeof icons] ?? Document)
</script>
<template>
  <el-icon aria-hidden="true"><component :is="icon" /></el-icon>
</template>
