// Explicit UI fixtures; business endpoints will be connected through API composables.
export type Difficulty = '入门' | '简单' | '中等' | '困难'
export interface Problem {
  id: string
  title: string
  difficulty: Difficulty
  tags: string[]
  accepted: number
  submissions: number
  updated: string
  description: string
  input: string
  output: string
  sampleInput: string
  sampleOutput: string
  constraint: string
}
const definitions: [string, string, Difficulty, string[], number, number][] = [
  ['1001', 'A + B Problem', '入门', ['模拟', '输入输出'], 428, 536],
  ['1002', '两数之和', '简单', ['数组', '哈希表'], 315, 482],
  ['1003', '最长无重复子串', '中等', ['字符串', '滑动窗口'], 206, 527],
  ['1004', '旅行者的背包', '中等', ['动态规划', '背包'], 186, 461],
  ['1005', '城市间的最短路径', '中等', ['图论', '最短路'], 152, 387],
  ['1006', '寻找第一个位置', '简单', ['二分', '数组'], 241, 402],
  ['1007', '括号匹配', '简单', ['栈', '字符串'], 287, 389],
  ['1008', '岛屿的数量', '中等', ['搜索', '图论'], 143, 328],
  ['1009', '区间求和', '中等', ['数据结构', '前缀和'], 124, 306],
  ['1010', '最小生成树', '困难', ['图论', '并查集'], 96, 288],
  ['1011', '活动安排', '简单', ['贪心', '排序'], 203, 312],
  ['1012', '最长公共子序列', '中等', ['动态规划', '字符串'], 112, 345],
]
export const problems: Problem[] = definitions.map(
  ([id, title, difficulty, tags, accepted, submissions], index) => ({
    id,
    title,
    difficulty,
    tags,
    accepted,
    submissions,
    updated: '2026-10-' + String(4 - Math.floor(index / 4)).padStart(2, '0'),
    description:
      id === '1001'
        ? '给定两个整数 A 和 B，请计算它们的和。认真阅读输入和输出格式，完成你的第一次提交。'
        : '这道题的完整题面将在题库接入后展示。当前提供列表与编辑器的界面预览。',
    input:
      id === '1001'
        ? '输入一行，包含两个整数 A 和 B，以一个空格分隔。'
        : '完整输入格式待题库接入。',
    output: id === '1001' ? '输出一个整数，表示 A 与 B 的和。' : '完整输出格式待题库接入。',
    sampleInput: id === '1001' ? '1 2' : '',
    sampleOutput: id === '1001' ? '3' : '',
    constraint:
      id === '1001'
        ? '−10⁹ ≤ A, B ≤ 10⁹。建议使用 64 位整数保存计算结果。'
        : '数据范围待题库接入。',
  }),
)
export const problemTags = [...new Set(problems.flatMap((problem) => problem.tags))]
export const rankings = [
  {
    username: 'Ada',
    motto: '每天进步一点点',
    solved: 167,
    submissions: 238,
    rating: 1924,
    color: '#6196ec',
  },
  {
    username: 'Kronecker',
    motto: '写下思路，再写代码。',
    solved: 142,
    submissions: 216,
    rating: 1846,
    color: '#6b70d6',
  },
  {
    username: 'Sora',
    motto: '今天也要 AC',
    solved: 129,
    submissions: 204,
    rating: 1732,
    color: '#3fb7a1',
  },
  {
    username: 'Natsume',
    motto: '慢慢来，比较快',
    solved: 118,
    submissions: 182,
    rating: 1685,
    color: '#e9a35d',
  },
  {
    username: 'Lin',
    motto: '保持好奇',
    solved: 106,
    submissions: 175,
    rating: 1621,
    color: '#dc82a4',
  },
  {
    username: 'Mika',
    motto: '从一道题开始',
    solved: 98,
    submissions: 153,
    rating: 1594,
    color: '#63a9c9',
  },
  {
    username: 'Chen',
    motto: 'Hello, algorithm',
    solved: 92,
    submissions: 151,
    rating: 1540,
    color: '#897cb7',
  },
  {
    username: 'Yuki',
    motto: '专注这一题',
    solved: 87,
    submissions: 144,
    rating: 1506,
    color: '#6aaa8a',
  },
  {
    username: 'Joago',
    motto: 'Thinking in code',
    solved: 81,
    submissions: 132,
    rating: 1482,
    color: '#bb9b67',
  },
  {
    username: 'Hyperbola',
    motto: '努力成为更好的自己',
    solved: 74,
    submissions: 128,
    rating: 1428,
    color: '#8095b9',
  },
]
export const notices = [
  {
    id: '1',
    title: '欢迎来到 KF OnlineJudge',
    date: '2026-10-04',
    category: '平台公告',
    text: '从基础练习开始，记录每一次提交，让思路在代码里落地。当前页面使用示例内容。',
  },
  {
    id: '2',
    title: '新学期基础训练题单已更新',
    date: '2026-10-03',
    category: '训练',
    text: '输入输出、数组、字符串和简单搜索，按顺序完成你的第一组算法练习。',
  },
  {
    id: '3',
    title: '周末练习赛 · 热身题单',
    date: '2026-10-01',
    category: '比赛',
    text: '练习赛页面已准备好，实际比赛安排以平台发布的信息为准。',
  },
]
export const trainings = [
  {
    id: 'basics',
    title: '从零开始 · 编程基础',
    category: '基础入门',
    level: '入门',
    description: '从输入输出到数组与字符串，熟悉在线评测的基本流程。',
    ids: ['1001', '1002', '1006', '1007'],
    color: 'blue',
    icon: '01',
  },
  {
    id: 'search',
    title: '搜索与图论入门',
    category: '算法专题',
    level: '中等',
    description: '理解遍历、连通性与路径，建立图论的基础解题思路。',
    ids: ['1008', '1005', '1010'],
    color: 'green',
    icon: '02',
  },
  {
    id: 'dp',
    title: '动态规划 · 一步一步推出来',
    category: '算法专题',
    level: '中等',
    description: '从状态定义开始，练习转移、边界和空间优化。',
    ids: ['1004', '1012'],
    color: 'violet',
    icon: '03',
  },
  {
    id: 'structures',
    title: '常用数据结构练习',
    category: '进阶练习',
    level: '中等',
    description: '通过具体问题理解哈希表、栈、前缀和与并查集。',
    ids: ['1002', '1007', '1009', '1010'],
    color: 'amber',
    icon: '04',
  },
]
export const contests = [
  {
    id: 'weekly-01',
    title: 'KF 周末练习赛 #01',
    type: 'ACM',
    status: '即将开始',
    start: '2026-10-10 14:00',
    duration: '2 小时',
    description: '五道由浅入深的算法题，给自己一个专注思考的下午。',
    ids: ['1001', '1002', '1003', '1004', '1005'],
  },
  {
    id: 'starter',
    title: '新同学编程热身赛',
    type: 'OI',
    status: '即将开始',
    start: '2026-10-12 19:00',
    duration: '2 小时',
    description: '面向刚开始学习程序设计的同学，练习输入输出与基本逻辑。',
    ids: ['1001', '1006', '1007'],
  },
  {
    id: 'graph',
    title: '图论专题 · 路径与连通性',
    type: 'ACM',
    status: '进行中',
    start: '2026-10-04 14:00',
    duration: '4 小时',
    description: '题单式比赛界面示例，状态与时间为展示用数据。',
    ids: ['1005', '1008', '1010'],
  },
  {
    id: 'weekly-00',
    title: 'KF 周末练习赛 #00',
    type: 'ACM',
    status: '已结束',
    start: '2026-09-27 14:00',
    duration: '2 小时',
    description: '往期比赛界面示例，便于浏览题目和比赛说明。',
    ids: ['1001', '1003', '1011'],
  },
  {
    id: 'dp-practice',
    title: '动态规划专项练习',
    type: 'OI',
    status: '已结束',
    start: '2026-09-20 09:00',
    duration: '3 小时',
    description: '集中练习常见动态规划模型。',
    ids: ['1004', '1012'],
  },
]
export type Verdict = 'AC' | 'WA' | 'TLE' | 'RE' | 'CE' | 'Pending'
export const verdictLabels: Record<Verdict, string> = {
  AC: 'Accepted',
  WA: 'Wrong Answer',
  TLE: 'Time Limit Exceeded',
  RE: 'Runtime Error',
  CE: 'Compile Error',
  Pending: '等待评测',
}
export const submissions: {
  id: string
  problemId: string
  username: string
  verdict: Verdict
  language: string
  time: number | null
  memory: number | null
  created: string
}[] = [
  {
    id: '10248',
    problemId: '1001',
    username: 'Kronecker',
    verdict: 'AC',
    language: 'Rust',
    time: 12,
    memory: 2048,
    created: '2026-10-04 14:32:18',
  },
  {
    id: '10247',
    problemId: '1003',
    username: 'Ada',
    verdict: 'AC',
    language: 'C++',
    time: 34,
    memory: 4096,
    created: '2026-10-04 14:30:05',
  },
  {
    id: '10246',
    problemId: '1004',
    username: 'Sora',
    verdict: 'WA',
    language: 'Python',
    time: 62,
    memory: 8192,
    created: '2026-10-04 14:28:41',
  },
  {
    id: '10245',
    problemId: '1005',
    username: 'Lin',
    verdict: 'TLE',
    language: 'C++',
    time: 1000,
    memory: 6144,
    created: '2026-10-04 14:26:22',
  },
  {
    id: '10244',
    problemId: '1002',
    username: 'Mika',
    verdict: 'AC',
    language: 'Python',
    time: 48,
    memory: 7168,
    created: '2026-10-04 14:24:09',
  },
  {
    id: '10243',
    problemId: '1007',
    username: 'Chen',
    verdict: 'CE',
    language: 'C++',
    time: null,
    memory: null,
    created: '2026-10-04 14:21:33',
  },
  {
    id: '10242',
    problemId: '1008',
    username: 'Yuki',
    verdict: 'RE',
    language: 'Rust',
    time: 18,
    memory: 3072,
    created: '2026-10-04 14:19:06',
  },
  {
    id: '10241',
    problemId: '1006',
    username: 'Kronecker',
    verdict: 'AC',
    language: 'Rust',
    time: 11,
    memory: 2048,
    created: '2026-10-04 14:17:26',
  },
  {
    id: '10240',
    problemId: '1011',
    username: 'Natsume',
    verdict: 'Pending',
    language: 'C++',
    time: null,
    memory: null,
    created: '2026-10-04 14:15:12',
  },
  {
    id: '10239',
    problemId: '1009',
    username: 'Ada',
    verdict: 'AC',
    language: 'C++',
    time: 22,
    memory: 4096,
    created: '2026-10-04 14:12:45',
  },
  {
    id: '10238',
    problemId: '1002',
    username: 'Sora',
    verdict: 'WA',
    language: 'Python',
    time: 56,
    memory: 7168,
    created: '2026-10-04 14:10:18',
  },
  {
    id: '10237',
    problemId: '1012',
    username: 'Hyperbola',
    verdict: 'AC',
    language: 'C++',
    time: 71,
    memory: 10240,
    created: '2026-10-04 14:08:51',
  },
]
export const discussions = [
  {
    id: '1',
    title: '第一次使用在线评测，可以从哪里开始？',
    category: '交流',
    author: 'Kronecker',
    replies: 12,
    views: 328,
    date: '2026-10-04',
    pinned: true,
    content:
      '欢迎来到 KF OnlineJudge！可以先打开「从零开始 · 编程基础」题单，了解标准输入输出，再尝试 A + B Problem。\n\n遇到问题时，记录题号、语言和具体报错，把自己的思路一起写出来，讨论会更容易展开。',
  },
  {
    id: '2',
    title: 'A + B Problem：输入输出与整数范围',
    category: '题解',
    author: 'Ada',
    replies: 8,
    views: 216,
    date: '2026-10-04',
    pinned: false,
    content:
      '读取两个整数，计算它们的和，输出结果。\n\n记得检查整数范围。Rust 可以选择 i64；Python 的整数可以直接参与计算。输出只需要答案和换行。',
    problemId: '1001',
  },
  {
    id: '3',
    title: '动态规划中的状态应该怎么定义？',
    category: '问答',
    author: 'Sora',
    replies: 6,
    views: 184,
    date: '2026-10-03',
    pinned: false,
    content:
      '练习背包问题时，我经常卡在状态定义上。现在尝试先描述「已经处理哪些物品」和「剩余多少容量」，再考虑每一步有哪些选择。\n\n大家平时会怎样从题意里提取状态？',
  },
  {
    id: '4',
    title: '分享我的 Rust 刷题输入模板',
    category: '交流',
    author: 'Lin',
    replies: 4,
    views: 152,
    date: '2026-10-02',
    pinned: false,
    content:
      '先用 read_to_string 获取输入，再按空白分隔。\n\n不同题目的输入结构可能不同，使用模板之前还是需要认真阅读题面。',
  },
  {
    id: '5',
    title: '关于题库筛选的一点建议',
    category: '反馈',
    author: 'Mika',
    replies: 2,
    views: 96,
    date: '2026-10-01',
    pinned: false,
    content: '希望题库可以同时按照标签和难度筛选，这样练习某个知识点会更方便。',
  },
]
export function findProblem(id: string) {
  return problems.find((problem) => problem.id === id)
}
export function acceptance(problem: Pick<Problem, 'accepted' | 'submissions'>) {
  return Math.round((problem.accepted / problem.submissions) * 100)
}
