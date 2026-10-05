import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { registerUi } from './plugins/ui'
import App from './App.vue'
import { router } from './router'
import './styles/base.css'
import './styles/blueprint.css'

const app = createApp(App)
registerUi(app)
app.use(createPinia()).use(router).mount('#app')
