<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import JudgementStatus from '../components/JudgementStatus.vue'
import UiIcon from '../components/UiIcon.vue'
import { useSiteTheme } from '../composables/useSiteTheme'
import { languageLabels } from '../composables/useCodeDraft'
import { createEditorHighlighter, editorLanguage } from '../lib/editorHighlighting'
const selected = ref<'c' | 'cpp' | 'python' | 'java'>('cpp')
const languages = {
  c: {
    profile: 'GNU C · C17',
    file: 'main.c',
    note: '从 main 函数开始执行。',
    code: '#include <stdio.h>\n\nint main(void) {\n    int a, b;\n    scanf("%d %d", &a, &b);\n    printf("%d\\n", a + b);\n    return 0;\n}\n',
  },
  cpp: {
    profile: 'GNU C++ · C++20',
    file: 'main.cpp',
    note: '从 main 函数开始执行，可使用标准库。',
    code: '#include <iostream>\nusing namespace std;\n\nint main() {\n    int a, b;\n    cin >> a >> b;\n    cout << a + b << endl;\n    return 0;\n}\n',
  },
  python: {
    profile: 'CPython 3.13.16',
    file: 'main.py',
    note: '单文件程序，使用 Python 标准库。',
    code: 'import sys\n\na, b = map(int, sys.stdin.read().split())\nprint(a + b)\n',
  },
  java: {
    profile: 'OpenJDK 21',
    file: 'Main.java',
    note: '使用默认包，主类名为 Main。',
    code: 'import java.util.Scanner;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner in = new Scanner(System.in);\n        int a = in.nextInt();\n        int b = in.nextInt();\n        System.out.println(a + b);\n    }\n}\n',
  },
}
const current = computed(() => languages[selected.value])
const { theme } = useSiteTheme()
const highlighter = shallowRef<Awaited<ReturnType<typeof createEditorHighlighter>>>()
const highlighted = computed(
  () =>
    highlighter.value?.codeToTokens(current.value.code, {
      lang: editorLanguage(selected.value),
      theme: theme.value === 'dark' ? 'github-dark' : 'github-light',
    }).tokens,
)
let alive = true
onMounted(async () => {
  try {
    const value = await createEditorHighlighter()
    if (alive) highlighter.value = value
    else value.dispose()
  } catch {
    // The example remains readable as plain text.
  }
})
onBeforeUnmount(() => {
  alive = false
  highlighter.value?.dispose()
})
const verdicts = [
  ['accepted', '通过了本次评测的全部用例。'],
  ['wrong_answer', '输出与预期答案不一致。'],
  ['compilation_error', '编译或语法检查失败，详情页会显示诊断。'],
  ['runtime_error', '程序异常退出。'],
  ['time_limit_exceeded', '程序运行超过题目时间限制。'],
  ['memory_limit_exceeded', '程序使用的内存超过限制。'],
  ['output_limit_exceeded', '程序输出超过允许大小。'],
  ['cancelled', '本次评测已停止。'],
  ['system_error', '评测服务执行失败，可稍后重试。'],
] as const
const phases = [
  ['queued', '提交已接收，正在等待执行。'],
  ['compiling', '编译或语法检查正在进行。'],
  ['running', '程序正在运行测试用例，页面会自动更新。'],
] as const
</script>

<template>
  <PageHeading title="简介" description="在 KF OnlineJudge 解题、自测、提交评测，并按题单练习。">
    <RouterLink class="el-button el-button--primary" to="/problems">
      <UiIcon name="problems" /> 浏览题库
    </RouterLink>
    <RouterLink class="el-button" to="/trainings">
      <UiIcon name="trainings" /> 查看训练集
    </RouterLink>
  </PageHeading>
  <div class="about-guide">
    <div class="guide-overview">
      <PanelCard title="开始练习" icon="problems">
        <ol class="guide-steps">
          <li>
            <strong>选题和编写</strong><span>从题库打开题目，在工作区选择语言并编写代码。</span>
          </li>
          <li><strong>自测</strong><span>用公开样例或自定义输入查看输出和编译信息。</span></li>
          <li><strong>提交评测</strong><span>在当前题目查看判定，点击状态打开评测详情。</span></li>
          <li>
            <strong>按题单练习</strong><span>在训练题单查看进度，打开笔记记录解法和边界。</span>
          </li>
        </ol>
      </PanelCard>
      <PanelCard title="学习助手" icon="assistant">
        <template #actions>
          <RouterLink class="el-button el-button--small" to="/trainings/assistant"
            >打开助手</RouterLink
          >
        </template>
        <ul class="guide-steps">
          <li>
            <strong>自由提问</strong>
            <span>题目侧栏可参考当前公开题面及近 30 天最近做过的最多 8 道题。</span>
          </li>
          <li>
            <strong>授权提交与源码</strong>
            <span>提交详情需逐轮确认。获准的源码片段仅由本机模型分析。</span>
          </li>
          <li>
            <strong>聊天创建训练集</strong>
            <span>指定知识点、难度和题量，审阅草案后创建。</span>
          </li>
          <li>
            <strong>比赛期间暂停</strong>
            <span>确认参赛后，比赛期间暂停使用学习助手，赛后自动恢复。</span>
          </li>
        </ul>
      </PanelCard>
    </div>
    <div class="guide-reference">
      <PanelCard title="编译器与例题" icon="document">
        <el-tabs v-model="selected" aria-label="示例语言">
          <el-tab-pane
            v-for="(_, language) in languages"
            :key="language"
            :label="languageLabels[language]"
            :name="language"
          />
        </el-tabs>
        <div class="profile-row">
          <strong>{{ current.profile }}</strong>
          <code class="tag">{{ current.file }}</code>
        </div>
        <p class="small muted">{{ current.note }} 运行版本和限额可在题目工作区查看。</p>
        <div class="about-example">
          <h3>A + B</h3>
          <p>读入两个整数，输出它们的和。</p>
          <div class="about-samples">
            <div>
              <h4>样例输入</h4>
              <pre class="sample-block">1 2</pre>
            </div>
            <div>
              <h4>样例输出</h4>
              <pre class="sample-block">3</pre>
            </div>
          </div>
          <pre
            v-if="highlighted"
            class="sample-block about-code"
          ><code><span v-for="(line, index) in highlighted" :key="index"><span v-for="(token, tokenIndex) in line" :key="tokenIndex" :style="{ color: token.color }">{{ token.content }}</span>{{ '\n' }}</span></code></pre>
          <pre v-else class="sample-block about-code">{{ current.code }}</pre>
        </div>
        <p class="small muted">
          从标准输入读取数据，向标准输出写答案。普通题采用精确字节比较，注意空格和换行。C、C++ 与
          Java 先编译，Python 先做语法检查。
        </p>
      </PanelCard>
      <PanelCard title="评测状态" icon="submissions">
        <dl class="guide-statuses">
          <div v-for="[state, description] in phases" :key="state">
            <dt><JudgementStatus :state="state" :verdict="null" /></dt>
            <dd>{{ description }}</dd>
          </div>
          <div v-for="[verdict, description] in verdicts" :key="verdict">
            <dt><JudgementStatus state="finished" :verdict="verdict" /></dt>
            <dd>{{ description }}</dd>
          </div>
        </dl>
      </PanelCard>
    </div>
  </div>
</template>

<style scoped>
.about-guide,
.guide-overview,
.guide-reference {
  display: grid;
  gap: 1rem;
  min-width: 0;
}
.guide-overview,
.guide-reference {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.guide-overview > .panel,
.guide-reference > .panel {
  min-width: 0;
}
.guide-steps {
  margin: 0;
  padding-inline-start: 1.25rem;
  color: var(--secondary-ink);
  line-height: 1.6;
}
.guide-steps li + li {
  margin-top: 0.5rem;
}
.guide-steps strong {
  display: block;
  color: var(--strong-ink);
}
.profile-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.35rem;
}
.about-example {
  display: grid;
  gap: 0.5rem;
  margin-block: 1rem;
}
.about-example h3,
.about-samples h4 {
  margin: 0;
}
.about-samples {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}
.about-samples h4 {
  color: var(--strong-ink);
  font-size: 0.875rem;
}
.about-code {
  margin-top: 0.3rem;
  overflow: auto;
}
.guide-statuses {
  margin: 0;
}
.guide-statuses > div {
  display: grid;
  grid-template-columns: minmax(9.5rem, auto) minmax(0, 1fr);
  align-items: baseline;
  gap: 0.75rem;
  padding-block: 0.52rem;
}
.guide-statuses > div + div {
  border-top: 1px solid var(--line);
}
.guide-statuses dd {
  margin: 0;
  color: var(--secondary-ink);
  font-size: 0.85rem;
}
@media (max-width: 850px) {
  .guide-overview,
  .guide-reference {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 420px) {
  .guide-statuses > div {
    grid-template-columns: minmax(0, 1fr);
    gap: 0.25rem;
  }
}
</style>
