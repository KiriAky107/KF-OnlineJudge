<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { type FormInstance, type FormRules } from 'element-plus'
import { api } from '../api/client'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import { useSession } from '../composables/useSession'
import { newRequestId } from '../lib/requestCrypto'

const { user, refresh } = useSession()
const router = useRouter()
const form = ref<FormInstance>()
const model = reactive({ name: '', description: '' })
const key = ref('')
const saving = ref(false)
const error = ref('')
const scope = new AbortController()
const rules: FormRules<typeof model> = {
  name: [
    { required: true, message: '请填写团队名称', trigger: 'blur' },
    { min: 2, max: 30, message: '名称长度为 2 到 30 个字符', trigger: 'blur' },
  ],
  description: [
    { required: true, message: '请填写团队介绍', trigger: 'blur' },
    { max: 500, message: '介绍最多 500 个字符', trigger: 'blur' },
  ],
}
async function create() {
  if (!user.value || saving.value) return
  const owner = user.value.id
  saving.value = true
  error.value = ''
  try {
    if (!(await form.value?.validate().catch(() => false))) return
    if (!key.value) key.value = newRequestId()
    const team = unwrap(
      await api.POST('/api/v1/teams', {
        body: { ...model, idempotency_key: key.value },
        headers: csrfHeaders(),
        signal: scope.signal,
      }),
    )
    if (!scope.signal.aborted && user.value?.id === owner)
      await router.push({ name: 'team-detail', params: { id: team.id } })
  } catch (cause) {
    if (!scope.signal.aborted) error.value = errorText(cause)
  } finally {
    if (!scope.signal.aborted) saving.value = false
  }
}
watch([() => model.name, () => model.description, () => user.value?.id], () => (key.value = ''))
onMounted(() => {
  if (!user.value) void refresh().catch((cause) => (error.value = errorText(cause)))
})
onBeforeUnmount(() => scope.abort())
</script>
<template>
  <PageHeading
    title="创建团队"
    eyebrow="CREATE A TEAM"
    description="为团队起一个名字，写下你们想一起完成的事情。"
    ><RouterLink to="/teams"><el-button>返回团队列表</el-button></RouterLink></PageHeading
  >
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="!user"><RouterLink to="/login?next=/teams/create">登录</RouterLink>后创建团队。</p>
  <PanelCard v-else title="团队资料" icon="teams" class="form-panel"
    ><el-form
      ref="form"
      :model="model"
      :rules="rules"
      label-position="top"
      :disabled="saving"
      @submit.prevent="create"
      ><el-form-item label="团队名称" prop="name"
        ><el-input
          v-model="model.name"
          placeholder="例如：KF 算法研习组"
          maxlength="30"
          show-word-limit /></el-form-item
      ><el-form-item label="团队介绍" prop="description"
        ><el-input
          v-model="model.description"
          type="textarea"
          :rows="5"
          placeholder="介绍团队的练习方向、交流方式和目标"
          maxlength="500"
          show-word-limit
      /></el-form-item>
      <p class="form-help">团队资料公开，成员申请由队长审核。</p>
      <div class="form-actions">
        <el-button type="primary" native-type="submit" :loading="saving">创建团队</el-button>
      </div></el-form
    ></PanelCard
  >
</template>
