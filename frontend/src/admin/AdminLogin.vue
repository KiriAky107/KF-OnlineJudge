<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSession } from '../composables/useSession'
import { maintenanceError } from '../api/adminProblems'
import { hasMaintenance } from './intents'
const session = useSession(),
  router = useRouter()
const username = ref(''),
  password = ref(''),
  busy = ref(false),
  error = ref('')
async function login() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await session.login(username.value, password.value)
    await router.replace(hasMaintenance(session.user.value) ? '/problems' : '/forbidden')
  } catch (e) {
    error.value = maintenanceError(e)
  } finally {
    password.value = ''
    busy.value = false
  }
}
</script>
<template>
  <section class="admin-real-panel">
    <h1>管理员登录</h1>
    <p>使用已有账号与同源安全会话。维护权限由服务端授予。</p>
    <form @submit.prevent="login">
      <label
        >用户名<input v-model="username" autocomplete="username" required maxlength="24"
      /></label>
      <label
        >密码<input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          maxlength="256"
      /></label>
      <button :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>
