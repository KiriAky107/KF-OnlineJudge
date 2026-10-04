<template>
  <div class="app-shell">
    <header class="app-header">
      <RouterLink to="/" class="brand"> KF<span>OnlineJudge</span> </RouterLink>
      <span class="header-caption">编程 · 练习 · 成长</span>
    </header>
    <main class="app-main">
      <slot />
    </main>
    <footer class="app-footer">KF-OnlineJudge · 开发预览</footer>
  </div>
</template>
