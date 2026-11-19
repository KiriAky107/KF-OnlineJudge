<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
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
const highlighted = ref<Record<string, { content: string; color?: string }[][]>>({})
let highlighter: Awaited<ReturnType<typeof createEditorHighlighter>> | undefined
let alive = true
onMounted(async () => {
  const value = await createEditorHighlighter()
  if (!alive) {
    value.dispose()
    return
  }
  highlighter = value
  highlighted.value = Object.fromEntries(
    Object.entries(languages).map(([language, item]) => [
      language,
      value.codeToTokens(item.code, { lang: editorLanguage(language), theme: 'github-light' })
        .tokens,
    ]),
  )
})
onBeforeUnmount(() => {
  alive = false
  highlighter?.dispose()
})
const results = [
  { badge: 'AC', type: 'success', title: '通过', text: '程序通过了本次评测的全部测试。' },
  { badge: 'WA', type: 'danger', title: '答案错误', text: '输出与预期答案不一致。' },
  {
    badge: 'CE',
    type: 'warning',
    title: '编译错误',
    text: '编译或语法检查失败，查看提交详情中的诊断。',
  },
  { badge: 'RE', type: 'danger', title: '运行错误', text: '程序运行时异常退出。' },
  { badge: 'TLE', type: 'warning', title: '时间超限', text: '程序运行超过题目时间限制。' },
  { badge: 'MLE', type: 'warning', title: '内存超限', text: '程序使用的内存超过限制。' },
  { badge: 'OLE', type: 'warning', title: '输出超限', text: '程序输出超过允许大小。' },
] as const
</script>
<template>
  <PageHeading
    title="简介"
    eyebrow="ABOUT KF ONLINEJUDGE"
    description="选择一道题，编写程序，从每一次运行中进步。"
  />
  <div class="guide-columns">
    <div class="stack">
      <PanelCard title="编译器 & 例题" icon="document">
        <el-tabs v-model="selected" aria-label="示例语言">
          <el-tab-pane
            v-for="(_, language) in languages"
            :key="language"
            :label="languageLabels[language]"
            :name="language"
          />
        </el-tabs>
        <div class="profile-row">
          <strong>{{ current.profile }}</strong
          ><code>{{ current.file }}</code>
        </div>
        <p class="profile-note">
          {{ current.note }}题目工具栏会列出该题的可用语言、运行版本和限额。
        </p>
        <h3>A + B</h3>
        <p>读入两个整数，输出它们的和。</p>
        <div class="sample-pair">
          <div>
            <h4>输入</h4>
            <pre>1 2</pre>
          </div>
          <div>
            <h4>输出</h4>
            <pre>3</pre>
          </div>
        </div>
        <pre
          v-if="highlighted[selected]"
          class="example-code"
        ><code><span v-for="(line, index) in highlighted[selected]" :key="index" class="code-line"><span v-for="(token, tokenIndex) in line" :key="tokenIndex" :style="{ color: token.color }">{{ token.content }}</span>{{ '\n' }}</span></code></pre>
        <pre v-else class="example-code">{{ current.code }}</pre>
      </PanelCard>
      <PanelCard title="开始练习" icon="problems">
        <ol class="guide-steps">
          <li>在题库选择题目，使用右侧编辑器编写代码。</li>
          <li>点击「自测运行」，用样例或自己的输入查看输出。</li>
          <li>点击「提交代码」进行正式评测，在「本题我的提交」查看结果与源码。</li>
          <li>在训练页建立题单，记录进度与练习笔记。</li>
        </ol>
        <RouterLink to="/problems" class="text-link">前往题库 →</RouterLink>
      </PanelCard>
    </div>
    <div class="stack">
      <PanelCard title="结果说明" icon="submissions">
        <dl class="result-list">
          <div v-for="item in results" :key="item.badge">
            <dt>
              <el-tag :type="item.type" effect="light">{{ item.badge }}</el-tag
              ><strong>{{ item.title }}</strong>
            </dt>
            <dd>{{ item.text }}</dd>
          </div>
        </dl>
        <div class="state-notes">
          <p><el-tag type="info">等待评测</el-tag> 提交已接收，正在等待执行。</p>
          <p><el-tag>编译 / 运行中</el-tag> 页面会自动更新进度。</p>
          <p><el-tag type="info">已取消</el-tag> 本次执行已停止。</p>
          <p><el-tag type="danger">系统错误</el-tag> 评测服务执行失败，可稍后重试。</p>
        </div>
      </PanelCard>
      <PanelCard title="编译说明" icon="settings">
        <ul class="guide-steps">
          <li>从标准输入读取数据，向标准输出写入答案；不要输出额外的输入提示。</li>
          <li>普通题按精确字节比较输出，注意空格和换行。</li>
          <li>C/C++ 与 Java 先编译，Python 先做语法检查，再执行程序。</li>
          <li>自测展示标准输出、错误输出与编译信息；正式提交使用题目的测试数据给出判定。</li>
        </ul>
      </PanelCard>
    </div>
  </div>
</template>
<style scoped>
.guide-columns {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.profile-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  margin: 8px 0;
}
.profile-row code {
  color: #6b7f9d;
}
.profile-note {
  color: #7185a3;
  font-size: 13px;
  line-height: 1.8;
  margin-bottom: 20px;
}
h3 {
  margin-bottom: 8px;
}
.sample-pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin: 16px 0;
}
.sample-pair > div {
  padding: 12px 16px;
  border: 1px solid #e4ebf4;
  border-radius: 8px;
  background: #f8fafd;
}
h4 {
  font-size: 12px;
  color: #7185a3;
  margin-bottom: 8px;
}
.example-code {
  overflow: auto;
  border: 1px solid #e4ebf4;
  border-radius: 10px;
  font-size: 13px;
  line-height: 1.8;
}
.example-code {
  padding: 18px;
  background: #fff;
  margin: 0;
}
.result-list {
  margin: 0;
}
.result-list > div {
  padding: 10px 0;
  border-bottom: 1px solid #edf0f5;
}
.result-list dt {
  display: flex;
  align-items: center;
  gap: 12px;
}
.result-list dt .el-tag {
  min-width: 48px;
}
.result-list dd {
  margin: 5px 0 0 60px;
  color: #7185a3;
  font-size: 13px;
  line-height: 1.6;
}
.state-notes {
  margin-top: 18px;
  font-size: 13px;
  color: #7185a3;
  display: grid;
  gap: 10px;
}
.state-notes .el-tag {
  margin-right: 8px;
}
.guide-steps {
  margin: 0 0 16px;
  padding-left: 20px;
  line-height: 1.9;
  color: #60738f;
}
.guide-steps li + li {
  margin-top: 8px;
}
@media (max-width: 850px) {
  .guide-columns {
    grid-template-columns: 1fr;
  }
}
</style>
