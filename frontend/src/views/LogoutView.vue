<script setup lang="ts">
import EmptyState from '../components/EmptyState.vue'
</script>
<template>
  <div class="panel" style="max-width: 650px; margin: 50px auto">
    <EmptyState title="当前没有登录会话" description="账号服务接入后，这里会清除会话并退出登录。"
      ><RouterLink to="/"><el-button type="primary">返回首页</el-button></RouterLink
      ><RouterLink to="/login" style="margin-left: 12px"
        ><el-button>去登录</el-button></RouterLink
      ></EmptyState
    >
  </div>
</template>
