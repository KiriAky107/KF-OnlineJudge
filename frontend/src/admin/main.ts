import { createApp } from 'vue'
import { registerUi } from '../plugins/ui'
import { adminRouter } from './router'
import AdminApp from './AdminApp.vue'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '../styles/base.css'
import '../styles/blueprint.css'
import './admin.css'

import { initSiteTheme } from '../composables/useSiteTheme'

initSiteTheme()
const app = createApp(AdminApp)
registerUi(app)
app.use(adminRouter).mount('#admin-app')
