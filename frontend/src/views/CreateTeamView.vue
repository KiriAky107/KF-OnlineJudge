<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
const form = ref<FormInstance>()
const model = reactive({ name: '', description: '', visibility: 'public', joinMode: 'approval' })
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
async function validate() {
  if (await form.value?.validate().catch(() => false))
    ElMessage.info('填写已通过检查，团队创建功能待服务接入')
}
</script>
<template>
  <PageHeading
    title="创建团队"
    eyebrow="CREATE A TEAM"
    description="为团队起一个名字，写下你们想一起完成的事情。"
    ><RouterLink to="/teams"><el-button>返回团队列表</el-button></RouterLink></PageHeading
  ><PanelCard title="团队资料" icon="teams" class="form-panel"
    ><el-form
      ref="form"
      :model="model"
      :rules="rules"
      label-position="top"
      @submit.prevent="validate"
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
          show-word-limit /></el-form-item
      ><el-form-item label="可见性"
        ><el-radio-group v-model="model.visibility"
          ><el-radio value="public">公开团队</el-radio
          ><el-radio value="private">私有团队</el-radio></el-radio-group
        >
        <p class="form-help">公开团队可在团队列表中发现，私有团队通过邀请加入。</p></el-form-item
      ><el-form-item label="加入方式"
        ><el-radio-group v-model="model.joinMode"
          ><el-radio value="approval">需要审核</el-radio
          ><el-radio value="invite">仅接受邀请</el-radio></el-radio-group
        ></el-form-item
      >
      <div class="form-actions">
        <el-button native-type="submit">检查填写</el-button
        ><el-button type="primary" disabled>创建团队</el-button
        ><span class="muted">团队服务待接入</span>
      </div></el-form
    ></PanelCard
  >
</template>
