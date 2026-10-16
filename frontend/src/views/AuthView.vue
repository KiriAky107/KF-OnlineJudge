<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type FormInstance, type FormRules } from 'element-plus'
import { useSession } from '../composables/useSession'
import { ApiError, errorText } from '../api/transport'
const route = useRoute()
const router = useRouter()
const registering = computed(() => route.path === '/register')
const form = ref<FormInstance>()
const model = reactive({ username: '', password: '' })
const busy = ref(false)
const error = ref(route.query.unavailable ? '身份服务暂时无法连接，请稍后重试。' : '')
const rules: FormRules<typeof model> = {
  username: [
    {
      required: true,
      pattern: /^[a-z0-9_]{3,24}$/,
      message: '使用 3 至 24 位小写字母、数字或下划线',
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, min: 12, max: 128, message: '请输入 12 至 128 位密码', trigger: 'blur' },
  ],
}
async function submit() {
  if (busy.value || registering.value) return
  busy.value = true
  try {
    if (!(await form.value?.validate().catch(() => false))) return
    error.value = ''
    await useSession().login(model.username, model.password)
    model.password = ''
    const next =
      typeof route.query.next === 'string' &&
      route.query.next.startsWith('/') &&
      !route.query.next.startsWith('//')
        ? route.query.next
        : '/problems'
    await router.replace(next)
  } catch (cause) {
    error.value =
      cause instanceof ApiError && cause.status === 401 ? '用户名或密码不正确。' : errorText(cause)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="auth-shell">
    <aside class="auth-art">
      <RouterLink to="/" class="brand"
        ><span class="brand-mark">KF.</span><span class="brand-name">OnlineJudge</span></RouterLink
      >
      <h1>写下思路，<br />开始这一题。</h1>
      <p>练习、竞赛、讨论。<br />每一步，都算数。</p>
    </aside>
    <section class="auth-form">
      <p class="eyebrow">WELCOME TO KF ONLINEJUDGE</p>
      <h2>{{ registering ? '创建你的账号' : '欢迎回来' }}</h2>
      <p>{{ registering ? '准备好开始新的练习了吗？' : '继续你的算法练习之旅。' }}</p>
      <p v-if="registering" class="notice-strip">
        暂不开放自助注册。请使用管理员通过本地账号管理命令创建的账号。
      </p>
      <el-form
        v-else
        ref="form"
        :model="model"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username"
          ><el-input v-model="model.username" autocomplete="username" aria-label="用户名"
        /></el-form-item>
        <el-form-item label="密码" prop="password"
          ><el-input
            v-model="model.password"
            type="password"
            autocomplete="current-password"
            aria-label="密码"
        /></el-form-item>
        <p v-if="error" role="alert">{{ error }}</p>
        <el-button type="primary" native-type="submit" :loading="busy">登录</el-button>
      </el-form>
      <p class="form-help" style="margin-top: 14px">
        草稿只保存在当前浏览器，重新登录后可继续编辑。
      </p>
      <div class="auth-links">
        <RouterLink to="/">← 返回首页</RouterLink
        ><RouterLink :to="registering ? '/login' : '/register'">{{
          registering ? '已有账号？去登录' : '还没有账号？去注册'
        }}</RouterLink>
      </div>
    </section>
  </div>
</template>
