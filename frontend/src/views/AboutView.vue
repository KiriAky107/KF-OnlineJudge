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
          <p>数据库、题库、评测与学习服务将分别接入。</p>
        </div>
        <el-button :loading="state === 'loading'" @click="refresh">重新检查</el-button>
      </div></PanelCard
    ><PanelCard title="使用说明" icon="about"
      ><div class="detail-prose">
        <p>
          当前为前端预览版本。题目、比赛、排名和用户信息使用示例数据；页面筛选、导航和本地代码草稿已可使用。
        </p>
        <p style="margin-top: 12px">
          题目页可以选择语言、切换编辑器主题、保存草稿和填写自测输入。实际评测、账号和团队创建将在服务接入后开放。
        </p>
        <p style="margin-top: 12px">备案信息待填写，平台规则和联系方式将在上线前补充。</p>
      </div></PanelCard
    >
  </div>
</template>
