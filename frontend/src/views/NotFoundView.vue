<script setup lang="ts">
import { ElButton, ElResult } from 'element-plus'
</script>

<template>
  <el-result icon="info" title="页面不存在" sub-title="请返回首页继续浏览。">
    <template #extra>
      <RouterLink to="/">
        <el-button type="primary"> 返回首页 </el-button>
      </RouterLink>
    </template>
  </el-result>
</template>
