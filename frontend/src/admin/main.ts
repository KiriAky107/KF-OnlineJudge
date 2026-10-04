import { createApp } from 'vue'
import { registerUi } from '../plugins/ui'
import { adminRouter } from './router'
import AdminApp from './AdminApp.vue'
import '../styles/base.css'
import '../styles/blueprint.css'

const app = createApp(AdminApp)
registerUi(app)
app.use(adminRouter).mount('#admin-app')
