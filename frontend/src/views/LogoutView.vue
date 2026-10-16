<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useSession } from '../composables/useSession'
import { errorText } from '../api/transport'
import EmptyState from '../components/EmptyState.vue'
const done = ref(false)
const busy = ref(false)
const error = ref('')
async function logout() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await useSession().logout()
    done.value = true
  } catch (cause) {
    error.value = errorText(cause)
  } finally {
    busy.value = false
  }
}
onMounted(logout)
</script>
<template>
  <div class="panel" style="max-width: 650px; margin: 50px auto">
    <EmptyState
      :title="done ? '已退出登录' : '正在退出登录'"
      description="当前浏览器的题目草稿已保留。"
    >
      <p v-if="error" role="alert">{{ error }}</p>
      <el-button v-if="error" :loading="busy" @click="logout">重试退出</el-button>
      <RouterLink to="/login"><el-button type="primary">去登录</el-button></RouterLink>
    </EmptyState>
  </div>
</template>
