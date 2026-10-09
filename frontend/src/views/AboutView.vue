<script setup lang="ts">
import { computed } from 'vue'
import { usePlatformHealth } from '../composables/usePlatformHealth'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import UiIcon from '../components/UiIcon.vue'
const { state, version, refresh } = usePlatformHealth()
const status = computed(
  () =>
    ({ loading: '正在检查', connected: 'API 进程已连接', unavailable: 'API 暂未连接' })[
      state.value
    ],
)
const features = [
  { icon: 'problems', title: '练习与训练', text: '从基础题目到算法专题，通过题单组织日常练习。' },
  { icon: 'contests', title: '竞赛与评测', text: '围绕提交、评测和比赛，逐步完成在线评测流程。' },
  {
    icon: 'discussions',
    title: '交流与成长',
    text: '分享解题思路，结合练习记录，探索更合适的学习路径。',
  },
]
</script>
<template>
  <PageHeading
    title="关于 KF OnlineJudge"
    eyebrow="ABOUT"
    description="从一道题开始，积累每一次进步。"
  />
  <div class="intro-banner">
    <div>
      <p class="eyebrow">CODE · PRACTICE · GROW</p>
      <h2>让思路在代码里落地。</h2>
      <p>
        KF OnlineJudge
        是一个正在开发的在线评测项目，关注判题、沙箱、分布式执行与故障恢复，并逐步提供辅助答疑和个性化训练。
      </p>
    </div>
    <span class="intro-number">KF.</span>
  </div>
  <div class="stack">
    <PanelCard title="在这里，你可以" icon="reading"
      ><div class="about-grid">
        <div v-for="feature in features" :key="feature.title" class="about-feature">
          <UiIcon :name="feature.icon" />
          <h3>{{ feature.title }}</h3>
          <p>{{ feature.text }}</p>
        </div>
      </div></PanelCard
    ><PanelCard id="system" title="系统状态" icon="monitor"
      ><div class="service-row">
        <div>
          <h3>
            平台 API
            <el-tag
              :type="state === 'connected' ? 'success' : 'info'"
              size="small"
              effect="plain"
              >{{ status }}</el-tag
            >
          </h3>
          <p>
            这是实际的 API 存活检查。{{ version && state === 'connected' ? '版本 ' + version : '' }}
          </p>
          <p>题库和评测服务的可用情况，以各页面的实际响应为准。</p>
        </div>
        <el-button :loading="state === 'loading'" @click="refresh">重新检查</el-button>
      </div></PanelCard
    ><PanelCard title="使用说明" icon="about"
      ><div class="detail-prose">
        <p>
          账号登录、题库和提交记录已接入实际服务。题目页按题目版本公布可提交的语言、运行版本及限额。
        </p>
        <p style="margin-top: 12px">
          题目页可以选择语言、切换编辑器主题和保存本地草稿。提交后可查看评测结果、诊断和本人源码；进行中的评测可申请取消。
        </p>
        <p style="margin-top: 12px">比赛、排名、讨论和团队功能仍在开发，相关页面保留示例内容。</p>
        <p style="margin-top: 12px">备案信息待填写，平台规则和联系方式将在上线前补充。</p>
      </div></PanelCard
    >
  </div>
</template>
