<script setup lang="ts">
import { computed } from 'vue'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import { usePlatformHealth } from '../composables/usePlatformHealth'
const { state, version, refresh } = usePlatformHealth()
const status = computed(
  () =>
    ({ loading: '正在检查', connected: 'API 已连接', unavailable: 'API 暂未连接' })[state.value],
)
const repository = 'https://github.com/KiriAky107/KF-OnlineJudge'
const stack = [
  {
    name: '前端',
    tools: 'Vue 3 · TypeScript · Vite · Element Plus',
    text: 'Monaco 编辑器与 Shiki 语法高亮。',
  },
  {
    name: '平台服务',
    tools: 'Python · FastAPI · PostgreSQL',
    text: '账号、题目、提交、自测和个人训练。',
  },
  {
    name: '评测执行',
    tools: 'Rust · Linux Sandbox',
    text: 'Coordinator 调度任务，Worker 编译并运行程序。',
  },
  {
    name: '任务与契约',
    tools: 'RabbitMQ · OpenAPI · JSON Schema',
    text: '共享接口类型与版本化评测协议。',
  },
]
const documents = [
  { title: '项目说明', path: '/blob/main/README.md', text: '工程结构、启动方式与检查命令' },
  { title: '部署说明', path: '/blob/main/deploy/README.md', text: '平台服务与运行环境' },
  { title: '判题引擎', path: '/blob/main/judge/README.md', text: '沙箱、Worker 与调度机制' },
  { title: '接口契约', path: '/tree/main/contracts', text: 'OpenAPI 与评测协议' },
]
</script>
<template>
  <PageHeading
    title="开发者"
    eyebrow="BUILD KF ONLINEJUDGE"
    description="了解项目结构，查阅工程文档，参与改进。"
  />
  <div class="developer-grid">
    <PanelCard title="技术栈" icon="settings">
      <div class="stack-list">
        <article v-for="item in stack" :key="item.name">
          <h3>{{ item.name }}</h3>
          <strong>{{ item.tools }}</strong>
          <p>{{ item.text }}</p>
        </article>
      </div>
    </PanelCard>
    <PanelCard title="源码与文档" icon="document">
      <a class="repository-link" :href="repository" target="_blank" rel="noopener noreferrer"
        >KiriAky107 / KF-OnlineJudge ↗</a
      >
      <div class="document-list">
        <a
          v-for="item in documents"
          :key="item.path"
          :href="repository + item.path"
          target="_blank"
          rel="noopener noreferrer"
          ><strong>{{ item.title }} ↗</strong><span>{{ item.text }}</span></a
        >
      </div>
    </PanelCard>
    <PanelCard title="参与贡献" icon="connection">
      <p class="developer-copy">
        发现问题时，请附上复现步骤、预期表现和实际结果。代码改动可通过 Pull Request
        提交，按受影响模块运行项目检查。
      </p>
      <div class="contribution-links">
        <a :href="repository + '/issues'" target="_blank" rel="noopener noreferrer">反馈问题 ↗</a
        ><a :href="repository + '/pulls'" target="_blank" rel="noopener noreferrer"
          >查看 Pull Requests ↗</a
        >
      </div>
    </PanelCard>
    <PanelCard id="system" title="系统状态" icon="monitor">
      <div class="service-row">
        <div>
          <h3>
            平台 API
            <el-tag :type="state === 'connected' ? 'success' : 'info'" effect="plain">{{
              status
            }}</el-tag>
          </h3>
          <p v-if="version && state === 'connected'">版本 {{ version }}</p>
          <p class="developer-copy">检查平台 API 的当前连接状态。</p>
        </div>
        <el-button :loading="state === 'loading'" @click="refresh">重新检查</el-button>
      </div>
    </PanelCard>
  </div>
</template>
<style scoped>
.developer-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.stack-list {
  display: grid;
  gap: 20px;
}
h3 {
  font-size: 14px;
  margin-bottom: 8px;
}
.stack-list strong {
  color: var(--secondary-ink);
  font-size: 14px;
}
.stack-list p,
.developer-copy {
  color: var(--secondary-ink);
  line-height: 1.8;
  margin-top: 8px;
  font-size: 13px;
}
.repository-link {
  display: block;
  padding: 15px;
  border-radius: 8px;
  background: var(--accent-bg);
  color: var(--blue);
  font-weight: 600;
}
.document-list {
  margin-top: 14px;
  display: grid;
}
.document-list a {
  display: grid;
  gap: 6px;
  padding: 14px 0;
  border-bottom: 1px solid var(--line);
}
.document-list strong {
  color: var(--secondary-ink);
}
.document-list span {
  color: var(--secondary-ink);
  font-size: 13px;
}
.contribution-links {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  margin-top: 20px;
  color: var(--blue);
}
#system {
  scroll-margin-top: 95px;
}
@media (max-width: 850px) {
  .developer-grid {
    grid-template-columns: 1fr;
  }
}
</style>
