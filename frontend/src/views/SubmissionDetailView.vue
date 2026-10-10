<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import SubmissionInspector from '../components/SubmissionInspector.vue'
const route = useRoute()
const id = computed(() => String(route.params.id))
</script>
<template><SubmissionInspector :submission-id="id" /></template>
