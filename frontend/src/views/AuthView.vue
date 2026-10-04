<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
const route = useRoute()
const registering = computed(() => route.path === '/register')
const form = ref<FormInstance>()
const model = reactive({ username: '', email: '', password: '', confirmation: '' })
const rules = computed<FormRules<typeof model>>(() => ({
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    ...(registering.value
      ? [
          {
            pattern: /^[a-zA-Z0-9_]{3,24}$/,
            message: '使用 3 至 24 位字母、数字或下划线',
            trigger: 'blur',
          },
        ]
      : []),
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '请填写有效邮箱', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    ...(registering.value ? [{ min: 8, message: '密码至少包含 8 个字符', trigger: 'blur' }] : []),
  ],
  confirmation: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) =>
        value === model.password ? callback() : callback(new Error('两次输入的密码不一致')),
      trigger: 'blur',
    },
  ],
}))
async function submit() {
  if (await form.value?.validate().catch(() => false))
    ElMessage.info('账号服务尚未接入，暂时无法' + (registering.value ? '注册' : '登录'))
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
      <el-form
        ref="form"
        :model="model"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
        ><el-form-item label="用户名" prop="username"
          ><el-input
            v-model="model.username"
            placeholder="请输入用户名"
            autocomplete="username" /></el-form-item
        ><el-form-item v-if="registering" label="邮箱" prop="email"
          ><el-input
            v-model="model.email"
            placeholder="your@email.com"
            autocomplete="email" /></el-form-item
        ><el-form-item label="密码" prop="password"
          ><el-input
            v-model="model.password"
            type="password"
            show-password
            placeholder="请输入密码"
            :autocomplete="registering ? 'new-password' : 'current-password'" /></el-form-item
        ><el-form-item v-if="registering" label="确认密码" prop="confirmation"
          ><el-input
            v-model="model.confirmation"
            type="password"
            show-password
            placeholder="再次输入密码"
            autocomplete="new-password" /></el-form-item
        ><el-button type="primary" native-type="submit">{{
          registering ? '注册' : '登录'
        }}</el-button></el-form
      >
      <p class="form-help" style="margin-top: 14px">账号功能待接入，当前不会创建账号或保存密码。</p>
      <div class="auth-links">
        <RouterLink to="/">← 返回首页</RouterLink
        ><RouterLink :to="registering ? '/login' : '/register'">{{
          registering ? '已有账号？去登录' : '还没有账号？去注册'
        }}</RouterLink>
      </div>
    </section>
  </div>
</template>
